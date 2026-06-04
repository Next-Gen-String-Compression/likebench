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
  cpp/                      bench-compress-cpp (FSST/FSST12/Dictionary/LZ4); later: bench-duckdb-native
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

## Datasets

ClickBench is the default, but likebench can benchmark **arbitrary string data**
— the analogue of CompressionBenchmark pointing at any DuckDB database. Pick a
source with `--dataset`; string columns are auto-discovered unless `--columns`
is given (the likebench version of scanning `information_schema` for VARCHAR
columns).

| `--dataset` | source | columns |
|---|---|---|
| `clickbench` (default) | ClickBench `hits` (see Scales) | `URL Title Referer SearchPhrase` |
| `parquet:<path\|url>` | any Parquet file | auto-discovered Utf8 columns |
| `duckdb:<path>[#table]` | a DuckDB database (richest VARCHAR table, or `#table`) | its VARCHAR columns |
| `hf:<repo>[#shard]` | a HuggingFace dataset (datasets-server Parquet) | auto-discovered |
| `imdb` | alias for `hf:stanfordnlp/imdb#train` | `text` |

```bash
python run.py --dataset duckdb:/data/imdb.duckdb            # all VARCHAR cols
python run.py --dataset duckdb:/data/tpch.duckdb#lineitem --columns l_comment
python run.py --dataset hf:stanfordnlp/imdb --formats raw --engines bench-fsst bench-onpair
python run.py --dataset parquet:/data/logs.parquet --columns auto
```

`--scale {nano,sample}` head-caps generic sources for a quick run; `full` uses
everything. The `duckdb:` source needs the optional `duckdb` package
(`uv sync --extra duckdb`). TPC-H and Kaggle are just DuckDB files you point at
with `duckdb:<path>` — exactly CompressionBenchmark's workflow.

## Vortex encodings

**All Vortex runs in this repo use Vortex's `unstable_encodings`.** The workspace
pins `vortex`/`vortex-file`/`vortex-btrblocks` with the `["zstd",
"unstable_encodings"]` features, so the default Btrblocks `ALL_SCHEMES` cascade
(used by both the `convert` writer and the `bench-datafusion` reader) includes
Vortex's unstable string schemes — notably its own Rust **`OnPairScheme`** and
**`ZstdBuffersScheme`** (buffer-level Zstd). This materially improves Vortex's
compression ratio over the stable-only default and is the configuration every
ratio/latency number here is measured under. Any new Vortex engine added to the
workspace inherits these features automatically; do not add a Vortex engine that
disables them, or its numbers won't be comparable.

## Extending: add an engine in two steps

1. Drop a crate under `crates/<name>` (or `cpp/<name>`) that honours the CLI/JSON
   contract above.
2. Add one `[[binary]]` row to `benchmarks.toml`.

No Python changes. `runner.py` reads the manifest.

## Reproducibility

Deterministic seeded mining, pinned Rust/Python deps, `cargo build --release`.
`run.py` records machine info, engine/crate versions, and the dataset hash into
`results/metadata.json`. End to end: `./setup.sh && python run.py`.

## Standalone string codecs (`--format raw`)

Beyond the query engines, likebench benchmarks *raw string-compression schemes*
ported from CompressionBenchmark. These engines don't read Parquet/Vortex; they
read a dependency-free `.strings` column (the STRZ interchange file, a direct
port of CompressionBenchmark's `StringCollector`: offsets + concatenated bytes,
emitted per column by `convert.py`). Each one compresses the column itself, then
for every iteration **decodes + runs the synthetic matcher** — so its
`result_rows`/`result_checksum` join the same cross-engine correctness gate as
the Vortex/Arrow engines — while additionally reporting compression ratio and
encode/decode speed. They form the **`codec` group**: a "compressed storage +
decompress-then-scan" control for the pushdown thesis.

* **`bench-compress-cpp`** (C++) — one binary, codec via `--codec`: `fsst`,
  `fsst12`, `dictionary`, `lz4`. DuckDB/Arrow-free; vendors fsst, fsst12, lz4,
  robin_hood, nlohmann/json under `cpp/external/`.
* **`bench-onpair`** (Rust) — SpiralDB-adjacent OnPair (`onpair_rs`, the
  algorithm author's port): `--codec onpair | onpair16`. Tuned for random
  access, so it also reports `decompress_random_ns` (point-access latency).

## Engine status

| binary | lang | status |
|---|---|---|
| `bench-arrow` | Rust | implemented (decompressed baseline) |
| `bench-datafusion` | Rust | implemented (DataFusion + vortex-datafusion) |
| `bench-duckdb` | Rust | implemented; disabled by default (heavy native build + runtime `vortex` community extension) |
| `bench-compress-cpp` | C++ | implemented (FSST, FSST12, Dictionary, LZ4 — `--format raw`) |
| `bench-onpair` | Rust | implemented (OnPair / OnPair16 — `--format raw`) |
| `bench-duckdb-native` | C++ | planned |

> **Heads up for reviewers running this:** the `full` scale needs ~14 GB of disk
> for the download plus the Vortex/Parquet re-encodings. `bench-datafusion`
> compiles a large dependency tree on first build (DataFusion + Vortex).
> `bench-duckdb` additionally compiles DuckDB from source and loads the `vortex`
> community extension at runtime; enable it in `benchmarks.toml` once that
> extension is available for your platform.
