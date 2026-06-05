#!/usr/bin/env python3
"""likebench entrypoint: generate -> convert -> mine -> run -> outputs.

python run.py                      # default: TPC-H nano, all enabled engines
python run.py --scale small        # SF=0.1
python run.py --dataset tpch:orders --columns auto
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import subprocess
import sys
from pathlib import Path

from harness import convert as convert_mod
from harness import datasets, mine_queries, plots, runner, tpch
from harness import source as source_mod
from harness.manifest import enabled_binaries

REPO_ROOT = Path(__file__).resolve().parent


def _requested_columns(columns: list[str] | None) -> tuple[str, ...] | None:
    """``None``/``['auto']`` means auto-discover; otherwise the explicit list."""
    if not columns or (len(columns) == 1 and columns[0] == "auto"):
        return None
    return tuple(columns)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="likebench: LIKE-pushdown on compressed columnar strings"
    )
    p.add_argument("--scale", choices=tpch.SCALES, default="nano")
    p.add_argument(
        "--dataset",
        default="tpch",
        help="tpch | tpch:<table> (table: lineitem|orders|customer|part|supplier). "
        "String columns are auto-discovered unless --columns is given.",
    )
    p.add_argument("--engines", nargs="*", default=None, help="subset of manifest names")
    p.add_argument("--modes", nargs="*", default=None, choices=["in-mem", "full-query"])
    p.add_argument("--formats", nargs="*", default=None, choices=["parquet", "vortex", "raw"])
    p.add_argument(
        "--columns",
        nargs="*",
        default=None,
        help="string columns to benchmark; omit (or 'auto') to discover them all.",
    )
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--measure", type=int, default=10)
    p.add_argument("--seed", type=int, default=tpch.DEFAULT_SEED)
    p.add_argument("--max-per-op", type=int, default=None)
    p.add_argument("--no-explain", action="store_true")
    p.add_argument("--no-plots", action="store_true")
    p.add_argument("--allow-mismatch", action="store_true", help="warn instead of failing")
    p.add_argument("--reuse-queries", action="store_true", help="reuse the mined queries file")
    p.add_argument("--cache-dir", type=Path, default=REPO_ROOT / "data" / "cache")
    p.add_argument("--results-dir", type=Path, default=REPO_ROOT / "results")
    return p.parse_args(argv)


def _cmd(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # pragma: no cover
        return ""


def _crate_versions(names: set[str]) -> dict[str, str]:
    lock = REPO_ROOT / "Cargo.lock"
    if not lock.exists():
        return {}
    import tomllib

    doc = tomllib.loads(lock.read_text())
    return {p["name"]: p["version"] for p in doc.get("package", []) if p["name"] in names}


def _machine_meta() -> dict:
    mem_kb = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
                break
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": __import__("os").cpu_count(),
        "mem_total_gb": round(mem_kb / 1024 / 1024, 1) if mem_kb else None,
        "python": platform.python_version(),
    }


def build_metadata(args: argparse.Namespace, source: source_mod.SourceData) -> dict:
    crate_names = {
        "vortex",
        "vortex-array",
        "vortex-datafusion",
        "vortex-file",
        "datafusion",
        "arrow",
        "parquet",
    }
    return {
        "tool": "likebench",
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scale": args.scale,
        "rows": source.rows,
        "seed": args.seed,
        "warmup": args.warmup,
        "measured": args.measure,
        "columns": args.columns,
        "git_commit": _cmd(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "dataset_hash": source_mod.dataset_hash(source.parquet_path),
        "machine": _machine_meta(),
        "versions": {
            "rustc": _cmd(["rustc", "--version"]),
            "cargo": _cmd(["cargo", "--version"]),
            "crates": _crate_versions(crate_names),
        },
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    requested_columns = _requested_columns(args.columns)
    results_dir = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    binaries = enabled_binaries(REPO_ROOT / "benchmarks.toml")
    if args.engines:
        wanted = set(args.engines)
        binaries = [b for b in binaries if b.name in wanted]
        missing = wanted - {b.name for b in binaries}
        if missing:
            print(f"warning: requested engines not in manifest/enabled: {missing}", file=sys.stderr)
    if not binaries:
        print("error: no engines selected/enabled.", file=sys.stderr)
        return 2

    convert_bin = REPO_ROOT / "target" / "release" / "convert"
    if not convert_bin.exists():
        print(f"error: {convert_bin} not built. Run ./setup.sh first.", file=sys.stderr)
        return 2

    print(f"== dataset: {args.dataset}/{args.scale} ==")
    resolved = datasets.resolve(
        args.dataset,
        args.cache_dir,
        scale=args.scale,
        seed=args.seed,
        columns=requested_columns,
        repo_root=REPO_ROOT,
    )
    source = resolved.source
    columns = resolved.columns
    args.columns = list(columns)  # so build_metadata records the resolved columns
    print(f"   {source.parquet_path}  ({source.rows:,} rows)")
    print(f"   string columns: {', '.join(columns)}")

    print("== convert: per-column Parquet + Vortex ==")
    cols_dir = source.parquet_path.parent / "cols"
    inputs, comp_metrics = convert_mod.build_columns(
        source_parquet=source.parquet_path,
        cols_dir=cols_dir,
        convert_bin=convert_bin,
        columns=columns,
    )

    queries_path = REPO_ROOT / "queries" / "clickbench.json"
    if args.reuse_queries and queries_path.exists():
        print(f"== queries: reusing {queries_path} ==")
        specs = mine_queries.load_queries(queries_path)
    else:
        print("== queries: mining predicates by selectivity bucket ==")
        specs = mine_queries.mine(
            source.parquet_path, columns, seed=args.seed, max_per_op=args.max_per_op
        )
        mine_queries.write_queries(specs, queries_path)
    print(f"   {len(specs)} query specs -> {queries_path}")

    print("== run matrix ==")
    jobs = runner.build_jobs(
        binaries,
        specs,
        inputs,
        modes=tuple(args.modes) if args.modes else None,
        formats=tuple(args.formats) if args.formats else None,
        explain=not args.no_explain,
    )
    if not jobs:
        print("error: no jobs to run (check engine/mode/format filters).", file=sys.stderr)
        return 2
    entries, warnings = runner.run_matrix(
        jobs,
        REPO_ROOT,
        warmup=args.warmup,
        measured=args.measure,
        allow_mismatch=args.allow_mismatch,
    )

    meta = build_metadata(args, source)
    runner.write_results_json(
        results_dir / "results.json",
        entries=entries,
        compression=[m.as_dict() for m in comp_metrics],
        meta=meta,
        warnings=warnings,
    )
    runner.write_tables(results_dir / "table.md", results_dir / "table.csv", entries)
    (results_dir / "metadata.json").write_text(json.dumps(meta, indent=2))

    if not args.no_plots:
        print("== plots ==")
        produced = plots.render_all(results_dir / "results.json", results_dir / "plots")
        for p in produced:
            print(f"   {p}")

    print("\n== summary ==")
    print(f"   runs: {len(entries)}   queries: {len(specs)}   engines: {len(binaries)}")
    treat = sum(1 for e in entries if e.group == "treatment")
    ctrl = sum(1 for e in entries if e.group == "control")
    base = sum(1 for e in entries if e.group == "baseline")
    print(f"   treatment(pushdown): {treat}   control(decode): {ctrl}   baseline(parquet): {base}")
    if warnings:
        print(f"   correctness warnings: {len(warnings)} (allow-mismatch)")
    print(f"   results -> {results_dir}/results.json, table.md, table.csv, plots/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
