#!/usr/bin/env bash
# Build the DuckDB `vortex` extension from source, matching the DuckDB version
# the `duckdb` crate bundles (currently v1.5.3) AND the current Vortex on-disk
# format.
#
# Why this exists
# ---------------
# The `vortex` *community* extension (INSTALL vortex FROM community) lags
# upstream: at the time of writing it is published only for DuckDB v1.2.2–v1.4.2,
# and even those builds predate the `vortex.variant` array encoding emitted by
# the Vortex >= 0.74 writer this repo uses — so they 404 on DuckDB 1.5.x and
# fail to read our files on 1.4.x. Building the extension from `duckdb-vortex`
# HEAD produces an extension whose pinned DuckDB *and* Vortex submodules are
# current, so it loads into the bundled DuckDB and reads files written by the
# `convert` binary.
#
# Output
# ------
# Prints the path of the loadable extension. Use it with the harness via:
#
#   export VORTEX_DUCKDB_EXTENSION=<printed path>
#   # enable bench-duckdb in benchmarks.toml, then `python run.py ...`
#
# or with the bundled CLI directly (extension is statically linked there):
#
#   <repo>/build/release/duckdb -c "SELECT * FROM read_vortex('file.vortex')"
#
# Requirements: git, cmake, ninja (or make), a C++20 compiler, clang/libclang
# (for the Rust bindgen step), and rustup (the pinned toolchain is installed
# automatically from the submodule's rust-toolchain.toml).
set -euo pipefail

REPO="${VORTEX_DUCKDB_REPO:-https://github.com/vortex-data/duckdb-vortex}"
REF="${VORTEX_DUCKDB_REF:-main}"
WORKDIR="${VORTEX_DUCKDB_DIR:-$(pwd)/.vortex-duckdb-build}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

log() { printf '\033[1;34m[build-ext]\033[0m %s\n' "$*" >&2; }

if [ ! -d "$WORKDIR/.git" ]; then
  log "cloning $REPO@$REF -> $WORKDIR"
  git clone --depth 1 --branch "$REF" "$REPO" "$WORKDIR"
fi
cd "$WORKDIR"

log "initialising submodules (duckdb, vortex, extension-ci-tools, vcpkg) ..."
git submodule update --init --depth 1 duckdb extension-ci-tools vcpkg vortex

# DuckDB extensions are version-locked. Derive the version from the pinned
# duckdb submodule so the extension matches whatever DuckDB it vendors.
git -C duckdb fetch --tags --depth 1 origin >/dev/null 2>&1 || true
DUCKDB_VERSION="$(git -C duckdb describe --tags 2>/dev/null || true)"
if [ -z "$DUCKDB_VERSION" ]; then
  log "ERROR: could not determine DuckDB version from the submodule (no tags?)."
  exit 1
fi
log "DuckDB version: $DUCKDB_VERSION"

# The Vortex submodule pins the Rust toolchain via rust-toolchain.toml; make sure
# it is installed up front so the build does not stall mid-way.
if command -v rustup >/dev/null 2>&1 && [ -f vortex/rust-toolchain.toml ]; then
  CHANNEL="$(grep -E '^channel' vortex/rust-toolchain.toml | sed -E 's/.*"(.*)".*/\1/')"
  if [ -n "$CHANNEL" ]; then
    log "ensuring rust toolchain $CHANNEL (+rust-src) ..."
    rustup toolchain install "$CHANNEL" --profile minimal --component rust-src >/dev/null 2>&1 || true
  fi
fi

GEN="${GEN:-ninja}"
log "building (make release, GEN=$GEN, DUCKDB_VERSION=$DUCKDB_VERSION) ... this is a large build"
GEN="$GEN" DUCKDB_VERSION="$DUCKDB_VERSION" make release -j "$JOBS" >&2

EXT="$WORKDIR/build/release/extension/vortex/vortex.duckdb_extension"
CLI="$WORKDIR/build/release/duckdb"
if [ ! -f "$EXT" ]; then
  log "ERROR: build finished but $EXT is missing"
  exit 1
fi
log "done."
log "  loadable extension : $EXT"
log "  duckdb CLI (linked): $CLI"
# Machine-readable: stdout is just the extension path.
echo "$EXT"
