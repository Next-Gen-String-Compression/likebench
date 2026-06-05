"""Predicate-mining tests: deterministic, offline, over a generated parquet."""

from __future__ import annotations

from harness import mine_queries


def test_generation_and_mining(tmp_path, make_parquet):
    parquet = make_parquet(tmp_path / "data.parquet")

    specs = mine_queries.mine(parquet, ("URL", "Title"), seed=123)
    assert specs, "miner produced no specs"
    ops = {s.op for s in specs if s.kind == "synthetic"}
    assert {"prefix", "contains"} <= ops

    # Determinism: same seed -> identical specs.
    specs2 = mine_queries.mine(parquet, ("URL", "Title"), seed=123)
    assert [s.raw for s in specs] == [s.raw for s in specs2]

    # Every synthetic spec records a measured selectivity in [0, 1].
    for s in specs:
        if s.kind == "synthetic":
            assert s.selectivity is None or 0.0 <= s.selectivity <= 1.0
