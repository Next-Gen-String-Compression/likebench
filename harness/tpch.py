"""TPC-H dataset provider — the base, offline, deterministic source.

Generates one string-heavy TPC-H table to Parquet by invoking the `tpch-gen`
binary (pure-Rust `tpchgen-arrow`). No network, byte-for-byte `dbgen`-compatible,
and cached per (table, scale). `lineitem` is the default: its `l_comment` column
is free-text (great for `contains`), alongside low-cardinality codes
(`l_shipmode`, `l_shipinstruct`, …) that exercise dictionary-friendly paths.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.source import SourceData, utf8_columns

# Scale names → TPC-H scale factor. nano is tiny for CI/smoke; full is SF=1.
SCALES: dict[str, float] = {"nano": 0.01, "small": 0.1, "full": 1.0}
DEFAULT_SEED = 0  # TPC-H is deterministic; kept for CLI symmetry.
DEFAULT_TABLE = "lineitem"


def _tpch_gen_bin(repo_root: Path) -> Path:
    for profile in ("release", "debug"):
        cand = repo_root / "target" / profile / "tpch-gen"
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "tpch-gen binary not built. Run ./setup.sh (or `cargo build --release -p tpch-gen`)."
    )


def prepare(
    cache_dir: Path,
    scale: str,
    *,
    repo_root: Path,
    table: str = DEFAULT_TABLE,
) -> SourceData:
    """Ensure the TPC-H ``table`` at ``scale`` exists as Parquet; return its source."""
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose one of {tuple(SCALES)}")

    out_dir = cache_dir / "tpch" / table / scale
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet = out_dir / "data.parquet"
    meta = out_dir / "data.meta.json"

    if parquet.exists() and meta.exists():
        info = json.loads(meta.read_text())
        return SourceData(parquet, scale, int(info["rows"]), utf8_columns(parquet))

    proc = subprocess.run(
        [
            str(_tpch_gen_bin(repo_root)),
            "--table",
            table,
            "--scale",
            str(SCALES[scale]),
            "--output",
            str(parquet),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tpch-gen failed ({proc.returncode}):\n{proc.stderr}")
    rows = int(json.loads(proc.stdout.strip().splitlines()[-1])["rows"])
    meta.write_text(json.dumps({"rows": rows, "scale": scale, "table": table}, indent=2))
    return SourceData(parquet, scale, rows, utf8_columns(parquet))
