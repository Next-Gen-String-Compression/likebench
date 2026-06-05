"""Run the benchmark matrix and aggregate.

For each ``(binary × mode × format × kind × query)`` the binary supports, invoke
it with ``--iterations W+M``, discard the first ``W`` as warmup, and aggregate
the rest. The binaries never know what warmup is.

Two reviewer gates run here:
  * **correctness** — every engine×format×mode that ran the *same* predicate must
    agree on ``result_rows`` and ``result_checksum``; mismatch fails loudly.
  * **pushdown labelling** — each run is tagged ``baseline`` (parquet),
    ``treatment`` (vortex, predicate pushed down onto compressed data) or
    ``control`` (vortex, fell back to decode).
"""

from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from harness.convert import ColumnInputs
from harness.manifest import Binary
from harness.spec import QuerySpec, RunResult


class CorrectnessError(RuntimeError):
    """Raised when engines disagree on a query's result."""


@dataclass
class Stats:
    min_ns: int
    median_ns: float
    p50_ns: float
    p95_ns: float
    p99_ns: float
    mean_ns: float
    stddev_ns: float


@dataclass
class RunEntry:
    engine: str
    format: str
    mode: str
    kind: str
    op: str | None
    column: str | None
    label: str
    query_identity: str
    selectivity_bucket: str | None
    rows: int
    result_rows: int
    result_checksum: str
    selectivity: float
    file_bytes: int
    in_memory_bytes: int
    load_ns: int
    decompress_ns: int
    pushdown: bool
    plan: str
    group: str
    warmup: int
    measured: int
    stats: Stats
    throughput_rows_per_s: float
    iters_ns: list[int]
    # compression-quality extension (standalone string codecs only)
    codec: str | None = None
    compress_ns: int | None = None
    compressed_bytes: int | None = None
    decompress_random_ns: int | None = None
    compression_ratio: float | None = None


@dataclass
class Job:
    binary: Binary
    mode: str
    fmt: str
    spec: QuerySpec
    input_path: Path
    explain: bool = True


def _percentile(sorted_vals: list[int], pct: float) -> float:
    """Nearest-rank percentile."""
    if not sorted_vals:
        return 0.0
    import math

    k = max(1, math.ceil(pct / 100.0 * len(sorted_vals)))
    return float(sorted_vals[k - 1])


def aggregate(iters_ns: list[int], warmup: int) -> Stats:
    measured = iters_ns[warmup:] if warmup < len(iters_ns) else iters_ns[-1:]
    s = sorted(measured)
    return Stats(
        min_ns=s[0],
        median_ns=statistics.median(s),
        p50_ns=_percentile(s, 50),
        p95_ns=_percentile(s, 95),
        p99_ns=_percentile(s, 99),
        mean_ns=statistics.fmean(s),
        stddev_ns=statistics.pstdev(s) if len(s) > 1 else 0.0,
    )


def _group_of(fmt: str, pushdown: bool) -> str:
    if fmt == "parquet":
        return "baseline"
    if fmt == "raw":
        # Standalone string codecs: compressed storage, decompress-then-scan.
        return "codec"
    return "treatment" if pushdown else "control"


def build_jobs(
    binaries: list[Binary],
    specs: list[QuerySpec],
    inputs: dict[str, ColumnInputs],
    *,
    modes: tuple[str, ...] | None = None,
    formats: tuple[str, ...] | None = None,
    explain: bool = True,
) -> list[Job]:
    jobs: list[Job] = []
    for b in binaries:
        for mode in b.modes:
            if modes and mode not in modes:
                continue
            for fmt in b.formats:
                if formats and fmt not in formats:
                    continue
                for spec in specs:
                    if not b.supports(mode=mode, fmt=fmt, kind=spec.kind):
                        continue
                    col = spec.column
                    if col is None or col not in inputs:
                        # Real query with no single-column home -> skip (our
                        # per-column files can't answer multi-column SQL).
                        continue
                    jobs.append(Job(b, mode, fmt, spec, inputs[col].for_format(fmt), explain))
    return jobs


