"""Pytest setup: make the repo root importable, and a tiny-parquet fixture used
by the dataset/mining/runner tests (no download, no built binary)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def make_parquet():
    """Factory: write a small, deterministic string parquet to a path.

    `URL` values all start with `http://` and ~1/4 contain `google`, so the miner
    reliably finds `prefix`/`contains` candidates; `WatchID` is an int column.
    """
    import random

    import polars as pl

    def _make(path: Path, n: int = 3000) -> Path:
        rng = random.Random(0)
        hosts = ["google.com", "example.com", "yandex.ru", "bing.com"]
        urls = [f"http://www.{rng.choice(hosts)}/path/{i}" for i in range(n)]
        titles = [f"Title {rng.choice(['google', 'news', 'home'])} {i}" for i in range(n)]
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"URL": urls, "Title": titles, "WatchID": list(range(n))}).write_parquet(path)
        return path

    return _make
