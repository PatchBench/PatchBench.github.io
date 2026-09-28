#!/usr/bin/env python3
"""Static site generator for the PatchBench leaderboard.

Reads YAML from data/, renders every template in templates/pages/ with
Jinja2, and writes the site to dist/ together with static/.

    python build.py                   # build once into dist/
    python build.py serve [--port N]  # build, serve dist/, rebuild on change
"""

from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import threading
import time
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
DIST = ROOT / "dist"

WATCHED = (DATA, TEMPLATES, STATIC)


def load_yaml(name: str) -> dict:
    with open(DATA / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

AVERAGED_FIELDS = ("solved", "poc", "security", "semantic", "budget_exhausted")


def prepare_leaderboard(board: dict) -> dict:
    agents = sorted(board["agents"], key=lambda a: a["solved"], reverse=True)
    for rank, a in enumerate(agents, start=1):
        a["rank"] = rank
        a["display"] = f'{a["agent"]} + {a["model"]}' if a["type"] == "general" else a["agent"]
        a.setdefault("team", None)
        a.setdefault("url", None)
        a.setdefault("note", None)
        a.setdefault("chart", None)

    n = len(agents)
    average = {k: sum(a[k] for a in agents) / n for k in AVERAGED_FIELDS}

    return {
        "agents": agents,
        "average": average,
        "count": n,
        "inflation": average["poc"] / average["solved"],
        "notes": [a for a in agents if a["note"]],
    }


# ---------------------------------------------------------------------------
# Chart geometry. Templates only draw what these functions lay out.
# ---------------------------------------------------------------------------

SERIES_VAR = {"general": "--series-1", "crs": "--series-2"}
SERIES_NAME = {"general": "General-purpose agent", "crs": "AIxCC CRS"}


def _ticks(lo: float, hi: float, step: float) -> list[float]:
    out, v = [], lo
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def cost_scatter(agents: list[dict]) -> dict:
    """Solved rate vs. average cost per task, with a broken x-axis so the
    $20-budget Atlantis outlier fits without squashing everyone else."""
    width, height = 960, 530
    left, right, top, bottom = 56, 24, 16, 56
    plot_w, plot_h = width - left - right, height - top - bottom
    gap, outlier_w = 30, 64

    # (domain_lo, domain_hi, tick_lo, tick_step, pixel_x0, pixel_width)
    segments = [
        (0.0, 2.6, 0.0, 0.5, left, plot_w - gap - outlier_w),
        (16.0, 17.0, 16.0, 1.0, left + plot_w - outlier_w, outlier_w),
    ]
    y_lo, y_hi = 27.0, 62.0

    def sx(v: float) -> float:
        for lo, hi, _, _, x0, w in segments:
            if v <= hi or (lo, hi) == segments[-1][:2]:
                return x0 + (min(max(v, lo), hi) - lo) / (hi - lo) * w
        raise AssertionError

    def sy(v: float) -> float:
        return top + (y_hi - v) / (y_hi - y_lo) * plot_h

    x_ticks = []
    for lo, hi, t0, step, _, _ in segments:
        for t in _ticks(t0, hi, step):
            x_ticks.append({"x": sx(t), "label": f"${t:g}"})

    y_ticks = [{"y": sy(t), "label": f"{t:g}%"} for t in _ticks(30, 60, 10)]

    offsets = {  # label placement relative to the point: dx, dy, anchor
        "right": (10, 4, "start"),
        "left": (-10, 4, "end"),
        "above": (0, -11, "middle"),
        "below": (0, 19, "middle"),
    }
    points = []
    for a in agents:
        cx, cy = sx(a["avg_cost"]), sy(a["solved"])
        pt = {
            "cx": round(cx, 1),
            "cy": round(cy, 1),
            "color": SERIES_VAR[a["type"]],
            "title": a["display"],
            "tip": [
                ["Solved", f'{a["solved"]:.1f}%', None],
                ["Avg. cost", f'${a["avg_cost"]:.2f} / task', None],
            ],
            "label": None,
        }
        if a["chart"]:
            dx, dy, anchor = offsets[a["chart"]]
            pt["label"] = {"text": a["display"], "x": round(cx + dx, 1), "y": round(cy + dy, 1), "anchor": anchor}
        points.append(pt)

    return {
        "width": width,
        "height": height,
        "plot": {"left": left, "top": top, "right": left + plot_w, "bottom": top + plot_h},
        "segments": [{"x0": x0, "x1": x0 + w} for *_, x0, w in segments],
        "break_x": segments[0][4] + segments[0][5] + gap / 2,
        "x_ticks": x_ticks,
        "y_ticks": y_ticks,
        "points": points,
        "legend": [{"name": SERIES_NAME[k], "color": v} for k, v in SERIES_VAR.items()],
    }


def hbar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Horizontal bar: square at the baseline, rounded data-end."""
    r = min(r, w / 2, h / 2)
    return (
        f"M{x:.1f},{y:.1f}H{x + w - r:.1f}"
        f"A{r},{r} 0 0 1 {x + w:.1f},{y + r:.1f}"
        f"V{y + h - r:.1f}"
        f"A{r},{r} 0 0 1 {x + w - r:.1f},{y + h:.1f}"
        f"H{x:.1f}Z"
    )


def memorization_chart(mem: dict) -> dict:
    """Grouped horizontal bars: share of patches flagged as memorized."""
    width = 960
    left, right, top = 176, 72, 8
    bar_h, bar_gap, group_gap, axis_h = 14, 2, 22, 30
    x_max = 30.0
    plot_w = width - left - right
    series = mem["series"]
    group_h = len(series) * bar_h + (len(series) - 1) * bar_gap

    def sx(v: float) -> float:
        return left + v / x_max * plot_w

    groups = []
    y = top
    for g in mem["groups"]:
        bars = []
        for i, s in enumerate(series):
            v = g[s["key"]]
            by = y + i * (bar_h + bar_gap)
            w = sx(v) - left
            bars.append({
                "path": hbar_path(left, by, w, bar_h) if w > 0.5 else None,
                "hit_y": by - 1,
                "value": f"{v:.1f}%",
                "label_x": round(left + w + 6, 1),
                "label_y": round(by + bar_h / 2 + 4, 1),
                "color": f"--series-{i + 1}",
                "tip": [[s["name"], f"{v:.1f}%", f"--series-{i + 1}"]],
            })
        groups.append({
            "family": g["family"],
            "agent": g["agent"],
            "cy": y + group_h / 2,
            "bars": bars,
            "title": f'{g["family"]} ({g["agent"]})',
        })
        y += group_h + group_gap

    plot_bottom = y - group_gap
    return {
        "width": width,
        "height": plot_bottom + axis_h,
        "left": left,
        "right": left + plot_w,
        "top": top,
        "bottom": plot_bottom,
        "bar_h": bar_h,
        "x_ticks": [{"x": sx(t), "label": f"{t:g}%"} for t in _ticks(0, x_max, 10)],
        "groups": groups,
        "legend": [{"name": s["name"], "color": f"--series-{i + 1}"} for i, s in enumerate(series)],
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v: f"{v:.1f}"
    env.filters["usd"] = lambda v: f"${v:,.2f}"
    return env


def build() -> None:
    started = time.perf_counter()
    site = load_yaml("site.yaml")
    board = prepare_leaderboard(load_yaml("leaderboard.yaml"))
    bench = load_yaml("benchmark.yaml")

    context = {
        "site": site,
        "board": board,
        "bench": bench,
        "charts": {
            "cost": cost_scatter(board["agents"]),
            "memorization": memorization_chart(bench["memorization"]),
        },
        "build_id": str(int(time.time())),
        "year": time.strftime("%Y"),
    }

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    shutil.copytree(STATIC, DIST / "static")
    (DIST / ".nojekyll").touch()

    env = make_env()
    pages = sorted((TEMPLATES / "pages").glob("*.html"))
    for page in pages:
        template = env.get_template(f"pages/{page.name}")
        html = template.render(**context, page_id=page.stem)
        (DIST / page.name).write_text(html, encoding="utf-8")

    elapsed = (time.perf_counter() - started) * 1000
    print(f"Built {len(pages)} pages into {DIST.relative_to(ROOT)}/ in {elapsed:.0f} ms")


# ---------------------------------------------------------------------------
# Dev server with rebuild-on-change
# ---------------------------------------------------------------------------

def _snapshot() -> dict:
    return {
        p: p.stat().st_mtime
        for d in WATCHED
        for p in d.rglob("*")
        if p.is_file()
    }


def _watch(interval: float = 0.5) -> None:
    last = _snapshot()
    while True:
        time.sleep(interval)
        current = _snapshot()
        if current != last:
            last = current
            try:
                build()
            except Exception as exc:  # keep serving the last good build
                print(f"Build failed: {exc!r}")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - signature from stdlib
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve(port: int) -> None:
    build()
    handler = functools.partial(_QuietHandler, directory=str(DIST))
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise SystemExit(f"Cannot bind port {port} ({exc.strerror}); try --port {port + 1}")
    threading.Thread(target=_watch, daemon=True).start()
    print(f"Serving http://localhost:{port}  (watching data/, templates/, static/; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["build", "serve"], default="build")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.port)
    else:
        build()


if __name__ == "__main__":
    main()