def run_job(job: Job, repo_root: Path, *, warmup: int, measured: int) -> RunEntry:
    binpath = job.binary.binary_path(repo_root)
    if not binpath.exists():
        raise FileNotFoundError(
            f"binary {binpath} not built. Run ./setup.sh (or cargo build --release "
            f"-p {job.binary.name})."
        )
    k = warmup + measured
    args = [
        str(binpath),
        "--format",
        job.fmt,
        "--input",
        str(job.input_path),
        "--mode",
        job.mode,
        "--query-spec",
        job.spec.to_json(),
        "--iterations",
        str(k),
        "--output",
        "json",
    ]
    args.extend(job.binary.extra_args())
    if job.explain:
        args.append("--explain")

    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{job.binary.name} failed ({proc.returncode}) on {job.spec.label} "
            f"[{job.fmt}/{job.mode}]\nargs: {args}\nstderr:\n{proc.stderr}"
        )
    res = RunResult.from_json(proc.stdout.strip().splitlines()[-1])
    if len(res.iters_ns) != k:
        raise RuntimeError(f"{job.binary.name} returned {len(res.iters_ns)} iters, expected {k}")

    stats = aggregate(res.iters_ns, warmup)
    selectivity = res.result_rows / res.rows if res.rows else 0.0
    median_s = stats.median_ns * 1e-9
    throughput = res.rows / median_s if median_s > 0 else 0.0

    return RunEntry(
        engine=res.engine,
        format=res.format,
        mode=res.mode,
        kind=job.spec.kind,
        op=job.spec.op,
        column=job.spec.column,
        label=job.spec.label,
        query_identity=job.spec.identity(),
        selectivity_bucket=job.spec.selectivity_bucket,
        rows=res.rows,
        result_rows=res.result_rows,
        result_checksum=res.result_checksum,
        selectivity=selectivity,
        file_bytes=res.file_bytes,
        in_memory_bytes=res.in_memory_bytes,
        load_ns=res.load_ns,
        decompress_ns=res.decompress_ns,
        pushdown=res.pushdown,
        plan=res.plan,
        group=_group_of(res.format, res.pushdown),
        warmup=warmup,
        measured=measured,
        stats=stats,
        throughput_rows_per_s=throughput,
        iters_ns=res.iters_ns,
        codec=res.codec,
        compress_ns=res.compress_ns,
        compressed_bytes=res.compressed_bytes,
        decompress_random_ns=res.decompress_random_ns,
        compression_ratio=(
            res.in_memory_bytes / res.compressed_bytes if res.compressed_bytes else None
        ),
    )


def check_correctness(entries: list[RunEntry], *, allow_mismatch: bool = False) -> list[str]:
    """Assert all engines agree per predicate identity. Returns warning strings."""
    by_id: dict[str, list[RunEntry]] = {}
    for e in entries:
        by_id.setdefault(e.query_identity, []).append(e)

    problems: list[str] = []
    for ident, group in by_id.items():
        rows = {e.result_rows for e in group}
        sums = {e.result_checksum for e in group}
        if len(rows) > 1 or len(sums) > 1:
            detail = ", ".join(
                f"{e.engine}/{e.format}/{e.mode}:rows={e.result_rows},cksum={e.result_checksum}"
                for e in group
            )
            label = group[0].label
            problems.append(f"MISMATCH for {label} ({ident[:12]}): {detail}")

    if problems and not allow_mismatch:
        raise CorrectnessError("cross-engine correctness check FAILED:\n  " + "\n  ".join(problems))
    return problems


def run_matrix(
    jobs: list[Job],
    repo_root: Path,
    *,
    warmup: int,
    measured: int,
    allow_mismatch: bool = False,
    progress: bool = True,
) -> tuple[list[RunEntry], list[str]]:
    entries: list[RunEntry] = []
    total = len(jobs)
    for i, job in enumerate(jobs, 1):
        if progress:
            print(
                f"[{i:>3}/{total}] {job.binary.name:<16} "
                f"{job.fmt:<7} {job.mode:<10} {job.spec.label}",
                flush=True,
            )
        entries.append(run_job(job, repo_root, warmup=warmup, measured=measured))
    warnings = check_correctness(entries, allow_mismatch=allow_mismatch)
    return entries, warnings


# ---------------------------------------------------------------------------
# Serialization + tables
# ---------------------------------------------------------------------------
def entry_to_dict(e: RunEntry) -> dict:
    d = asdict(e)
    return d


def write_results_json(
    path: Path,
    *,
    entries: list[RunEntry],
    compression: list[dict],
    meta: dict,
    warnings: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "meta": meta,
        "warnings": warnings,
        "compression": compression,
        "runs": [entry_to_dict(e) for e in entries],
    }
    path.write_text(json.dumps(payload, indent=2))


def write_tables(md_path: Path, csv_path: Path, entries: list[RunEntry]) -> None:
    import csv

    headers = [
        "engine",
        "format",
        "mode",
        "kind",
        "op",
        "column",
        "label",
        "group",
        "selectivity",
        "rows",
        "result_rows",
        "median_us",
        "p95_us",
        "min_us",
        "load_ms",
        "decompress_ms",
        "throughput_Mrows_s",
        "pushdown",
    ]

    def row_of(e: RunEntry) -> list:
        return [
            e.engine,
            e.format,
            e.mode,
            e.kind,
            e.op or "",
            e.column or "",
            e.label,
            e.group,
            f"{e.selectivity:.6f}",
            e.rows,
            e.result_rows,
            f"{e.stats.median_ns / 1e3:.2f}",
            f"{e.stats.p95_ns / 1e3:.2f}",
            f"{e.stats.min_ns / 1e3:.2f}",
            f"{e.load_ns / 1e6:.2f}",
            f"{e.decompress_ns / 1e6:.2f}",
            f"{e.throughput_rows_per_s / 1e6:.2f}",
            "yes" if e.pushdown else "no",
        ]

    rows = [row_of(e) for e in sorted(entries, key=lambda x: (x.label, x.engine, x.format, x.mode))]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        w.writerows(rows)

    md_lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        md_lines.append("| " + " | ".join(str(x) for x in r) + " |")
    md_path.write_text("\n".join(md_lines) + "\n")
