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
# `convert` is the compression tool the harness always needs; the rest come from
# the manifest. For rows that share one binary via `bin`, the cargo *package* is
# the basename of that path (e.g. bench-onpair16 -> bench-onpair), so we dedupe
# on package name rather than the logical row name.
mapfile -t CRATES < <(uv run python -c "
from pathlib import Path
from harness.manifest import load_manifest
pkgs = {'convert'}
for b in load_manifest('benchmarks.toml'):
    if b.enabled and b.lang == 'rust':
        pkgs.add(Path(b.bin).name if b.bin else b.name)
for p in sorted(pkgs):
    print(p)
")

PKG_ARGS=()
for c in "${CRATES[@]}"; do PKG_ARGS+=(-p "$c"); done
log "packages: ${CRATES[*]}"
cargo build --release "${PKG_ARGS[@]}"

# ---------------------------------------------------------------------------
# 5. C++ binaries (bench-compress-cpp: FSST/FSST12/Dictionary/LZ4). Built when
#    the manifest has any enabled C++ engine and a compiler toolchain exists.
# ---------------------------------------------------------------------------
HAVE_CPP="$(uv run python -c "
from harness.manifest import load_manifest
print(any(b.enabled and b.lang == 'cpp' for b in load_manifest('benchmarks.toml')))
")"
if [ "$HAVE_CPP" = "True" ]; then
  if have cmake && (have g++ || have clang++); then
    log "building C++ engines (cmake) ..."
    cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
    cmake --build cpp/build -j
  else
    log "WARNING: enabled C++ engines but cmake/g++ missing; skipping. Install cmake + a C++20 compiler."
  fi
fi

log "done. Run the benchmark with:  python run.py   (or: uv run python run.py)"
