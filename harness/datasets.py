"""Dataset resolution: turn a ``--dataset`` selector into a source Parquet plus
the string columns to benchmark.

ClickBench stays the default and is unchanged. On top of it this module adds the
CompressionBenchmark-style ability to point likebench at *arbitrary* string data
and discover its string columns automatically (the analogue of
``GetBenchmarkFromDatabase`` scanning ``information_schema`` for VARCHAR columns).

Selectors
---------
* ``clickbench``            — the built-in ClickBench ``hits`` source (default).
* ``parquet:<path|url>``    — any Parquet file; string columns auto-discovered
                              unless ``--columns`` is given. ``--scale`` head-caps
                              rows (nano/sample) for a quick local run.
* ``duckdb:<path>[#table]`` — a DuckDB database; VARCHAR columns of ``table``
                              (or the first table) are exported to Parquet.
* ``hf:<repo>[#file]``      — a HuggingFace dataset Parquet (see ``hf_scrape``).

The miner, converter and runner already take ``columns`` as a parameter, so once
a dataset resolves to ``(parquet_path, columns)`` the rest of the pipeline is
dataset-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness import data as data_mod
from harness.data import SourceData, _download, _row_count
from harness.spec import STRING_COLUMNS

# Scales that head-cap a generic source for a quick run. ``full`` uses everything.
_SCALE_ROW_CAP = {"nano": 50_000, "sample": 1_000_000}

# Convenience aliases for datasets CompressionBenchmark used. TPC-H / Kaggle are
# user-supplied DuckDB files (use ``duckdb:<path>`` directly — that needs no
# auth); the ones below resolve to public, auth-free HuggingFace Parquet.
ALIASES = {
    "imdb": "hf:stanfordnlp/imdb#train",
}


@dataclass(frozen=True)
class ResolvedDataset:
    name: str
    source: SourceData
    columns: tuple[str, ...]


def utf8_columns(parquet: Path) -> tuple[str, ...]:
    """Every Utf8/String column in a Parquet file (likebench's VARCHAR scan)."""
    import polars as pl

    schema = pl.scan_parquet(parquet).collect_schema()
    return tuple(
        name for name, dtype in schema.items() if dtype in (pl.Utf8, pl.String, pl.Categorical)
    )


def resolve(
    selector: str,
    cache_dir: Path,
    *,
    scale: str,
    seed: int,
    columns: tuple[str, ...] | None,
) -> ResolvedDataset:
    """Resolve a ``--dataset`` selector to a source + the columns to benchmark.

    ``columns`` of ``None`` means *auto-discover all string columns*.
    """
    if selector == "clickbench":
        source = data_mod.prepare(cache_dir, scale, seed=seed)
        return ResolvedDataset("clickbench", source, columns or STRING_COLUMNS)

    selector = ALIASES.get(selector, selector)
    kind, _, arg = selector.partition(":")
    if not arg:
        raise ValueError(
            f"unknown --dataset {selector!r}; expected 'clickbench', 'parquet:<path>', "
            f"'duckdb:<path>' or 'hf:<repo>'"
        )

    if kind == "parquet":
        source = _prepare_parquet(arg, cache_dir, scale=scale)
    elif kind == "duckdb":
        from harness import duckdb_source

        source = duckdb_source.prepare(arg, cache_dir, scale=scale, default_columns=columns)
    elif kind == "hf":
        from harness import hf_scrape

        source = hf_scrape.prepare(arg, cache_dir, scale=scale)
    else:
        raise ValueError(f"unknown --dataset kind {kind!r} in {selector!r}")

    cols = columns or utf8_columns(source.parquet_path)
    if not cols:
        raise ValueError(f"dataset {selector!r} has no string columns to benchmark")
    return ResolvedDataset(selector, source, cols)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")[:64] or "dataset"


def _prepare_parquet(path_or_url: str, cache_dir: Path, *, scale: str) -> SourceData:
    """Cache a generic Parquet (local path or http(s) URL) under the cache dir,
    head-capping rows for the small scales so a quick run stays cheap."""
    import polars as pl

    out_dir = cache_dir / "parquet" / _slug(path_or_url) / scale
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet = out_dir / "data.parquet"
    if not parquet.exists():
        if path_or_url.startswith(("http://", "https://")):
            tmp = out_dir / "_download.parquet"
            _download(path_or_url, tmp)
            src_path: Path = tmp
        else:
            src_path = Path(path_or_url).expanduser()
            if not src_path.exists():
                raise FileNotFoundError(f"parquet dataset not found: {src_path}")
        cap = _SCALE_ROW_CAP.get(scale)
        lf = pl.scan_parquet(src_path)
        if cap is not None:
            lf = lf.head(cap)
        lf.sink_parquet(parquet, compression="zstd")
        if path_or_url.startswith(("http://", "https://")):
            (out_dir / "_download.parquet").unlink(missing_ok=True)

    return SourceData(parquet, scale, _row_count(parquet), utf8_columns(parquet))
