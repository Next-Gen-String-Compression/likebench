"""Render the four benchmark plots from ``results/results.json``.

1. Headline   — latency vs selectivity, grouped by engine×format×mode.
2. Ratio      — compression ratio per column per format.
3. Codec speed— compression + decompression throughput (bars).
4. Throughput — rows/s vs selectivity, grouped by engine×format×mode.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _series_key(run: dict) -> str:
    return f"{run['engine']}/{run['format']}/{run['mode']}"


def _load(results_json: Path) -> dict:
    return json.loads(Path(results_json).read_text())


def render_all(results_json: Path, out_dir: Path) -> list[Path]:
    doc = _load(results_json)
    runs = doc.get("runs", [])
    compression = doc.get("compression", [])
    out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []

    produced.append(_headline_latency(runs, out_dir))
    produced.append(_compression_ratio(compression, out_dir))
    produced.append(_codec_speed(compression, runs, out_dir))
    produced.append(_throughput(runs, out_dir))
    return [p for p in produced if p is not None]


def _predicate_runs(runs: list[dict]) -> list[dict]:
    # Predicate ops that have a meaningful, varying selectivity x-axis.
    ops = {"prefix", "suffix", "contains", "multicontains", "expr"}
    return [r for r in runs if r.get("op") in ops and r.get("selectivity", 0) > 0]


def _headline_latency(runs: list[dict], out_dir: Path) -> Path | None:
    data = _predicate_runs(runs)
    if not data:
        return None
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in data:
        series[_series_key(r)].append((r["selectivity"], r["stats"]["median_ns"] / 1e3))

    fig, ax = plt.subplots(figsize=(8, 5.5))
    for key in sorted(series):
        pts = sorted(series[key])
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, marker="o", ms=4, lw=1.2, label=key)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("selectivity (result_rows / rows)")
    ax.set_ylabel("median latency per op (µs)")
    ax.set_title("LIKE-pushdown: latency vs selectivity")
    ax.grid(True, which="both", ls=":", alpha=0.4)
    ax.legend(fontsize=8, title="engine/format/mode")
    return _save(fig, out_dir / "01_headline_latency_vs_selectivity.png")


def _compression_ratio(compression: list[dict], out_dir: Path) -> Path | None:
    if not compression:
        return None
    cols = sorted({c["column"] for c in compression})
    fmts = sorted({c["fmt"] for c in compression})
    by: dict[tuple[str, str], float] = {(c["column"], c["fmt"]): c["ratio"] for c in compression}
    import numpy as np

    x = np.arange(len(cols))
    width = 0.8 / max(1, len(fmts))
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, fmt in enumerate(fmts):
        vals = [by.get((c, fmt), 0.0) for c in cols]
        ax.bar(x + i * width, vals, width, label=fmt)
    ax.set_xticks(x + width * (len(fmts) - 1) / 2)
    ax.set_xticklabels(cols, rotation=20, ha="right")
    ax.set_ylabel("compression ratio (uncompressed / encoded)")
    ax.set_title("Compression ratio per column per format")
    ax.legend(title="format")
    ax.grid(True, axis="y", ls=":", alpha=0.4)
    return _save(fig, out_dir / "02_compression_ratio.png")


def _codec_speed(compression: list[dict], runs: list[dict], out_dir: Path) -> Path | None:
    if not compression:
        return None
    import numpy as np

    cols = sorted({c["column"] for c in compression})
    fmts = sorted({c["fmt"] for c in compression})

    # Compression speed: uncompressed_bytes / encode_ns -> GB/s.
    comp_speed: dict[tuple[str, str], float] = {}
    for c in compression:
        ns = c.get("encode_ns", 0) or 1
        comp_speed[(c["column"], c["fmt"])] = c.get("uncompressed_bytes", 0) / ns  # bytes/ns = GB/s

    # Decompression speed from runs: in_memory_bytes / decompress_ns -> GB/s.
    # Vortex decompress_ns ~ 0 (compute-on-compressed), reported as NaN/omitted.
    decomp_speed: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in runs:
        col, fmt = r.get("column"), r.get("format")
        dns = r.get("decompress_ns", 0)
        if col and dns and r.get("in_memory_bytes"):
            decomp_speed[(col, fmt)].append(r["in_memory_bytes"] / dns)

    x = np.arange(len(cols))
    width = 0.8 / max(1, len(fmts) * 2)
    fig, ax = plt.subplots(figsize=(9, 5))
    idx = 0
    for fmt in fmts:
        cvals = [comp_speed.get((c, fmt), 0.0) for c in cols]
        ax.bar(x + idx * width, cvals, width, label=f"{fmt} compress")
        idx += 1
    for fmt in fmts:
        dvals = []
        for c in cols:
            vs = decomp_speed.get((c, fmt), [])
            dvals.append(sum(vs) / len(vs) if vs else 0.0)
        ax.bar(x + idx * width, dvals, width, label=f"{fmt} decompress", hatch="//")
        idx += 1
    ax.set_xticks(x + width * (idx - 1) / 2)
    ax.set_xticklabels(cols, rotation=20, ha="right")
    ax.set_ylabel("throughput (GB/s)")
    ax.set_title(
        "Compression / decompression speed\n(vortex decompress ≈ 0: compute-on-compressed)"
    )
    ax.legend(fontsize=8)
    ax.grid(True, axis="y", ls=":", alpha=0.4)
    return _save(fig, out_dir / "03_codec_speed.png")


def _throughput(runs: list[dict], out_dir: Path) -> Path | None:
    data = _predicate_runs(runs)
    if not data:
        return None
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in data:
        series[_series_key(r)].append((r["selectivity"], r["throughput_rows_per_s"] / 1e6))
    fig, ax = plt.subplots(figsize=(8, 5.5))
    for key in sorted(series):
        pts = sorted(series[key])
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="s", ms=4, lw=1.2, label=key)
    ax.set_xscale("log")
    ax.set_xlabel("selectivity (result_rows / rows)")
    ax.set_ylabel("scan throughput (M rows/s)")
    ax.set_title("Predicate scan throughput vs selectivity")
    ax.grid(True, which="both", ls=":", alpha=0.4)
    ax.legend(fontsize=8, title="engine/format/mode")
    return _save(fig, out_dir / "04_throughput.png")


def _save(fig, path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
