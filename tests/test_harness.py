"""Harness tests: aggregation, correctness gate, mining, and a real end-to-end
run driving two conforming (fake) engines over generated nano data."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from harness import data as data_mod
from harness import mine_queries, runner
from harness.convert import ColumnInputs
from harness.manifest import Binary, load_manifest
from harness.spec import QuerySpec

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake_engine.py"


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_discards_warmup_and_computes_percentiles():
    iters = [10_000, 5, 6, 7, 8]  # first is warmup (slow)
    s = runner.aggregate(iters, warmup=1)
    assert s.min_ns == 5
    assert s.median_ns == 6.5
    assert s.p95_ns == 8  # nearest-rank over [5,6,7,8]
    assert s.p99_ns == 8
    assert s.mean_ns == pytest.approx(6.5)


def test_aggregate_handles_warmup_ge_len():
    s = runner.aggregate([42], warmup=3)
    assert s.min_ns == 42


# --------------------------------------------------------------------------- #
# Correctness gate
# --------------------------------------------------------------------------- #
def _entry(engine, fmt, mode, ident, rows, cksum):
    return runner.RunEntry(
        engine=engine,
        format=fmt,
        mode=mode,
        kind="synthetic",
        op="contains",
        column="URL",
        label="contains_URL",
        query_identity=ident,
        selectivity_bucket="p01",
        rows=100,
        result_rows=rows,
        result_checksum=cksum,
        selectivity=rows / 100,
        file_bytes=0,
        in_memory_bytes=0,
        load_ns=0,
        decompress_ns=0,
        pushdown=(fmt == "vortex"),
        plan="",
        group=runner._group_of(fmt, fmt == "vortex"),
        warmup=1,
        measured=2,
        stats=runner.Stats(1, 1, 1, 1, 1, 1, 0),
        throughput_rows_per_s=1.0,
        iters_ns=[1, 1],
    )


def test_correctness_passes_when_agree():
    es = [
        _entry("a", "parquet", "in-mem", "id1", 7, "0x7"),
        _entry("b", "vortex", "in-mem", "id1", 7, "0x7"),
    ]
    assert runner.check_correctness(es) == []


def test_correctness_fails_loudly_on_mismatch():
    es = [
        _entry("a", "parquet", "in-mem", "id1", 7, "0x7"),
        _entry("b", "vortex", "in-mem", "id1", 9, "0x9"),
    ]
    with pytest.raises(runner.CorrectnessError):
        runner.check_correctness(es)
    # allow_mismatch downgrades to a warning list
    warns = runner.check_correctness(es, allow_mismatch=True)
    assert warns and "MISMATCH" in warns[0]


def test_group_labels():
    assert runner._group_of("parquet", False) == "baseline"
    assert runner._group_of("vortex", True) == "treatment"
    assert runner._group_of("vortex", False) == "control"


# --------------------------------------------------------------------------- #
# Spec identity
# --------------------------------------------------------------------------- #
def test_spec_identity_ignores_cosmetic_fields():
    a = QuerySpec.from_dict(
        {"kind": "synthetic", "op": "contains", "column": "URL", "value": "g", "label": "x"}
    )
    b = QuerySpec.from_dict(
        {
            "kind": "synthetic",
            "op": "contains",
            "column": "URL",
            "value": "g",
            "label": "y",
            "selectivity": 0.1,
            "selectivity_bucket": "p10",
        }
    )
    assert a.identity() == b.identity()
    c = QuerySpec.from_dict({"kind": "synthetic", "op": "contains", "column": "URL", "value": "h"})
    assert a.identity() != c.identity()


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def test_manifest_loads_repo_file():
    bins = load_manifest(ROOT / "benchmarks.toml")
    names = {b.name for b in bins}
    assert {"bench-arrow", "bench-datafusion", "bench-duckdb"} <= names
    arrow = next(b for b in bins if b.name == "bench-arrow")
    assert arrow.supports(mode="in-mem", fmt="parquet", kind="synthetic")
    assert not arrow.supports(mode="full-query", fmt="vortex", kind="real")


def test_manifest_rejects_bad_values(tmp_path):
    bad = tmp_path / "b.toml"
    bad.write_text(
        '[[binary]]\nname="x"\nbin_path="p"\nlang="rust"\n'
        'modes=["warp"]\nformats=["parquet"]\nkinds=["synthetic"]\n'
    )
    with pytest.raises(ValueError):
        load_manifest(bad)


# --------------------------------------------------------------------------- #
# Data generation + mining (deterministic, offline)
# --------------------------------------------------------------------------- #
def test_nano_generation_and_mining(tmp_path):
    src = data_mod.prepare(tmp_path, "nano", seed=123)
    assert src.parquet_path.exists()
    assert src.rows == 50_000

    specs = mine_queries.mine(src.parquet_path, ("URL", "Title"), seed=123)
    assert specs, "miner produced no specs"
    ops = {s.op for s in specs if s.kind == "synthetic"}
    assert {"prefix", "contains"} <= ops
    # Determinism: same seed -> identical specs.
    specs2 = mine_queries.mine(src.parquet_path, ("URL", "Title"), seed=123)
    assert [s.raw for s in specs] == [s.raw for s in specs2]
    # Every synthetic spec records a measured selectivity in (0, 1].
    for s in specs:
        if s.kind == "synthetic":
            assert s.selectivity is None or 0.0 <= s.selectivity <= 1.0


# --------------------------------------------------------------------------- #
# End-to-end: two fake engines over nano data through the full runner.
# --------------------------------------------------------------------------- #
def _install_fake(repo_root: Path, name: str) -> None:
    """Drop a target/release/<name> wrapper that execs the fake engine."""
    bindir = repo_root / "target" / "release"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@" --engine-name {name}\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


def test_end_to_end_two_engines(tmp_path):
    # Generate data and point both formats at the same nano parquet (the fake
    # engine computes from the parquet regardless of --format).
    src = data_mod.prepare(tmp_path / "cache", "nano", seed=7)
    inputs = {
        "URL": ColumnInputs(parquet=src.parquet_path, vortex=src.parquet_path),
        "Title": ColumnInputs(parquet=src.parquet_path, vortex=src.parquet_path),
    }

    repo_root = tmp_path / "repo"
    _install_fake(repo_root, "bench-arrow")
    _install_fake(repo_root, "bench-datafusion")

    arrow = Binary(
        "bench-arrow", "crates/bench-arrow", "rust", ("in-mem",), ("parquet",), ("synthetic",)
    )
    df = Binary(
        "bench-datafusion",
        "crates/bench-datafusion",
        "rust",
        ("in-mem", "full-query"),
        ("parquet", "vortex"),
        ("synthetic", "real"),
    )

    specs = [
        QuerySpec.synthetic("contains", "URL", value="google", label="contains_URL_p01"),
        QuerySpec.synthetic("prefix", "URL", value="http://", label="prefix_URL"),
        QuerySpec.real(
            "SELECT count(*) FROM hits WHERE \"URL\" LIKE '%google%'",
            column="URL",
            label="clickbench_like_URL",
        ),
    ]

    jobs = runner.build_jobs([arrow, df], specs, inputs)
    assert jobs, "no jobs built"
    entries, warnings = runner.run_matrix(jobs, repo_root, warmup=1, measured=3, progress=False)
    assert warnings == []  # engines agree
    assert entries

    # Both engines ran the synthetic contains on URL and must agree.
    contains = [e for e in entries if e.label == "contains_URL_p01"]
    assert len({e.result_rows for e in contains}) == 1
    assert {"bench-arrow", "bench-datafusion"} <= {e.engine for e in contains}

    # Pushdown labelling: vortex -> treatment, parquet -> baseline.
    groups = {(e.format, e.group) for e in entries}
    assert ("vortex", "treatment") in groups
    assert ("parquet", "baseline") in groups

    # Tables + json write without error.
    runner.write_results_json(
        tmp_path / "results.json", entries=entries, compression=[], meta={}, warnings=warnings
    )
    runner.write_tables(tmp_path / "t.md", tmp_path / "t.csv", entries)
    assert (tmp_path / "results.json").exists()
    assert (tmp_path / "t.csv").exists()
