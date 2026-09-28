/* PatchBench site behavior: page reveal, theme toggle, leaderboard
   controls, chart tooltips, copy-to-clipboard. No dependencies. */
(function () {
  "use strict";

  const root = document.documentElement;

  /* ------------------------------------------------------------- fade-in */
  // The top bar and the content below it are revealed separately. The top bar
  // appears as soon as its own fonts are ready. The content fades in once the
  // whole page has finished rendering. Each step is capped so a slow resource
  // cannot hold the page back.
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  // Resolves after the browser paints a frame (or 100 ms, where frames are paused).
  const afterPaint = () => Promise.race([
    new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    wait(100),
  ]);
  const fonts = document.fonts;
  const barFonts = fonts
    ? Promise.all([fonts.load('600 21px "Fraunces"'), fonts.load('500 14px "Public Sans"')])
    : Promise.resolve();
  Promise.race([barFonts.then(afterPaint), wait(800)])
    .then(() => root.classList.add("bar-ready"));

  const loaded = document.readyState === "complete"
    ? Promise.resolve()
    : new Promise((resolve) => window.addEventListener("load", resolve, { once: true }));
  // Opened at a section (e.g. index.html#leaderboard from another page): the
  // browser jumps there before fonts load, so line the section up again once
  // layout has settled, just before the content fades in.
  const alignToHash = () => {
    const id = decodeURIComponent(location.hash.slice(1));
    const target = id && document.getElementById(id);
    if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
  };
  Promise.race([Promise.all([loaded, fonts ? fonts.ready : null]).then(afterPaint), wait(1500)])
    .then(() => { alignToHash(); root.classList.add("is-ready"); });

  /* ---------------------------------------------------------------- theme */
  const toggle = document.querySelector(".theme-toggle");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
  const currentTheme = () => root.dataset.theme || (prefersDark.matches ? "dark" : "light");

  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("patchbench:theme", next); } catch (e) { /* storage unavailable */ }
    });
  }

  /* ---------------------------------------------------------- leaderboard */
  const board = document.querySelector("[data-board]");
  if (board) {
    const tbody = board.querySelector("tbody");
    const rows = Array.from(tbody.rows);

    board.querySelectorAll("[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const type = btn.dataset.filter;
        board.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
        rows.forEach((r) => { r.hidden = type !== "all" && r.dataset.type !== type; });
      });
    });

    // Lower is better for these columns, so their first click sorts ascending.
    const ascendingFirst = new Set(["avg_cost", "budget_exhausted"]);
    const headers = Array.from(board.querySelectorAll("th"));

    board.querySelectorAll("th button[data-sort]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.sort;
        const th = btn.closest("th");
        const was = th.getAttribute("aria-sort");
        const dir = was ? (was === "descending" ? "ascending" : "descending")
                        : (ascendingFirst.has(key) ? "ascending" : "descending");

        headers.forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", dir);

        // Row attributes are data-<key> with underscores, which dataset keeps as-is.
        const sign = dir === "ascending" ? 1 : -1;
        rows
          .slice()
          .sort((a, b) => sign * (parseFloat(a.dataset[key]) - parseFloat(b.dataset[key]))
                          || parseInt(a.dataset.rank, 10) - parseInt(b.dataset.rank, 10))
          .forEach((r) => tbody.appendChild(r));
      });
    });
  }

  /* -------------------------------------------------------------- tooltip */
  const tip = document.querySelector(".tooltip");

  function fillTip(el) {
    let rows = [];
    try { rows = JSON.parse(el.dataset.tip || "[]"); } catch (e) { return false; }
    tip.replaceChildren();
    if (el.dataset.tipTitle) {
      const title = document.createElement("div");
      title.className = "tooltip-title";
      title.textContent = el.dataset.tipTitle;
      tip.appendChild(title);
    }
    rows.forEach(([label, value, color]) => {
      const row = document.createElement("div");
      row.className = "tooltip-row";
      const key = document.createElement("span");
      key.className = "tooltip-key";
      if (color) key.style.setProperty("--c", `var(${color})`);
      const val = document.createElement("span");
      val.className = "tooltip-value";
      val.textContent = value;
      const lab = document.createElement("span");
      lab.className = "tooltip-label";
      lab.textContent = label;
      row.append(key, val, lab);
      tip.appendChild(row);
    });
    return true;
  }

  function placeTip(x, y) {
    const pad = 12;
    const { width, height } = tip.getBoundingClientRect();
    let left = x + 14;
    let top = y + 14;
    if (left + width > window.innerWidth - pad) left = x - width - 14;
    if (top + height > window.innerHeight - pad) top = y - height - 14;
    tip.style.left = `${Math.max(pad, left)}px`;
    tip.style.top = `${Math.max(pad, top)}px`;
  }

  function hideTip() { if (tip) tip.hidden = true; }

  if (tip) {
    document.addEventListener("pointermove", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (!el) return hideTip();
      if (tip.hidden || tip._for !== el) {
        if (!fillTip(el)) return;
        tip._for = el;
        tip.hidden = false;
      }
      placeTip(e.clientX, e.clientY);
    });
    document.addEventListener("pointerleave", hideTip);
    document.addEventListener("scroll", hideTip, { passive: true });

    document.addEventListener("focusin", (e) => {
      const el = e.target.closest && e.target.closest("[data-tip]");
      if (!el || !fillTip(el)) return hideTip();
      tip._for = el;
      tip.hidden = false;
      const r = el.getBoundingClientRect();
      placeTip(r.right, r.top);
    });
    document.addEventListener("focusout", hideTip);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
  }

  /* ----------------------------------------------------------------- copy */
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const src = document.querySelector(btn.dataset.copy);
      if (!src) return;
      const label = btn.textContent;
      try {
        await navigator.clipboard.writeText(src.textContent.trim());
        btn.textContent = "Copied";
      } catch (e) {
        btn.textContent = "Press ⌘/Ctrl+C";
        const range = document.createRange();
        range.selectNodeContents(src);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
      setTimeout(() => { btn.textContent = label; }, 1600);
    });
  });
})();
