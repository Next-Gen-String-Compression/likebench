"""Query specs and engine results — the data model on both ends of the wire.

A *query spec* is the self-describing JSON the harness hands to a binary. It
describes the op (intent), not a raw ``LIKE`` pattern. A *result* is the single
JSON object a binary prints back.

Specs may carry extra harness-only fields (``selectivity``, ``selectivity_bucket``)
that the binaries ignore — the Rust/C++ side deliberately tolerates unknown keys.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SYNTHETIC_OPS = {"prefix", "suffix", "contains", "multicontains", "expr"}
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


@dataclass(frozen=True)
class QuerySpec:
    """Wraps the spec dict that is serialized verbatim to a binary."""

    raw: dict[str, Any]

    # ---- semantic accessors -------------------------------------------------
    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def op(self) -> str | None:
        return self.raw.get("op")

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
    def synthetic(op: str, column: str, *, label: str | None = None, **kw: Any) -> "QuerySpec":
        assert op in SYNTHETIC_OPS, op
        raw: dict[str, Any] = {"kind": "synthetic", "op": op, "column": column}
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

    @staticmethod
    def from_json(text: str) -> "RunResult":
        d = json.loads(text)
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
        )
