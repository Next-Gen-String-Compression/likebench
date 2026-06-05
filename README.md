# likebench — LIKE-pushdown on compressed columnar strings

**Thesis.** `LIKE`-family predicates evaluated directly on *compressed* columnar
string data (Vortex / FSST) beat the same predicates on *decompressed* data
(Arrow). likebench measures compression ratio, compress/decompress speed, and the
predicate ops `prefix`, `suffix`, `contains`, `multi-contains`, `expr`.

## Design

**Binaries are dumb executors.** Each engine binary loads data once, runs *one*
query *K* times in-process, and prints raw per-iteration timings as a single JSON
object. **Python owns all the logic**: data generation/download, query mining,
warmup/iteration/aggregation, correctness + pushdown checks, and the plots.
Because every binary speaks the same CLI/JSON contract (this crate,
`bench-core`), Rust and C++ engines are interchangeable.

```
bench-<engine> --format parquet|vortex|raw --input <path> \
  --mode in-mem|full-query --query-spec '<json>' --iterations <K> --output json
```

The harness reads one manifest (`benchmarks.toml`) to learn which engines exist;
adding an engine is "drop a crate + add a row". A synthetic query spec carries a
small predicate **AST** over a column — `prefix` / `suffix` / `contains`, or a
`multi-contains` (`%a%b%…%` — each value in order). The AST is the source of
truth: SQL engines *convert* it to `... LIKE '<pattern>' ESCAPE '\'` (escaping +
wildcards added in one place), while scan engines (e.g. bench-arrow) and the C++
codecs *walk* it directly with native kernels (`starts_with`/`ends_with`/ordered
`memmem`). Nothing parses `LIKE` back, both sides evaluate the identical
predicate, and a shared checksum gates cross-engine correctness.

## Layout (built up over the PR stack)

```
crates/bench-core   the CLI/JSON/checksum/Matcher contract (this layer)
crates/tpch-gen     offline TPC-H data source
crates/convert      transcode to Parquet/Vortex; compression metrics
crates/bench-*      engines (datafusion, arrow, onpair, …)
harness/            spec, manifest, datasets, mining, runner, plots
run.py              entrypoint: generate -> convert -> mine -> run -> plot
```

Run it: `./setup.sh && python run.py`.
