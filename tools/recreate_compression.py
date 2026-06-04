#!/usr/bin/env python3
"""Recreate CompressionBenchmark's per-column compression results inside likebench
— now including Vortex, Parquet and the Rust OnPair alongside the ported C++
codecs — and diff the new entries against the ported CompressionBenchmark roster.

  uv run python tools/recreate_compression.py --scale sample
  uv run python tools/recreate_compression.py --dataset duckdb:/data/imdb.duckdb

Outputs (under results/compression/):
  ratio.md / speed.md / diff.md   human-readable tables
  recreation.json                  machine-readable, every (algorithm, column) row

All ratios use ONE baseline — the raw UTF-8 payload bytes of the column — so the
string codecs (which compress those bytes directly) and the columnar formats
(Parquet/Vortex, whose `convert` output we re-divide by the same payload) are
directly comparable.
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harness import convert as convert_mod  # noqa: E402
from harness import datasets, runner  # noqa: E402
from harness.manifest import enabled_binaries  # noqa: E402
from harness.spec import QuerySpec  # noqa: E402

# Display names + provenance. "ported" = the CompressionBenchmark roster running
# natively in likebench (the "old" benchmark); "new" = what likebench adds.
ALGO = {
    "fsst": ("FSST", "ported"),
    "fsst12": ("FSST12", "ported"),
    "dictionary": ("Dictionary", "ported"),
    "lz4": ("LZ4", "ported"),
    "onpair-cpp": ("OnPair (C++)", "ported"),
    "onpair16-cpp": ("OnPair16 (C++)", "ported"),
    "onpairmini10": ("OnPairMini10", "ported"),
    "onpairmini12": ("OnPairMini12", "ported"),
    "onpairmini14": ("OnPairMini14", "ported"),
    "onpair": ("OnPair (Rust)", "new"),
    "onpair16": ("OnPair16 (Rust)", "new"),
    "__vortex__": ("Vortex", "new"),
    "__parquet__": ("Parquet+zstd", "new"),
}
MB = 1_000_000.0
# Each codec binary times this many random single-row decodes for the
# point-access metric (must match RANDOM_ROWS in bench-compress-cpp / bench-onpair).
RANDOM_DECODES = 50_000


@dataclass
class Row:
    algo: str
    provenance: str
    per_col_ratio: dict[str, float] = field(default_factory=dict)
    per_col_comp_mbps: dict[str, float] = field(default_factory=dict)
    per_col_decomp_mbps: dict[str, float] = field(default_factory=dict)
    per_col_rand_mrows: dict[str, float] = field(default_factory=dict)

    def mean_ratio(self) -> float:
        return statistics.fmean(self.per_col_ratio.values()) if self.per_col_ratio else 0.0

    def mean(self, d: dict[str, float]) -> float | None:
        return statistics.fmean(d.values()) if d else None


def payload_bytes(strings_path: Path) -> int:
    """Raw UTF-8 payload byte count from a STRZ `.strings` header."""
    with strings_path.open("rb") as fh:
        head = fh.read(24)
    return struct.unpack("<Q", head[16:24])[0]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", default="clickbench")
    p.add_argument("--scale", default="sample")
    p.add_argument("--columns", nargs="*", default=None)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--measure", type=int, default=3)
    p.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "data" / "cache")
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "results" / "compression")
    args = p.parse_args(argv)

    convert_bin = REPO_ROOT / "target" / "release" / "convert"
    if not convert_bin.exists():
        print(f"error: {convert_bin} not built (run ./setup.sh).", file=sys.stderr)
        return 2

    cols = None if not args.columns else tuple(args.columns)
    rd = datasets.resolve(args.dataset, args.cache_dir, scale=args.scale, seed=0, columns=cols)
    columns = rd.columns
    print(f"== {rd.name}/{args.scale}: {rd.source.rows:,} rows; columns {columns} ==")

    cols_dir = rd.source.parquet_path.parent / "cols"
    inputs, comp_metrics = convert_mod.build_columns(
        source_parquet=rd.source.parquet_path,
        cols_dir=cols_dir,
        convert_bin=convert_bin,
        columns=columns,
    )
    payload = {c: payload_bytes(inputs[c].strings) for c in columns}

    rows: dict[str, Row] = {}

    # --- string codecs (raw format) -----------------------------------------
    codec_bins = [b for b in enabled_binaries(REPO_ROOT / "benchmarks.toml") if "raw" in b.formats]
    # One representative spec per column drives the decode loop; ratios/speeds are
    # codec properties independent of the predicate.
    specs = [QuerySpec.synthetic("contains", c, value="a", label=f"probe_{c}") for c in columns]
    jobs = runner.build_jobs(codec_bins, specs, inputs, formats=("raw",), explain=False)
    print(f"running {len(jobs)} codec measurements ...")
    for job in jobs:
        res = runner.run_job(job, REPO_ROOT, warmup=args.warmup, measured=args.measure)
        codec = res.codec or res.engine
        name, prov = ALGO.get(codec, (codec, "?"))
        row = rows.setdefault(codec, Row(name, prov))
        col = job.spec.column
        pb = payload[col]
        row.per_col_ratio[col] = pb / res.compressed_bytes if res.compressed_bytes else 0.0
        if res.compress_ns:
            row.per_col_comp_mbps[col] = pb / MB / (res.compress_ns / 1e9)
        if res.decompress_ns:
            row.per_col_decomp_mbps[col] = pb / MB / (res.decompress_ns / 1e9)
        if res.decompress_random_ns:
            # Honest point-access rate: random rows decoded per second (the work
            # is RANDOM_DECODES rows, NOT the whole column).
            row.per_col_rand_mrows[col] = RANDOM_DECODES / 1e6 / (res.decompress_random_ns / 1e9)

    # --- columnar formats from `convert` (ratio + compress speed) ------------
    for m in comp_metrics:
        key = "__vortex__" if m.fmt == "vortex" else "__parquet__"
        name, prov = ALGO[key]
        row = rows.setdefault(key, Row(name, prov))
        pb = payload[m.column]
        # Fair, framing-free footprint: Vortex array-tree nbytes / Parquet
        # compressed column-chunk bytes (same basis as the codecs' compressed_bytes).
        comp = m.inmem_compressed_bytes or m.output_bytes
        row.per_col_ratio[m.column] = pb / comp if comp else 0.0
        if m.encode_ns:
            row.per_col_comp_mbps[m.column] = pb / MB / (m.encode_ns / 1e9)

    _write_reports(args.out_dir, columns, rows)
    return 0


def _fmt(v: float | None, nd: int = 2) -> str:
    return f"{v:.{nd}f}" if v is not None else "—"


def _write_reports(out_dir: Path, columns, rows: dict[str, Row]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: r.mean_ratio(), reverse=True)

    # Ratio table
    rt = [
        "# Compression ratio (× vs raw UTF-8 payload)\n",
        "| algorithm | source | " + " | ".join(columns) + " | **mean** |",
        "|" + "---|" * (len(columns) + 3),
    ]
    for r in ordered:
        cells = " | ".join(_fmt(r.per_col_ratio.get(c)) for c in columns)
        rt.append(f"| {r.algo} | {r.provenance} | {cells} | **{r.mean_ratio():.2f}** |")
    (out_dir / "ratio.md").write_text("\n".join(rt) + "\n")

    # Speed table (mean across columns)
    st = [
        "# Throughput, mean across columns. compress/decompress = MB/s of uncompressed "
        "payload; random = M random single-row decodes/s.\n",
        "| algorithm | source | compress MB/s | decompress MB/s | random Mrows/s |",
        "|---|---|---|---|---|",
    ]
    for r in sorted(rows.values(), key=lambda r: r.mean(r.per_col_comp_mbps) or 0, reverse=True):
        st.append(
            f"| {r.algo} | {r.provenance} | {_fmt(r.mean(r.per_col_comp_mbps), 1)} | "
            f"{_fmt(r.mean(r.per_col_decomp_mbps), 1)} | {_fmt(r.mean(r.per_col_rand_mrows), 2)} |"
        )
    (out_dir / "speed.md").write_text("\n".join(st) + "\n")

    # Diff: new entries vs the best ported (CompressionBenchmark) algorithm.
    ported = [r for r in rows.values() if r.provenance == "ported"]
    new = [r for r in rows.values() if r.provenance == "new"]
    best_ported = max(ported, key=lambda r: r.mean_ratio()) if ported else None
    dt = ["# Diff — new entries vs the ported CompressionBenchmark roster\n"]
    if best_ported:
        dt.append(
            f"Best ported algorithm by mean ratio: **{best_ported.algo}** "
            f"({best_ported.mean_ratio():.2f}×).\n"
        )
        dt.append("| new algorithm | mean ratio | Δ vs best-ported | compress MB/s |")
        dt.append("|---|---|---|---|")
        for r in sorted(new, key=lambda r: r.mean_ratio(), reverse=True):
            delta = r.mean_ratio() - best_ported.mean_ratio()
            dt.append(
                f"| {r.algo} | {r.mean_ratio():.2f}× | {delta:+.2f}× | "
                f"{_fmt(r.mean(r.per_col_comp_mbps), 1)} |"
            )
    (out_dir / "diff.md").write_text("\n".join(dt) + "\n")

    (out_dir / "recreation.json").write_text(
        json.dumps(
            {
                r.algo: {
                    "source": r.provenance,
                    "ratio": r.per_col_ratio,
                    "compress_mbps": r.per_col_comp_mbps,
                    "decompress_mbps": r.per_col_decomp_mbps,
                    "random_mrows": r.per_col_rand_mrows,
                }
                for r in rows.values()
            },
            indent=2,
        )
    )

    for f in ("ratio.md", "speed.md", "diff.md"):
        print(f"\n===== {f} =====")
        print((out_dir / f).read_text())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
