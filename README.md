# likebench — LIKE-pushdown on compressed columnar strings

**Thesis.** `LIKE`-family predicates evaluated directly on *compressed* columnar
string data (Vortex / FSST) beat the same predicates on *decompressed* data
(Arrow). We measure compression speed, compression ratio, decompression speed,
and the predicate ops `prefix`, `suffix`, `contains`, `multi-contains`, `expr`.

**Dataset.** ClickBench `hits`. String columns of interest: `URL`, `Title`,
`Referer`, `SearchPhrase`.

```bash
./setup.sh && python run.py            # clone-and-verify, one command
```

A reviewer can clone this repo, run the two commands above, and reproduce the
table + plots in `results/`.

---

## Design in one paragraph

**Binaries are dumb executors.** Each engine binary loads data once, runs *one*
query *K* times in-process, and prints raw per-iteration timings as a single
JSON object. No statistics, no warmup logic, no query catalog. **Python owns all
the logic**: warmup/iteration/aggregation, data download + cache, query mining,
correctness + pushdown checks, and the table + plots. Because every binary
speaks the same CLI/JSON contract, the harness never knows (or cares) which
language produced a binary — Rust and C++ engines are interchangeable.

## Two orthogonal axes (kept strictly separate)

* `--mode in-mem | full-query` — **how data is fed.**
  * `in-mem`: load the target column(s) once into the format's native in-memory
    representation, then run the spec *K* times timing *compute only*.
  * `full-query`: run the spec against the file each iteration, timing
    IO + scan + decode + compute end-to-end.
* `kind` (in the query spec) — **what predicate runs.** `synthetic` (a mined op)
  vs `real` (a ClickBench SQL statement).

Default pairing is `synthetic → in-mem` and `real → full-query`, but they stay
independent: running one real SQL query in *both* modes attributes a Vortex win
to *compute-on-compressed* vs *scan / segment-pruning*.

## What `--format` means (the actual comparison)

In `in-mem` mode the same rows are resident in two representations and the
identical op runs over both:

| `--format` | resident representation | op runs on | `decompress_ns` | role |
|---|---|---|---|---|
| `parquet`  | decoded Arrow array | Arrow | full decode cost | **decompressed baseline** |
| `vortex`   | compressed Vortex array | Vortex kernels | ≈ 0 | **pushdown path** |

The synthetic spec encodes the *intent* (`contains "google"`), not a raw
`%google%` pattern. That lets the decompressed baseline use its **strongest
kernel** (`memmem`/SIMD substring, `starts_with`/`ends_with`) instead of being
strawmanned through a generic `LIKE`. Each binary escapes LIKE metacharacters
and adds wildcards itself, correctly per engine, so cross-engine checksums agree.

---

## Repository layout

```
likebench/
  setup.sh                  one-shot installer + build
  run.py                    thin entrypoint: setup -> download -> convert -> mine -> run -> plot
  benchmarks.toml           engine manifest (one row per binary)
  pyproject.toml            uv project (polars, matplotlib)
  harness/
    manifest.py             parse benchmarks.toml
    data.py                 download/cache/generate ClickBench hits  (nano|sample|full)
    convert.py              drive the `convert` binary: csv/parquet -> parquet -> vortex
    mine_queries.py         mine predicates bucketed by selectivity; emit queries/clickbench.json
    runner.py               run the matrix; discard warmup; aggregate; correctness + pushdown
    plots.py                render the four plots
    spec.py                 shared dataclasses for query specs / results
  crates/                   cargo workspace
    bench-core/             shared CLI + JSON contract + LIKE escaping
    convert/                compression/transcode tool (where ratio + encode speed are measured)
    bench-arrow/            decode -> Arrow -> strongest scan kernel (decompressed baseline)
    bench-datafusion/       DataFusion + vortex-datafusion (pushdown vs parquet)
    bench-duckdb/           duckdb crate + LOAD vortex (heavy; disabled by default)
  cpp/                      later: bench-duckdb-native, bench-baseline
  scripts/
    build_vortex_duckdb_extension.sh   build the DuckDB `vortex` extension from source
  queries/clickbench.json   mined synthetic specs + real ClickBench LIKE SQL
  data/cache/               downloaded + converted artifacts (gitignored)
  results/                  results.json, table.md, table.csv, metadata.json, plots/
```

## The uniform binary contract

```
bench-<engine> \
  --format parquet|vortex \
  --input <path> \
  --mode in-mem|full-query \
  --query-spec '<json>'   # inline JSON, or '-' to read JSON from stdin
  --iterations <K>        # Python sets K = warmup + measured
  [--explain]             # also emit the physical plan + pushdown flag
  --output json
```

Query specs are **self-describing** (they describe the op, not a pointer):

```json
{ "kind":"synthetic", "op":"contains",      "column":"URL",   "value":"google", "label":"contains_p01_003" }
{ "kind":"synthetic", "op":"prefix",        "column":"URL",   "value":"http://" }
{ "kind":"synthetic", "op":"suffix",        "column":"Title", "value":".html" }
{ "kind":"synthetic", "op":"multicontains", "column":"URL",   "values":["google","ads"], "mode":"all" }
{ "kind":"synthetic", "op":"expr",          "column":"URL",   "predicate":{"and":[{"prefix":"http"},{"contains":"google"}]} }
{ "kind":"real",      "sql":"SELECT count(*) FROM hits WHERE URL LIKE '%google%'", "label":"clickbench_q28" }
```

