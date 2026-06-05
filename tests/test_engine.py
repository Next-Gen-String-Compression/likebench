"""Engine-manifest test: the repo's benchmarks.toml parses and declares the
DataFusion engine with the expected matrix cells."""

from __future__ import annotations

from pathlib import Path

from harness.manifest import load_manifest

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_loads_repo_file():
    bins = load_manifest(ROOT / "benchmarks.toml")
    names = {b.name for b in bins}
    assert "bench-datafusion" in names
    df = next(b for b in bins if b.name == "bench-datafusion")
    assert df.supports(mode="in-mem", fmt="parquet", kind="synthetic")
    assert df.supports(mode="full-query", fmt="vortex", kind="real")
    assert not df.supports(mode="in-mem", fmt="raw", kind="synthetic")
