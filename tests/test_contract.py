"""Contract tests: query-spec identity and engine-manifest parsing."""

from __future__ import annotations

import pytest

from harness.manifest import load_manifest
from harness.spec import QuerySpec, contains, multi_contains, predicate_matches, prefix


def test_spec_identity_ignores_cosmetic_fields():
    pred = {"op": "contains", "value": "g"}
    a = QuerySpec.from_dict({"kind": "synthetic", "column": "URL", "predicate": pred, "label": "x"})
    b = QuerySpec.from_dict(
        {
            "kind": "synthetic",
            "column": "URL",
            "predicate": pred,
            "label": "y",
            "selectivity": 0.1,
            "selectivity_bucket": "p10",
        }
    )
    # Cosmetic fields (label, selectivity) don't change the correctness identity.
    assert a.identity() == b.identity()
    c = QuerySpec.from_dict(
        {"kind": "synthetic", "column": "URL", "predicate": {"op": "contains", "value": "h"}}
    )
    assert a.identity() != c.identity()


def test_synthetic_builder_and_op():
    s = QuerySpec.synthetic("URL", contains("google"))
    assert s.predicate == {"op": "contains", "value": "google"}
    assert s.op == "contains"

    multi = QuerySpec.synthetic("URL", multi_contains(["a", "b"]))
    assert multi.op == "multi-contains"
    assert multi.predicate["values"] == ["a", "b"]


def test_predicate_matches_mirrors_matcher():
    assert predicate_matches(prefix("htt"), "http://x")
    assert not predicate_matches(prefix("zzz"), "http://x")
    assert predicate_matches(contains("oo"), "food")
    # multi-contains is ordered: `%a%b%` = a then b.
    assert predicate_matches(multi_contains(["a", "b"]), "xaybz")
    assert not predicate_matches(multi_contains(["a", "b"]), "xbya")
    assert not predicate_matches(multi_contains(["a", "b"]), "a")


def test_manifest_rejects_bad_values(tmp_path):
    bad = tmp_path / "b.toml"
    bad.write_text(
        '[[binary]]\nname="x"\nbin_path="p"\nlang="rust"\n'
        'modes=["warp"]\nformats=["parquet"]\nkinds=["synthetic"]\n'
    )
    with pytest.raises(ValueError):
        load_manifest(bad)


def test_manifest_parses_codec_and_bin_fields(tmp_path):
    toml = tmp_path / "m.toml"
    toml.write_text(
        '[[binary]]\nname="e"\nbin="target/release/e"\nlang="rust"\n'
        'modes=["in-mem"]\nformats=["raw"]\nkinds=["synthetic"]\ncodec="fsst"\n'
    )
    (b,) = load_manifest(toml)
    assert b.codec == "fsst"
    assert b.extra_args() == ["--codec", "fsst"]
    assert b.binary_path(tmp_path).name == "e"
