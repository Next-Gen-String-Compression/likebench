"""Drive the ``convert`` binary to build the per-column benchmark inputs.

For every string column we materialize two files the engines will read:

    data/cache/clickbench/<scale>/cols/<COLUMN>.parquet   (canonical Parquet)
    data/cache/clickbench/<scale>/cols/<COLUMN>.vortex     (Vortex cascade)

and we record the compression metrics (encode_ns, ratio, ...) that feed the
compression-ratio and compression-speed plots. Per-column files keep the
``in-mem`` loads focused, and a single-column file still answers ClickBench's
``count(*) WHERE <col> LIKE ...`` real queries.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ConvertMetrics:
    column: str
    fmt: str
    path: Path
    encode_ns: int
    input_bytes: int
    uncompressed_bytes: int
    output_bytes: int
    ratio: float
    rows: int
    codec: str
    # Fair, framing-free compressed footprint (Vortex array-tree nbytes / Parquet
    # summed column-chunk size). Defaults to output_bytes for older caches.
    inmem_compressed_bytes: int = 0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["path"] = str(self.path)
        return d


@dataclass
class ColumnInputs:
    """Where each format's per-column file lives, for the runner to read."""

    parquet: Path
    vortex: Path
    strings: Path | None = None

    def for_format(self, fmt: str) -> Path:
        if fmt == "parquet":
            return self.parquet
        if fmt == "raw":
            assert self.strings is not None, "raw format requested but no .strings file"
            return self.strings
        return self.vortex


def write_strings_file(path: Path, values: list[str]) -> None:
    """Write a column as the dependency-free STRZ `.strings` interchange file.

    Byte-identical to ``bench_core::strings`` / ``cpp/common/strings_file.hpp``.
    Nulls are materialized as empty strings so the row count matches the
    Parquet/Vortex engines (empty strings never match a non-empty predicate).
    """
    import struct

    import numpy as np

    blobs = [v.encode("utf-8") for v in values]
    lengths = np.fromiter((len(b) for b in blobs), dtype="<u8", count=len(blobs))
    offsets = np.zeros(len(blobs) + 1, dtype="<u8")
    np.cumsum(lengths, out=offsets[1:])
    data = b"".join(blobs)
    with path.open("wb") as fh:
        fh.write(b"STRZ")
        fh.write(struct.pack("<I", 1))
        fh.write(struct.pack("<Q", len(blobs)))
        fh.write(struct.pack("<Q", len(data)))
        fh.write(offsets.tobytes())
        fh.write(data)


def _run_convert(convert_bin: Path, args: list[str]) -> dict:
    proc = subprocess.run(
        [str(convert_bin), *args, "--report", "json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"convert failed ({proc.returncode}): {' '.join(args)}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def build_columns(
    *,
    source_parquet: Path,
    cols_dir: Path,
    convert_bin: Path,
    columns: tuple[str, ...],
    parquet_codec: str = "zstd",
    force: bool = False,
) -> tuple[dict[str, ColumnInputs], list[ConvertMetrics]]:
    """Project each column and transcode to canonical Parquet + Vortex."""
    import polars as pl

    cols_dir.mkdir(parents=True, exist_ok=True)
    inputs: dict[str, ColumnInputs] = {}
    metrics: list[ConvertMetrics] = []

    for col in columns:
        raw = cols_dir / f"{col}.raw.parquet"
        pq = cols_dir / f"{col}.parquet"
        vx = cols_dir / f"{col}.vortex"
        st = cols_dir / f"{col}.strings"
        inputs[col] = ColumnInputs(parquet=pq, vortex=vx, strings=st)

        if not force and pq.exists() and vx.exists() and st.exists():
            # Reuse; reconstruct metrics from the sidecar if present.
            side = cols_dir / f"{col}.metrics.json"
            if side.exists():
                for m in json.loads(side.read_text()):
                    metrics.append(ConvertMetrics(**{**m, "path": Path(m["path"])}))
            continue

        # The dependency-free `.strings` column read by the standalone string
        # codecs (bench-compress-cpp, bench-onpair).
        col_values = (
            pl.scan_parquet(source_parquet)
            .select(pl.col(col).cast(pl.Utf8).fill_null(""))
            .collect()
            .to_series()
            .to_list()
        )
        write_strings_file(st, col_values)

        # 1. Project the single column into a carrier Parquet (snappy: cheap,
        #    just a transport into the convert binary).
        pl.scan_parquet(source_parquet).select(pl.col(col).cast(pl.Utf8)).sink_parquet(
            raw, compression="snappy"
        )

        # 2. Canonical Parquet (this is what the parquet-format engines read).
        m_pq = _run_convert(
            convert_bin,
            [
                "--input",
                str(raw),
                "--input-format",
                "parquet",
                "--output",
                str(pq),
                "--output-format",
                "parquet",
                "--compression",
                parquet_codec,
            ],
        )
        # 3. Vortex (default cascade).
        m_vx = _run_convert(
            convert_bin,
            [
                "--input",
                str(pq),
                "--input-format",
                "parquet",
                "--output",
                str(vx),
                "--output-format",
                "vortex",
                "--compression",
                "default",
            ],
        )
        raw.unlink(missing_ok=True)

        col_metrics = [
            ConvertMetrics(
                column=col,
                fmt="parquet",
                path=pq,
                codec=parquet_codec,
                encode_ns=int(m_pq["encode_ns"]),
                input_bytes=int(m_pq["input_bytes"]),
                uncompressed_bytes=int(m_pq.get("uncompressed_bytes", m_pq["input_bytes"])),
                output_bytes=int(m_pq["output_bytes"]),
                ratio=float(m_pq["ratio"]),
                rows=int(m_pq.get("rows", 0)),
                inmem_compressed_bytes=int(
                    m_pq.get("inmem_compressed_bytes", m_pq["output_bytes"])
                ),
            ),
            ConvertMetrics(
                column=col,
                fmt="vortex",
                path=vx,
                codec="default",
                encode_ns=int(m_vx["encode_ns"]),
                input_bytes=int(m_vx["input_bytes"]),
                uncompressed_bytes=int(m_vx.get("uncompressed_bytes", m_vx["input_bytes"])),
                output_bytes=int(m_vx["output_bytes"]),
                ratio=float(m_vx["ratio"]),
                rows=int(m_vx.get("rows", 0)),
                inmem_compressed_bytes=int(
                    m_vx.get("inmem_compressed_bytes", m_vx["output_bytes"])
                ),
            ),
        ]
        metrics.extend(col_metrics)
        (cols_dir / f"{col}.metrics.json").write_text(
            json.dumps([m.as_dict() for m in col_metrics], indent=2)
        )

    return inputs, metrics
