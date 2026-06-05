#!/usr/bin/env bash
# Reproducible one-shot setup for likebench.
#
#   ./setup.sh && python run.py
#
# Installs (only what is missing): uv (Python env manager) + the Python deps,
# the Rust toolchain via rustup, then builds the workspace in release mode.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

log() { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# 1. Python (an interpreter must already exist; uv manages the venv).
if ! have python3 && ! have python; then
  echo "error: no python3/python on PATH. Install Python >= 3.11 first." >&2
  exit 1
fi

# 2. uv (fast, reproducible Python env manager).
if ! have uv; then
  log "installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
log "syncing Python deps (uv sync) ..."
uv sync

# 3. Rust toolchain via rustup.
if ! have cargo; then
  log "installing rustup ..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  # shellcheck disable=SC1090,SC1091
  source "$HOME/.cargo/env"
fi
log "cargo: $(cargo --version)"

# 4. Build the whole workspace (engines + tools land in later layers).
log "building release binaries (cargo build --release --workspace) ..."
cargo build --release --workspace

log "done. Run the benchmark with:  python run.py   (or: uv run python run.py)"
