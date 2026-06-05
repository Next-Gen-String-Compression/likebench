"""Dataset-agnostic source primitives shared by every dataset provider.

A ``SourceData`` is "a Parquet file + its string columns + a row count". Providers
(currently just TPC-H) produce one; the rest of the pipeline (convert, mine, run)
consumes it without caring where it came from.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceData:
    parquet_path: Path
    scale: str
    rows: int
    string_columns: tuple[str, ...]


def utf8_columns(parquet: Path) -> tuple[str, ...]:
    """Every Utf8/String column in a Parquet file (likebench's VARCHAR scan)."""
    import polars as pl

    schema = pl.scan_parquet(parquet).collect_schema()
    return tuple(
        name for name, dtype in schema.items() if dtype in (pl.Utf8, pl.String, pl.Categorical)
    )


def row_count(parquet: Path) -> int:
    import polars as pl

    return pl.scan_parquet(parquet).select(pl.len()).collect().item()


def dataset_hash(parquet: Path) -> str:
    """Cheap, stable fingerprint: size + sha256 of head/tail 4 MiB."""
    size = parquet.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    window = 4 << 20
    with parquet.open("rb") as fh:
        h.update(fh.read(window))
        if size > window:
            fh.seek(max(0, size - window))
            h.update(fh.read(window))
    return f"sha256-head-tail:{h.hexdigest()}"


def download(url: str, dest: Path, *, retries: int = 4) -> None:
    """Stream ``url`` to ``dest`` with exponential-backoff retries."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "likebench/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
            last = exc
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"failed to download {url} after {retries} attempts: {last}")
