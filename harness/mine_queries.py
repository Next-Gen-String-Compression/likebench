"""Mine predicates from real string-column distributions.

For every string column we mine ``prefix`` / ``suffix`` / ``contains`` values
bucketed to target selectivities (0.1 % / 1 % / 10 % / 50 %), plus one
``multicontains`` and one ``expr`` composite, and we attach a handful of
ClickBench-style ``real`` SQL queries. Everything is deterministic given the
seed: candidate discovery and selectivity estimation run on a fixed
(``head``-sliced) sample, and the recorded ``selectivity`` for each chosen value
is measured exactly on that sample.

The exact run-time selectivity used by the plots is ``result_rows / rows`` from
the engine output; the mined ``selectivity`` here only drives bucketing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from harness.spec import SELECTIVITY_BUCKETS, QuerySpec

# How many rows to inspect when mining/estimating (deterministic head slice).
MINE_SAMPLE_ROWS = 1_000_000
# A candidate counts for a bucket only if within this multiplicative tolerance.
BUCKET_TOLERANCE = 3.0
MIN_TOKEN_LEN = 3


@dataclass
class _Candidate:
    value: str
    selectivity: float


def _closest_for_bucket(cands: list[_Candidate], target: float) -> _Candidate | None:
    """Pick the candidate whose selectivity is nearest ``target`` (log distance)."""
    import math

    best: _Candidate | None = None
    best_d = float("inf")
    for c in cands:
        if c.selectivity <= 0:
            continue
        d = abs(math.log(c.selectivity) - math.log(target))
        if d < best_d:
            best, best_d = c, d
    if best is None:
        return None
    # reject if wildly off (more than tolerance x in either direction)
    if best.selectivity > target * BUCKET_TOLERANCE or best.selectivity < target / BUCKET_TOLERANCE:
        return None
    return best


def _exact_contains(series, value: str) -> float:
    return float(series.str.contains(value, literal=True).mean() or 0.0)


def _prefix_candidates(series) -> list[_Candidate]:

    out: list[_Candidate] = []
    n = series.len()
    seen: set[str] = set()
    for k in (4, 6, 8, 10, 14, 20):
        vc = series.str.slice(0, k).value_counts(sort=True).head(6)
        col = vc.columns[0]
        for row in vc.iter_rows(named=True):
            val = row[col]
            cnt = row["count"]
            if not val or val in seen:
                continue
            seen.add(val)
            out.append(_Candidate(val, cnt / n))
    return out


def _suffix_candidates(series) -> list[_Candidate]:
    out: list[_Candidate] = []
    n = series.len()
    seen: set[str] = set()
    for k in (4, 5, 6, 8, 12):
        vc = series.str.slice(-k).value_counts(sort=True).head(6)
        col = vc.columns[0]
        for row in vc.iter_rows(named=True):
            val = row[col]
            cnt = row["count"]
            if not val or val in seen:
                continue
            seen.add(val)
            out.append(_Candidate(val, cnt / n))
    return out


def _contains_candidates(series) -> list[_Candidate]:
    """Document-frequency of alnum tokens, then exact substring selectivity."""

    n = series.len()
    tokens = (
        series.str.extract_all(rf"[A-Za-z0-9]{{{MIN_TOKEN_LEN},}}")
        .list.unique()
        .explode()
        .drop_nulls()
    )
    vc = tokens.value_counts(sort=True)
    val_col = vc.columns[0]
    cands: list[_Candidate] = []
    # Spread the search across the frequency range: head (frequent) + a stride
    # through the tail (rare) so low buckets are reachable.
    rows = vc.to_dicts()
    picks = rows[:40] + rows[40 :: max(1, len(rows) // 60)]
    seen: set[str] = set()
    for row in picks:
        tok = row[val_col]
        if not tok or tok in seen:
            continue
        seen.add(tok)
        df = row["count"] / n
        # token document-frequency lower-bounds substring selectivity; refine
        # exactly only for plausible candidates to keep this cheap.
        if 1e-5 <= df <= 0.95:
            cands.append(_Candidate(tok, df))
    return cands


def mine(
    source_parquet: Path,
    columns: tuple[str, ...],
    *,
    seed: int = 0,
    sample_rows: int = MINE_SAMPLE_ROWS,
    max_per_op: int | None = None,
) -> list[QuerySpec]:
    import polars as pl

    specs: list[QuerySpec] = []

    for col in columns:
        s = (
            pl.scan_parquet(source_parquet)
            .select(pl.col(col).cast(pl.Utf8))
            .drop_nulls()
            .head(sample_rows)
            .collect()
            .to_series()
        )
        if s.len() == 0:
            continue

        op_cands = {
            "prefix": _prefix_candidates(s),
            "suffix": _suffix_candidates(s),
            "contains": _contains_candidates(s),
        }

        chosen_contains: dict[str, str] = {}
        for op, cands in op_cands.items():
            for bucket, target in SELECTIVITY_BUCKETS.items():
                pick = _closest_for_bucket(cands, target)
                if pick is None:
                    continue
                # Refine `contains` selectivity to exact substring match.
                sel = _exact_contains(s, pick.value) if op == "contains" else pick.selectivity
                if op == "contains":
                    chosen_contains[bucket] = pick.value
                specs.append(
                    QuerySpec(
                        {
                            "kind": "synthetic",
                            "op": op,
                            "column": col,
                            "value": pick.value,
                            "label": f"{op}_{col}_{bucket}",
                            "selectivity": round(sel, 6),
                            "selectivity_bucket": bucket,
                        }
                    )
                )

        # multicontains: AND two tokens from different buckets (intersection is
        # rarer -> a naturally low-selectivity composite).
        if "p10" in chosen_contains and "p01" in chosen_contains:
            v1, v2 = chosen_contains["p10"], chosen_contains["p01"]
            if v1 != v2:
                sel_all = float(
                    (s.str.contains(v1, literal=True) & s.str.contains(v2, literal=True)).mean()
                    or 0.0
                )
                specs.append(
                    QuerySpec(
                        {
                            "kind": "synthetic",
                            "op": "multicontains",
                            "column": col,
                            "values": [v1, v2],
                            "mode": "all",
                            "label": f"multicontains_{col}_all",
                            "selectivity": round(sel_all, 6),
                            "selectivity_bucket": "mixed",
                        }
                    )
                )

        # expr: prefix AND contains.
        pre = next((sp for sp in specs if sp.op == "prefix" and sp.column == col), None)
        con = next((sp for sp in specs if sp.op == "contains" and sp.column == col), None)
        if pre is not None and con is not None:
            pv = pre.raw["value"]
            cv = con.raw["value"]
            sel_expr = float(
                (s.str.starts_with(pv) & s.str.contains(cv, literal=True)).mean() or 0.0
            )
            specs.append(
                QuerySpec(
                    {
                        "kind": "synthetic",
                        "op": "expr",
                        "column": col,
                        "predicate": {"and": [{"prefix": pv}, {"contains": cv}]},
                        "label": f"expr_{col}_prefix_and_contains",
                        "selectivity": round(sel_expr, 6),
                        "selectivity_bucket": "mixed",
                    }
                )
            )

        # A ClickBench-style real SQL query on this column (single-column
        # count(*), directly comparable to the synthetic `contains`).
        mid = chosen_contains.get("p01") or chosen_contains.get("p10")
        if mid:
            specs.append(
                QuerySpec(
                    {
                        "kind": "real",
                        "sql": f"SELECT count(*) FROM hits WHERE \"{col}\" LIKE '%{mid}%'",
                        "column": col,
                        "label": f"clickbench_like_{col}",
                    }
                )
            )

    if max_per_op is not None:
        specs = _cap_per_op(specs, max_per_op)
    return specs


def _cap_per_op(specs: list[QuerySpec], cap: int) -> list[QuerySpec]:
    counts: dict[tuple[str | None, str | None], int] = {}
    out: list[QuerySpec] = []
    for sp in specs:
        key = (sp.op, sp.column)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= cap:
            out.append(sp)
    return out


def write_queries(specs: list[QuerySpec], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "buckets": SELECTIVITY_BUCKETS,
        "queries": [sp.raw for sp in specs],
    }
    path.write_text(json.dumps(payload, indent=2))


def load_queries(path: Path) -> list[QuerySpec]:
    doc = json.loads(Path(path).read_text())
    return [QuerySpec(q) for q in doc["queries"]]
