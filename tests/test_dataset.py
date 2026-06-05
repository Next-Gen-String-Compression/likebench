"""Dataset-resolution tests: string-column discovery and selector validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import datasets
from harness.source import utf8_columns


def test_utf8_column_discovery(tmp_path, make_parquet):
    """String columns are auto-discovered; the integer column is excluded."""
    parquet = make_parquet(tmp_path / "data.parquet")
    assert set(utf8_columns(parquet)) == {"URL", "Title"}  # WatchID (int) excluded


def test_resolve_rejects_unknown_dataset(tmp_path):
    with pytest.raises(ValueError):
        datasets.resolve(
            "clickbench",
            tmp_path,
            scale="nano",
            seed=0,
            columns=None,
            repo_root=Path("."),
        )
