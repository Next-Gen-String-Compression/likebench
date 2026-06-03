"""Dataset acquisition with caching keyed on ``dataset × scale × format``.

Scales:
  * ``nano``   — generated, seeded, ~50k rows, offline. CI + smoke tests.
  * ``sample`` — one real ClickBench partition (~1M rows, ~150 MB download).
  * ``full``   — the full ClickBench ``hits`` (~100M rows, ~14 GB download).

Everything is idempotent: a populated, marker-validated cache entry is reused.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from harness.spec import STRING_COLUMNS

CLICKBENCH_SINGLE = "https://datasets.clickhouse.com/hits_compatible/hits.parquet"
CLICKBENCH_PART = (
    "https://datasets.clickhouse.com/hits_compatible/athena_partitioned/hits_{i}.parquet"
)

SCALES = ("nano", "sample", "full")
DEFAULT_SEED = 0xC1BCB

# Number of real partitions to pull for `sample`.
SAMPLE_PARTITIONS = 1


@dataclass(frozen=True)
class SourceData:
    parquet_path: Path
    scale: str
    rows: int
    string_columns: tuple[str, ...]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def prepare(cache_dir: Path, scale: str, *, seed: int = DEFAULT_SEED) -> SourceData:
    """Ensure the canonical source Parquet for ``scale`` exists; return its path."""
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose one of {SCALES}")

    out_dir = cache_dir / "clickbench" / scale
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet = out_dir / "hits.parquet"
    meta = out_dir / "hits.meta.json"

    if parquet.exists() and meta.exists():
        info = json.loads(meta.read_text())
        if info.get("bytes") == parquet.stat().st_size:
            return SourceData(parquet, scale, int(info["rows"]), STRING_COLUMNS)

    if scale == "nano":
        rows = _generate_nano(parquet, seed=seed)
    elif scale == "sample":
        rows = _download_sample(parquet)
    else:
        rows = _download_full(parquet)

    meta.write_text(
        json.dumps({"rows": rows, "bytes": parquet.stat().st_size, "scale": scale}, indent=2)
    )
    return SourceData(parquet, scale, rows, STRING_COLUMNS)


# ---------------------------------------------------------------------------
# nano: deterministic generated data with realistic-ish string distributions
# so the miner can hit every selectivity bucket without a network round-trip.
# ---------------------------------------------------------------------------
def _generate_nano(out: Path, *, seed: int, n: int = 50_000) -> int:
    import numpy as np
    import polars as pl

    rng = np.random.default_rng(seed)

    def pick(options: list[str], probs: list[float], size: int) -> np.ndarray:
        return rng.choice(np.array(options, dtype=object), size=size, p=probs)

    scheme = pick(["http://", "https://"], [0.5, 0.5], n)
    # Hosts carry tokens at controlled frequencies spanning the buckets.
    host = pick(
        ["www.google.com", "yandex.ru", "example.com", "news.site.org", "shop.example.net"],
        [0.01, 0.05, 0.49, 0.10, 0.35],
        n,
    )
    segment = pick(
        ["/news/", "/ads/", "/product/", "/zqx-rare/", "/search", "/"],
        [0.10, 0.03, 0.30, 0.001, 0.20, 0.369],
        n,
    )
    tail = rng.integers(0, 1_000_000, size=n)
    url = np.array(
        [
            f"{s}{h}{seg}{t}.html" if (t % 13 == 0) else f"{s}{h}{seg}{t}"
            for s, h, seg, t in zip(scheme, host, segment, tail)
        ],
        dtype=object,
    )

    title_word = pick(
        ["Breaking News", "Cheap Ads", "Product Page", "Google Search", "Home"],
        [0.10, 0.03, 0.30, 0.01, 0.56],
        n,
    )
    title = np.array(
        [f"{w} {t}.html" if (t % 11 == 0) else f"{w} {t}" for w, t in zip(title_word, tail)],
        dtype=object,
    )

    # Referer often empty; otherwise looks like a URL.
    has_ref = rng.random(n) < 0.4
    referer = np.where(has_ref, url, "").astype(object)

    # SearchPhrase mostly empty (matches ClickBench), occasionally a phrase.
    has_phrase = rng.random(n) < 0.18
    phrase_vocab = pick(
        ["buy shoes", "google maps", "cheap flights", "news today", "zqx widget"],
        [0.40, 0.01, 0.30, 0.28, 0.01],
        n,
    )
    search = np.where(has_phrase, phrase_vocab, "").astype(object)

    df = pl.DataFrame(
        {
            "WatchID": rng.integers(0, 2**62, size=n),
            "CounterID": rng.integers(0, 10_000, size=n),
            "URL": url.tolist(),
            "Title": title.tolist(),
            "Referer": referer.tolist(),
            "SearchPhrase": search.tolist(),
        }
    )
    # zstd so the generated file resembles a real columnar source.
    df.write_parquet(out, compression="zstd")
    return n


# ---------------------------------------------------------------------------
# sample / full downloads
# ---------------------------------------------------------------------------
def _download_sample(out: Path) -> int:
    import polars as pl

    if SAMPLE_PARTITIONS == 1:
        _download(CLICKBENCH_PART.format(i=0), out)
    else:
        parts = []
        tmp_dir = out.parent / "_parts"
        tmp_dir.mkdir(exist_ok=True)
        for i in range(SAMPLE_PARTITIONS):
            p = tmp_dir / f"hits_{i}.parquet"
            _download(CLICKBENCH_PART.format(i=i), p)
            parts.append(p)
        pl.concat([pl.scan_parquet(p) for p in parts]).sink_parquet(out, compression="zstd")
    return _row_count(out)


def _download_full(out: Path) -> int:
    _download(CLICKBENCH_SINGLE, out)
    return _row_count(out)


def _row_count(parquet: Path) -> int:
    import polars as pl

    return pl.scan_parquet(parquet).select(pl.len()).collect().item()


def _download(url: str, dest: Path, *, retries: int = 4) -> None:
    """Stream ``url`` to ``dest`` with exponential-backoff retries."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "likebench/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                next_mark = 0
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total and done >= next_mark:
                        pct = 100 * done / total
                        print(f"\r  {url.rsplit('/', 1)[-1]}: {pct:5.1f}%", end="", flush=True)
                        next_mark = done + total // 50
                print()
            tmp.replace(dest)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover
            last = exc
            wait = 2 ** (attempt + 1)
            print(f"  download failed ({exc}); retry in {wait}s ...")
            time.sleep(wait)
    raise RuntimeError(
        f"failed to download {url} after {retries} attempts: {last}.\n"
        f"If this environment has no outbound access to datasets.clickhouse.com, "
        f"use --scale nano (offline, generated)."
    )


# ---------------------------------------------------------------------------
# Dataset fingerprint for results/metadata.json
# ---------------------------------------------------------------------------
def dataset_hash(parquet: Path) -> str:
    """Cheap, stable fingerprint: size + sha256 of head/tail 4 MiB.

    Full hashing of a 14 GB file is wasteful; head+tail+size uniquely identifies
    the canonical ClickBench artifact for provenance purposes.
    """
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