Each binary emits one JSON object on stdout:

```json
{
  "engine":"datafusion","format":"vortex","mode":"in-mem",
  "label":"contains_p01_003","op":"contains","column":"URL",
  "rows":99997497,"result_rows":41233,"result_checksum":"0x9af3",
  "file_bytes":1234567890,"in_memory_bytes":7654321000,
  "load_ns":812334000,"decompress_ns":0,
  "pushdown":true,"plan":"FilterExec: like(...) [vortex-native]",
  "iters_ns":[ /* K values */ ]
}
```

The converter binary is where compression metrics are measured:

```
convert --input <path> --input-format auto|csv|json|parquet \
        --output <path> --output-format parquet|vortex \
        --compression <codec> --report json
# emits: { encode_ns, input_bytes, uncompressed_bytes, output_bytes, ratio, rows }
```

## Reviewer-defensibility checks (baked in)

* **Correctness.** `result_rows` *and* `result_checksum` must agree across every
  engine×format for the same query. The harness **fails loudly** on any mismatch.
* **Pushdown proof.** Each run captures `EXPLAIN` and sets a `pushdown` flag.
  Results are labelled **treatment** (executed on compressed data) vs **control**
  (fell back to decode). Vortex does not push down every predicate, so a result
  without this label is meaningless.

## Plots (`results/plots/`)

1. **Headline** — latency vs selectivity, grouped by engine×format (compressed
   pushdown should win hardest at low selectivity).
2. Compression ratio per column per format.
3. Compression + decompression speed (bars).
4. Throughput (rows/s).

---

## Scales

`run.py --scale {nano,sample,full}`:

| scale  | source | rows | network | use |
|--------|--------|------|---------|-----|
| `nano`   | generated, seeded | ~50k | none | CI + smoke test |
| `sample` | one real ClickBench partition | ~1M | ~150 MB | default, quick real run |
| `full`   | full ClickBench `hits` | ~100M | ~14 GB | paper numbers |

## Extending: add an engine in two steps

1. Drop a crate under `crates/<name>` (or `cpp/<name>`) that honours the CLI/JSON
   contract above.
2. Add one `[[binary]]` row to `benchmarks.toml`.

No Python changes. `runner.py` reads the manifest.

## Reproducibility

Deterministic seeded mining, pinned Rust/Python deps, `cargo build --release`.
`run.py` records machine info, engine/crate versions, and the dataset hash into
`results/metadata.json`. End to end: `./setup.sh && python run.py`.

## Engine status

| binary | lang | status |
|---|---|---|
| `bench-arrow` | Rust | implemented (decompressed baseline) |
| `bench-datafusion` | Rust | implemented (DataFusion + vortex-datafusion) |
| `bench-duckdb` | Rust | implemented; disabled by default (heavy native build + a locally-built `vortex` extension — see below) |
| `bench-duckdb-native` | C++ | planned |
| `bench-baseline` | C++ | planned (hand-rolled FSST decode + `memmem`) |

> **Heads up for reviewers running this:** the `full` scale needs ~14 GB of disk
> for the download plus the Vortex/Parquet re-encodings. `bench-datafusion`
> compiles a large dependency tree on first build (DataFusion + Vortex).
> `bench-duckdb` additionally compiles DuckDB from source; see the next section
> for its `vortex` extension requirement.

### Running `bench-duckdb` with Vortex

The DuckDB `vortex` **community** extension (`INSTALL vortex FROM community`)
lags upstream: at the time of writing it is published only for DuckDB
v1.2.2–v1.4.2, and even those builds predate the `vortex.variant` array encoding
emitted by the Vortex ≥ 0.74 writer this repo uses. So on the DuckDB version the
`duckdb` crate bundles (v1.5.3) the community install **404s**, and on v1.4.x it
loads but cannot read files written by `convert`. The Parquet path is unaffected.

To run `bench-duckdb`'s Vortex path, build a matching extension from source and
point the binary at it:

```bash
ext=$(scripts/build_vortex_duckdb_extension.sh)   # builds duckdb-vortex HEAD (DuckDB 1.5.3 + current Vortex)
export VORTEX_DUCKDB_EXTENSION="$ext"              # bench-duckdb LOADs this instead of INSTALL FROM community
# enable bench-duckdb in benchmarks.toml, then:
python run.py --scale sample --engines bench-duckdb
```

When `VORTEX_DUCKDB_EXTENSION` is set, `bench-duckdb` opens DuckDB with
`allow_unsigned_extensions` and `LOAD`s that file; unset, it falls back to the
community registry. The same build also yields a `duckdb` CLI with the extension
statically linked (`<build>/release/duckdb`) for ad-hoc `read_vortex(...)`
queries — including the full ClickBench `LIKE` queries (Q20–Q22), where the
predicate is pushed into the Vortex scan and beats the Parquet baseline ~5×.
