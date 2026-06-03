#!/usr/bin/env bash
# Reproducible one-shot setup for the LIKE-pushdown benchmark.
#
#   ./setup.sh && python run.py
#
# Installs (only what is missing): uv (Python env manager), the Python deps via
# `uv sync`, the Rust toolchain via rustup, then builds the release binaries.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1. Python (we require an interpreter to already exist; uv manages the venv).
# ---------------------------------------------------------------------------
if ! have python3 && ! have python; then
  echo "error: no python3/python on PATH. Install Python >= 3.11 first." >&2
  exit 1
fi
log "python: $(python3 --version 2>&1 || python --version)"

# ---------------------------------------------------------------------------
# 2. uv (fast, reproducible Python env manager).
# ---------------------------------------------------------------------------
if ! have uv; then
  log "installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin or ~/.cargo/bin depending on platform.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
log "uv: $(uv --version)"

log "syncing Python deps (uv sync) ..."
uv sync

# ---------------------------------------------------------------------------
# 3. Rust toolchain via rustup.
# ---------------------------------------------------------------------------
if ! have cargo; then
  log "installing rustup ..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  # shellcheck disable=SC1090,SC1091
  source "$HOME/.cargo/env"
fi
log "cargo: $(cargo --version)"

# ---------------------------------------------------------------------------
# 4. Build release binaries for every *enabled* rust engine in benchmarks.toml.
#    We let the harness decide which crates are enabled so this stays in sync.
# ---------------------------------------------------------------------------
log "building enabled rust binaries (cargo build --release) ..."
mapfile -t CRATES < <(uv run python -c "
from harness.manifest import load_manifest
for b in load_manifest('benchmarks.toml'):
    if b.enabled and b.lang == 'rust':
        print(b.name)
")

if [ "${#CRATES[@]}" -eq 0 ]; then
  log "no enabled rust binaries; skipping cargo build"
else
  PKG_ARGS=()
  for c in "${CRATES[@]}"; do PKG_ARGS+=(-p "$c"); done
  log "packages: ${CRATES[*]}"
  cargo build --release "${PKG_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
# 5. (Later) C++ binaries: cmake + a C++17 compiler.
#    Uncomment once cpp/ binaries exist.
# ---------------------------------------------------------------------------
# if ! have cmake; then echo "install cmake + a C++17 compiler"; fi
# cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release && cmake --build cpp/build -j

log "done. Run the benchmark with:  python run.py   (or: uv run python run.py)"
