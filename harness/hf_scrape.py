"""HuggingFace dataset source — likebench's port of CompressionBenchmark's
``hf scrape`` tooling.

Resolves a HuggingFace dataset to one of its Parquet shards (served by the HF
datasets-server) and hands it to the generic Parquet path, so any HF dataset of
strings can be benchmarked. No extra Python dependency: we hit the public REST
API with urllib.

Selector syntax (after the ``hf:`` prefix): ``<repo>[#substr]`` where ``repo`` is
``owner/name`` and the optional ``#substr`` selects a specific shard URL by
substring (e.g. a config or split name like ``train``).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

from harness.data import SourceData, _download, _row_count

_PARQUET_API = "https://datasets-server.huggingface.co/parquet?dataset="
_SCALE_ROW_CAP = {"nano": 50_000, "sample": 1_000_000}


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")[:64] or "hf"


def _list_parquet_urls(repo: str) -> list[dict]:
    """Return the datasets-server parquet shard descriptors for ``repo``."""
    url = _PARQUET_API + urllib.parse.quote(repo, safe="/")
    req = urllib.request.Request(url, headers={"User-Agent": "likebench/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        doc = json.loads(resp.read().decode())
    files = doc.get("parquet_files", [])
    if not files:
        raise RuntimeError(f"no parquet shards listed for HF dataset {repo!r}")
    return files


def prepare(arg: str, cache_dir: Path, *, scale: str) -> SourceData:
    import polars as pl

    repo, _, want = arg.partition("#")
    repo = repo.strip()
    files = _list_parquet_urls(repo)

    chosen = None
    if want:
        chosen = next((f for f in files if want in f.get("url", "") or want == f.get("split")), None)
        if chosen is None:
            raise ValueError(f"no HF shard matching {want!r} for {repo!r}")
    else:
        chosen = files[0]
    shard_url = chosen["url"]

    out_dir = cache_dir / "hf" / _slug(arg) / scale
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet = out_dir / "data.parquet"
    if not parquet.exists():
        tmp = out_dir / "_shard.parquet"
        _download(shard_url, tmp)
        cap = _SCALE_ROW_CAP.get(scale)
        lf = pl.scan_parquet(tmp)
        if cap is not None:
            lf = lf.head(cap)
        lf.sink_parquet(parquet, compression="zstd")
        tmp.unlink(missing_ok=True)

    return SourceData(parquet, scale, _row_count(parquet), ())
