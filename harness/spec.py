"""Query specs and engine results — the data model on both ends of the wire.

A *query spec* is the JSON the harness hands to a binary. A synthetic spec
carries a predicate AST over a column — ``prefix``/``suffix``/``contains`` or a
``multi-contains`` (``%a%b%…%`` — each value in order). The binary *converts* the
AST to a ``LIKE`` clause (SQL engines) or *walks* it directly (scan engines);
nothing parses ``LIKE`` back. A *result* is the single JSON object a binary
prints back.

Specs may carry extra harness-only fields (``selectivity``, ``selectivity_bucket``)
that the binaries ignore — the Rust/C++ side deliberately tolerates unknown keys.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

STRING_COLUMNS = ("URL", "Title", "Referer", "SearchPhrase")

# Target selectivity buckets used by the miner and the headline plot.
SELECTIVITY_BUCKETS: dict[str, float] = {
    "p001": 0.001,  # 0.1 %
    "p01": 0.01,  # 1 %
    "p10": 0.10,  # 10 %
    "p50": 0.50,  # 50 %
}


def _canonical(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


# ---- predicate AST helpers (the Python mirror of bench_core's `Pred`) --------
# Keep in lockstep with crates/bench-core/src/lib.rs. A predicate is a dict:
#   {"op": "prefix"|"suffix"|"contains", "value": str}
#   {"op": "multi-contains", "values": [str, ...]}  # ordered: `%a%b%…%`


def prefix(value: str) -> dict[str, Any]:
    return {"op": "prefix", "value": value}


def suffix(value: str) -> dict[str, Any]:
    return {"op": "suffix", "value": value}


def contains(value: str) -> dict[str, Any]:
    return {"op": "contains", "value": value}


def multi_contains(values: list[str]) -> dict[str, Any]:
    """`%a%b%…%` — each value occurs in order. Not an AND of substrings."""
    return {"op": "multi-contains", "values": list(values)}


def predicate_matches(pred: dict[str, Any], s: str) -> bool:
    """Evaluate a predicate AST against a string — mirrors bench_core::Matcher.
    Lets the miner estimate selectivity without any LIKE/regex machinery."""
    op = pred["op"]
    if op == "prefix":
        return s.startswith(pred["value"])
    if op == "suffix":
        return s.endswith(pred["value"])
    if op == "contains":
        return pred["value"] in s
    if op == "multi-contains":
        # `%a%b%`: find each value in order, each after the previous match.
        start = 0
        for v in pred["values"]:
            i = s.find(v, start)
            if i < 0:
                return False
            start = i + len(v)
        return True
    raise ValueError(f"unknown predicate op: {op!r}")


@dataclass(frozen=True)
class QuerySpec:
    """Wraps the spec dict that is serialized verbatim to a binary."""

    raw: dict[str, Any]

    # ---- semantic accessors -------------------------------------------------
    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def predicate(self) -> dict[str, Any] | None:
        return self.raw.get("predicate")

    @property
    def op(self) -> str | None:
        """The op tag from the predicate AST (mirrors bench_core's ``op_label``);
        ``None`` for non-synthetic specs."""
        pred = self.raw.get("predicate")
        return pred.get("op") if pred else None

    @property
    def column(self) -> str | None:
        return self.raw.get("column")

    @property
    def label(self) -> str:
        return self.raw.get("label") or self.identity()[:16]

    @property
    def selectivity(self) -> float | None:
        v = self.raw.get("selectivity")
        return float(v) if v is not None else None

    @property
    def selectivity_bucket(self) -> str | None:
        return self.raw.get("selectivity_bucket")

    # ---- identity -----------------------------------------------------------
    def semantic(self) -> dict[str, Any]:
        """Just the parts that define *what* runs (drop cosmetic/harness keys)."""
        drop = {"label", "selectivity", "selectivity_bucket"}
        return {k: v for k, v in self.raw.items() if k not in drop}

    def identity(self) -> str:
        """Stable hash of the semantic content.

        Two binaries handed specs with the same identity MUST return the same
        ``result_rows`` and ``result_checksum``; that is the correctness gate.
        """
        return hashlib.sha1(_canonical(self.semantic()).encode()).hexdigest()

    def to_json(self) -> str:
        return _canonical(self.raw)

    # ---- construction helpers ----------------------------------------------
    @staticmethod
    def synthetic(
        column: str,
        predicate: dict[str, Any],
        *,
        label: str | None = None,
        **kw: Any,
    ) -> "QuerySpec":
        """Build a synthetic spec from a predicate AST.

        Build ``predicate`` with the module helpers, e.g.
        ``QuerySpec.synthetic("URL", contains("google"))`` or
        ``QuerySpec.synthetic("URL", multi_contains(["a", "b"]))``.
        """
        assert "op" in predicate, "predicate needs an 'op'"
        raw: dict[str, Any] = {
            "kind": "synthetic",
            "column": column,
            "predicate": dict(predicate),
        }
        raw.update(kw)
        if label:
            raw["label"] = label
        return QuerySpec(raw)

    @staticmethod
    def real(sql: str, *, label: str | None = None, column: str | None = None) -> "QuerySpec":
        raw: dict[str, Any] = {"kind": "real", "sql": sql}
        if label:
            raw["label"] = label
        if column:
            raw["column"] = column
        return QuerySpec(raw)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "QuerySpec":
        return QuerySpec(dict(d))


@dataclass(frozen=True)
class RunResult:
    """Parsed JSON emitted by a binary for a single (engine, format, mode, query)."""

    engine: str
    format: str
    mode: str
    label: str
    op: str | None
    column: str | None
    rows: int
    result_rows: int
    result_checksum: str
    file_bytes: int
    in_memory_bytes: int
    load_ns: int
    decompress_ns: int
    pushdown: bool
    plan: str
    iters_ns: list[int]
    raw: dict[str, Any]
    # compression-quality extension (only the standalone string codecs set these)
    codec: str | None = None
    compress_ns: int | None = None
    compressed_bytes: int | None = None
    decompress_random_ns: int | None = None

    @staticmethod
    def from_json(text: str) -> "RunResult":
        d = json.loads(text)

        def _opt_int(key: str) -> int | None:
            v = d.get(key)
            return int(v) if v is not None else None

        return RunResult(
            engine=d["engine"],
            format=d["format"],
            mode=d["mode"],
            label=d.get("label", ""),
            op=d.get("op"),
            column=d.get("column"),
            rows=int(d["rows"]),
            result_rows=int(d["result_rows"]),
            result_checksum=str(d["result_checksum"]),
            file_bytes=int(d.get("file_bytes", 0)),
            in_memory_bytes=int(d.get("in_memory_bytes", 0)),
            load_ns=int(d.get("load_ns", 0)),
            decompress_ns=int(d.get("decompress_ns", 0)),
            pushdown=bool(d.get("pushdown", False)),
            plan=str(d.get("plan", "")),
            iters_ns=[int(x) for x in d["iters_ns"]],
            raw=d,
            codec=d.get("codec"),
            compress_ns=_opt_int("compress_ns"),
            compressed_bytes=_opt_int("compressed_bytes"),
            decompress_random_ns=_opt_int("decompress_random_ns"),
        )
