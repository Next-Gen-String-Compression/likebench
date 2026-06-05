"""Dataset resolution: turn a ``--dataset`` selector into a source Parquet plus
the string columns to benchmark.

The base ships a single provider — TPC-H (offline, generated). Later layers add
ClickBench, generic Parquet, DuckDB and HuggingFace sources behind the same
``resolve()`` seam. ``columns=None`` means *auto-discover every string column*.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness import tpch
from harness.source import SourceData, utf8_columns


@dataclass(frozen=True)
class ResolvedDataset:
    name: str
    source: SourceData
    columns: tuple[str, ...]


def resolve(
    selector: str,
    cache_dir: Path,
    *,
    scale: str,
    seed: int,
    columns: tuple[str, ...] | None,
    repo_root: Path,
) -> ResolvedDataset:
    """Resolve a ``--dataset`` selector to a source + the columns to benchmark."""
    name, _, arg = selector.partition(":")
    if name == "tpch":
        source = tpch.prepare(
            cache_dir, scale, repo_root=repo_root, table=arg or tpch.DEFAULT_TABLE
        )
    else:
        raise ValueError(f"unknown --dataset {selector!r} (base supports 'tpch' or 'tpch:<table>')")

    cols = columns or utf8_columns(source.parquet_path)
    if not cols:
        raise ValueError(f"dataset {selector!r} has no string columns to benchmark")
    return ResolvedDataset(name, source, cols)
