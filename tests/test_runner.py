"""Runner tests: aggregation, the correctness gate, group labelling, and a real
end-to-end matrix driving two conforming (fake) engines."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from harness import runner
from harness.convert import ColumnInputs
from harness.manifest import Binary
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
    assert s.mean_ns == pytest.approx(6.5)


def test_aggregate_handles_warmup_ge_len():
    assert runner.aggregate([42], warmup=3).min_ns == 42


# --------------------------------------------------------------------------- #
# Correctness gate + group labels
# --------------------------------------------------------------------------- #
def _entry(engine, fmt, rows, cksum):
    return runner.RunEntry(
        engine=engine,
        format=fmt,
        mode="in-mem",
        kind="synthetic",
        op="contains",
        column="URL",
        label="contains_URL",
        query_identity="id1",
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
    assert (
        runner.check_correctness(
            [_entry("a", "parquet", 7, "0x7"), _entry("b", "vortex", 7, "0x7")]
        )
        == []
    )


def test_correctness_fails_loudly_on_mismatch():
    es = [_entry("a", "parquet", 7, "0x7"), _entry("b", "vortex", 9, "0x9")]
    with pytest.raises(runner.CorrectnessError):
        runner.check_correctness(es)
    warns = runner.check_correctness(es, allow_mismatch=True)
    assert warns and "MISMATCH" in warns[0]


def test_group_labels():
    assert runner._group_of("parquet", False) == "baseline"
    assert runner._group_of("vortex", True) == "treatment"
    assert runner._group_of("vortex", False) == "control"
    assert runner._group_of("raw", False) == "codec"


# --------------------------------------------------------------------------- #
# End-to-end matrix over two fake engines
# --------------------------------------------------------------------------- #
def _install_fake(repo_root: Path, name: str) -> None:
    bindir = repo_root / "target" / "release"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / name
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@" --engine-name {name}\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)


def test_end_to_end_matrix_two_engines(tmp_path, make_parquet):
    parquet = make_parquet(tmp_path / "cache" / "data.parquet")
    inputs = {
        "URL": ColumnInputs(parquet=parquet, vortex=parquet),
        "Title": ColumnInputs(parquet=parquet, vortex=parquet),
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
    ]
    jobs = runner.build_jobs([arrow, df], specs, inputs)
    assert jobs
    entries, warnings = runner.run_matrix(jobs, repo_root, warmup=1, measured=3, progress=False)
    assert warnings == []  # engines agree

    contains = [e for e in entries if e.label == "contains_URL_p01"]
    assert len({e.result_rows for e in contains}) == 1
    assert {"bench-arrow", "bench-datafusion"} <= {e.engine for e in contains}
    groups = {(e.format, e.group) for e in entries}
    assert ("vortex", "treatment") in groups
    assert ("parquet", "baseline") in groups

    runner.write_results_json(
        tmp_path / "results.json", entries=entries, compression=[], meta={}, warnings=warnings
    )
    runner.write_tables(tmp_path / "t.md", tmp_path / "t.csv", entries)
    assert (tmp_path / "results.json").exists()
