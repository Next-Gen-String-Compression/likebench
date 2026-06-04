"""DuckDB dataset source — likebench's port of CompressionBenchmark's
``GetBenchmarkFromDatabase``.

Given a DuckDB database file, discover its VARCHAR columns (optionally scoped to
one table) and export them to a Parquet file the rest of the pipeline consumes.
This lets likebench run on the exact databases CompressionBenchmark targeted
(IMDB, TPC-H, Kaggle, …) instead of only ClickBench.

Selector syntax (after the ``duckdb:`` prefix): ``<path>[#table]``, where
``table`` may be ``schema.table`` or just ``table``. Without a table we pick the
table with the most VARCHAR columns.

``duckdb`` is an optional dependency; install it with ``uv add duckdb`` (or
``pip install duckdb``) to use this source.
"""

from __future__ import annotations

from pathlib import Path

from harness.data import SourceData, _row_count

# Head-cap rows for the quick scales (matches datasets._SCALE_ROW_CAP).
_SCALE_ROW_CAP = {"nano": 50_000, "sample": 1_000_000}


def _import_duckdb():
    try:
        import duckdb  # noqa: F401

        return duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "the 'duckdb' dataset source requires the duckdb package; "
            "install it with `uv add duckdb` (or `pip install duckdb`)."
        ) from exc


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")[:64] or "duckdb"


def prepare(
    arg: str,
    cache_dir: Path,
    *,
    scale: str,
    default_columns: tuple[str, ...] | None = None,
) -> SourceData:
    duckdb = _import_duckdb()

    db_path, _, table_filter = arg.partition("#")
    db_path = db_path.strip()
    if not Path(db_path).expanduser().exists():
        raise FileNotFoundError(f"duckdb database not found: {db_path}")

    out_dir = cache_dir / "duckdb" / _slug(arg) / scale
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet = out_dir / "data.parquet"

    con = duckdb.connect(str(Path(db_path).expanduser()), read_only=True)
    try:
        table, columns = _choose_table_and_columns(con, table_filter, default_columns)
        if not parquet.exists():
            quoted = ", ".join(f'"{c}"' for c in columns)
            cap = _SCALE_ROW_CAP.get(scale)
            limit = f" LIMIT {cap}" if cap is not None else ""
            con.execute(
                f"COPY (SELECT {quoted} FROM {table}{limit}) "
                f"TO '{parquet.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
    finally:
        con.close()

    return SourceData(parquet, scale, _row_count(parquet), tuple(columns))


def _choose_table_and_columns(
    con,
    table_filter: str,
    default_columns: tuple[str, ...] | None,
) -> tuple[str, list[str]]:
    """Resolve the qualified table name + its VARCHAR columns to export."""
    rows = con.execute(
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.columns "
        "WHERE data_type = 'VARCHAR' "
        "ORDER BY table_schema, table_name, ordinal_position"
    ).fetchall()
    if not rows:
        raise ValueError("no VARCHAR columns found in the DuckDB database")

    # Group VARCHAR columns per fully-qualified table.
    by_table: dict[tuple[str, str], list[str]] = {}
    for schema, name, col in rows:
        by_table.setdefault((schema, name), []).append(col)

    wanted_schema, _, wanted_name = table_filter.rpartition(".")
    chosen: tuple[str, str] | None = None
    if table_filter:
        for key in by_table:
            schema, name = key
            if name == wanted_name and (not wanted_schema or schema == wanted_schema):
                chosen = key
                break
        if chosen is None:
            raise ValueError(f"table {table_filter!r} not found (or has no VARCHAR columns)")
    else:
        # Most-VARCHAR-columns table wins (the richest string workload).
        chosen = max(by_table, key=lambda k: len(by_table[k]))

    schema, name = chosen
    columns = by_table[chosen]
    if default_columns:
        requested = list(default_columns)
        missing = [c for c in requested if c not in columns]
        if missing:
            raise ValueError(f"requested columns not VARCHAR in {name}: {missing}")
        columns = requested
    return f'"{schema}"."{name}"', columns
