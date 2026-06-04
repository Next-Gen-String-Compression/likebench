#!/usr/bin/env python3
"""A reference engine that honours the uniform binary contract.

It is *not* an optimized engine; it exists so the Python harness can be tested
end-to-end (subprocess plumbing, JSON parsing, warmup discard, aggregation,
cross-engine correctness) without the heavy Rust builds. Two invocations of this
script compute identical results, so the correctness gate is exercised for real.

It also documents, in the clearest possible terms, exactly what a conforming
binary must emit.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time


def fnv1a64(value: int) -> str:
    """Stable checksum over the 8-byte LE encoding of an integer.

    Must match the Rust `bench_core::checksum` implementation so checksums agree
    across engines.
    """
    h = 0xCBF29CE484222325
    for b in value.to_bytes(8, "little"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"0x{h:016x}"


def like_to_predicate(sql: str) -> tuple[str, str]:
    """Extract (column, substring) from a simple `... col LIKE '%x%'` query."""
    m = re.search(r'"?(\w+)"?\s+LIKE\s+\'%(.*?)%\'', sql, re.IGNORECASE)
    if not m:
        raise ValueError(f"unsupported real SQL: {sql}")
    return m.group(1), m.group(2)


def compute(spec: dict, input_path: str) -> tuple[int, int, str]:
    import polars as pl

    if spec["kind"] == "real":
        col, token = like_to_predicate(spec["sql"])
        op = "contains"
        values = [token]
        mode = "all"
        predicate = None
    else:
        col = spec["column"]
        op = spec["op"]
        values = spec.get("values") or ([spec["value"]] if "value" in spec else [])
        mode = spec.get("mode", "all")
        predicate = spec.get("predicate")

    s = pl.scan_parquet(input_path).select(pl.col(col).cast(pl.Utf8)).collect().to_series()
    rows = s.len()

    def mask_for(kind: str, val: str):
        if kind in ("contains", "multicontains"):
            return s.str.contains(val, literal=True)
        if kind == "prefix":
            return s.str.starts_with(val)
        if kind == "suffix":
            return s.str.ends_with(val)
        raise ValueError(kind)

    if op == "expr":
        # predicate is {"and":[{prefix:..},{contains:..}]} (or "or")
        ((boolop, terms),) = predicate.items()
        masks = []
        for term in terms:
            ((k, v),) = term.items()
            masks.append(mask_for(k, v))
        acc = masks[0]
        for m in masks[1:]:
            acc = acc & m if boolop == "and" else acc | m
        match = acc
    elif op == "multicontains":
        masks = [mask_for("contains", v) for v in values]
        acc = masks[0]
        for m in masks[1:]:
            acc = acc & m if mode == "all" else acc | m
        match = acc
    else:
        match = mask_for(op, values[0])

    result_rows = int(match.fill_null(False).sum())
    return rows, result_rows, fnv1a64(result_rows)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--mode", required=True)
    ap.add_argument("--query-spec", required=True)
    ap.add_argument("--iterations", type=int, required=True)
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--output", default="json")
    ap.add_argument("--engine-name", default="fake")  # test hook
    args = ap.parse_args(argv)

    spec_text = sys.stdin.read() if args.query_spec == "-" else args.query_spec
    spec = json.loads(spec_text)

    t0 = time.perf_counter_ns()
    rows, result_rows, checksum = compute(spec, args.input)
    load_ns = time.perf_counter_ns() - t0

    # Fake but plausible per-iteration timings (deterministic).
    base = 1000 + result_rows % 500
    iters = [base + (i * 7) % 50 for i in range(args.iterations)]

    pushdown = args.format == "vortex"  # pretend vortex pushes down
    out = {
        "engine": args.engine_name,
        "format": args.format,
        "mode": args.mode,
        "label": spec.get("label", ""),
        "op": spec.get("op"),
        "column": spec.get("column"),
        "rows": rows,
        "result_rows": result_rows,
        "result_checksum": checksum,
        "file_bytes": 0,
        "in_memory_bytes": rows * 32,
        "load_ns": load_ns,
        "decompress_ns": 0 if args.format == "vortex" else load_ns // 2,
        "pushdown": pushdown,
        "plan": "FakeScan[pushdown]" if pushdown else "FakeScan",
        "iters_ns": iters,
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
