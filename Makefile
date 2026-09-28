PYTHON ?= .venv/bin/python
PORT   ?= 8000

.PHONY: install build serve clean

# Create the venv if needed and install everything into it (nothing global).
install:
	@test -x $(PYTHON) || uv venv
	uv pip install --python $(PYTHON) -r requirements.txt

build:
	$(PYTHON) build.py

serve:
	$(PYTHON) build.py serve --port $(PORT)

clean:
	rm -rf dist
