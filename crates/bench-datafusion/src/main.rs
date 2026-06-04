//! `bench-datafusion` — Parquet/Arrow baseline vs Vortex pushdown.
//!
//! Four (format × mode) cells, all answering the same
//! `SELECT count(*) FROM hits WHERE <predicate>` so results are directly
//! comparable and cross-checkable against the other engines:
//!
//! | format  | mode       | how data is fed                                   |
//! |---------|------------|---------------------------------------------------|
//! | parquet | in-mem     | decode once into a `MemTable`, query K×            |
//! | parquet | full-query | re-scan the Parquet file each query (fresh ctx)   |
//! | vortex  | in-mem     | `open_buffer` (resident compressed bytes), query K×|
//! | vortex  | full-query | re-open the Vortex file each query (fresh ctx)     |
//!
//! Pushdown is detected from the physical plan: for Vortex, the predicate is
//! pushed onto the compressed array iff DataFusion did not insert a `FilterExec`
//! above the scan (treatment); otherwise it fell back to decode (control).

use std::sync::Arc;

use anyhow::{bail, Result};
use clap::Parser;
use datafusion::arrow::array::{Array, Int64Array, StringArray};
use datafusion::arrow::datatypes::SchemaRef;
use datafusion::datasource::MemTable;
use datafusion::prelude::{ParquetReadOptions, SessionContext};

use bench_core::{checksum, synthetic_to_sql, BenchArgs, BenchOutput, Format, Mode, QuerySpec};

const TABLE: &str = "hits";

struct Outcome {
    rows: u64,
    result_rows: u64,
    iters_ns: Vec<u64>,
    load_ns: u64,
    decompress_ns: u64,
    in_memory_bytes: u64,
    pushdown: bool,
    plan: String,
}

fn main() -> Result<()> {
    let args = BenchArgs::parse();
    // Single-threaded everywhere for a fair comparison against the single-threaded
    // Arrow baseline and the string-codec engines (see `new_ctx`).
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    rt.block_on(run(args))
}

/// A DataFusion context pinned to a single partition (one thread), so latency is
/// comparable to the single-threaded Arrow/codec engines rather than fanned out
/// across cores.
fn new_ctx() -> SessionContext {
    use datafusion::prelude::SessionConfig;
    SessionContext::new_with_config(SessionConfig::new().with_target_partitions(1))
}

async fn run(args: BenchArgs) -> Result<()> {
    let spec = args.read_spec()?;
    let sql = build_sql(&spec)?;

    let outcome = match (args.format, args.mode) {
        (Format::Parquet, Mode::InMem) => parquet_in_mem(&args, &sql).await?,
        (Format::Parquet, Mode::FullQuery) => parquet_full_query(&args, &sql).await?,
        (Format::Vortex, Mode::InMem) => vortex_in_mem(&args, &sql).await?,
        (Format::Vortex, Mode::FullQuery) => vortex_full_query(&args, &sql).await?,
        (Format::Raw, _) => bail!("bench-datafusion does not support --format raw (.strings)"),
    };

    let out = BenchOutput {
        engine: "datafusion".into(),
        format: args.format.as_str().into(),
        mode: args.mode.as_str().into(),
        label: spec.label(),
        op: spec.op(),
        column: spec.column(),
        rows: outcome.rows,
        result_rows: outcome.result_rows,
        result_checksum: checksum(outcome.result_rows),
        file_bytes: bench_core::file_bytes(&args.input),
        in_memory_bytes: outcome.in_memory_bytes,
        load_ns: outcome.load_ns,
        decompress_ns: outcome.decompress_ns,
        pushdown: outcome.pushdown,
        plan: outcome.plan,
        iters_ns: outcome.iters_ns,
        ..Default::default()
    };
    out.print()
}

/// Synthetic ops become an escaped LIKE count query; real specs use their SQL.
fn build_sql(spec: &QuerySpec) -> Result<String> {
    match spec {
        QuerySpec::Synthetic(s) => synthetic_to_sql(s, TABLE),
        QuerySpec::Real(r) => Ok(r.sql.clone()),
    }
}

// --------------------------------------------------------------------------- //
// Parquet
// --------------------------------------------------------------------------- //
async fn parquet_in_mem(args: &BenchArgs, sql: &str) -> Result<Outcome> {
    let ctx = new_ctx();
    let path = args.input.to_string_lossy().to_string();

    // Decode once into memory (this decode is the decompression cost).
    let load = bench_core::Timer::start();
    let df = ctx
        .read_parquet(path.as_str(), ParquetReadOptions::default())
        .await?;
    let schema: SchemaRef = Arc::new(df.schema().as_arrow().clone());
    let batches = df.collect().await?;
    let in_memory_bytes: u64 = batches
        .iter()
        .map(|b| b.get_array_memory_size() as u64)
        .sum();
    let mem = MemTable::try_new(schema, vec![batches])?;
    ctx.register_table(TABLE, Arc::new(mem))?;
    let load_ns = load.elapsed_ns();

    let rows = count(&ctx, &format!("SELECT count(*) FROM {TABLE}")).await?;
    let plan = physical_plan(&ctx, sql).await?;
    let (iters_ns, result_rows) = measure(&ctx, sql, args.iterations).await?;

    Ok(Outcome {
        rows,
        result_rows,
        iters_ns,
        load_ns,
        decompress_ns: load_ns,
        in_memory_bytes,
        pushdown: false,
        plan: tag(args, plan),
    })
}

async fn parquet_full_query(args: &BenchArgs, sql: &str) -> Result<Outcome> {
    let path = args.input.to_string_lossy().to_string();
    let mut iters_ns = Vec::with_capacity(args.iterations);
    let mut result_rows = 0u64;

    // Each iteration re-scans the file end-to-end via a fresh context.
    for _ in 0..args.iterations {
        let ctx = new_ctx();
        ctx.register_parquet(TABLE, &path, ParquetReadOptions::default())
            .await?;
        let t = bench_core::Timer::start();
        result_rows = count(&ctx, sql).await?;
        iters_ns.push(t.elapsed_ns());
    }

    let ctx = new_ctx();
    ctx.register_parquet(TABLE, &path, ParquetReadOptions::default())
        .await?;
    let rows = count(&ctx, &format!("SELECT count(*) FROM {TABLE}")).await?;
    let plan = physical_plan(&ctx, sql).await?;

    Ok(Outcome {
        rows,
        result_rows,
        iters_ns,
        load_ns: 0,
        decompress_ns: 0,
        in_memory_bytes: 0,
        pushdown: false,
        plan: tag(args, plan),
    })
}

// --------------------------------------------------------------------------- //
// Vortex (via vortex-datafusion v2::VortexTable)
// --------------------------------------------------------------------------- //
async fn vortex_in_mem(args: &BenchArgs, sql: &str) -> Result<Outcome> {
    use vortex::file::OpenOptionsSessionExt;
    use vortex::io::session::RuntimeSessionExt;
    use vortex::session::VortexSession;
    use vortex::VortexSessionDefault;
    use vortex_datafusion::v2::VortexTable;

    let load = bench_core::Timer::start();
    // Read the whole file into a resident, *compressed* in-memory buffer.
    let bytes = std::fs::read(&args.input)?;
    let in_memory_bytes = bytes.len() as u64;
    // We are inside `rt.block_on`, so the current tokio handle is available.
    let session = VortexSession::default().with_tokio();
    let file = session.open_options().open_buffer(bytes)?;
    let rows = file.row_count();
    let arrow_schema = vortex_arrow_schema(&session, &file)?;
    let data_source = file.data_source()?;
    let table = VortexTable::new(data_source, session.clone(), arrow_schema);

    let ctx = new_ctx();
    ctx.register_table(TABLE, Arc::new(table))?;
    let load_ns = load.elapsed_ns();

    let plan = physical_plan(&ctx, sql).await?;
    let pushdown = vortex_pushdown(&plan);
    let (iters_ns, result_rows) = measure(&ctx, sql, args.iterations).await?;

    Ok(Outcome {
        rows,
        result_rows,
        iters_ns,
        load_ns,
        decompress_ns: 0, // compute-on-compressed: no decode to Arrow
        in_memory_bytes,
        pushdown,
        plan: tag(args, plan),
    })
}

async fn vortex_full_query(args: &BenchArgs, sql: &str) -> Result<Outcome> {
    use vortex::file::OpenOptionsSessionExt;
    use vortex::io::session::RuntimeSessionExt;
    use vortex::session::VortexSession;
    use vortex::VortexSessionDefault;
    use vortex_datafusion::v2::VortexTable;

    let session = VortexSession::default().with_tokio();
    let path = &args.input;

    let mut iters_ns = Vec::with_capacity(args.iterations);
    let mut result_rows = 0u64;
    let mut rows = 0u64;
    let mut plan = String::new();

    for i in 0..args.iterations {
        let ctx = new_ctx();
        let file = session.open_options().open_path(path).await?;
        if i == 0 {
            rows = file.row_count();
        }
        let arrow_schema = vortex_arrow_schema(&session, &file)?;
        let data_source = file.data_source()?;
        let table = VortexTable::new(data_source, session.clone(), arrow_schema);
        ctx.register_table(TABLE, Arc::new(table))?;
        if i == 0 {
            plan = physical_plan(&ctx, sql).await?;
        }
        let t = bench_core::Timer::start();
        result_rows = count(&ctx, sql).await?;
        iters_ns.push(t.elapsed_ns());
    }

    let pushdown = vortex_pushdown(&plan);
    Ok(Outcome {
        rows,
        result_rows,
        iters_ns,
        load_ns: 0,
        decompress_ns: 0,
        in_memory_bytes: 0,
        pushdown,
        plan: tag(args, plan),
    })
}

fn vortex_arrow_schema(
    session: &vortex::session::VortexSession,
    file: &vortex::file::VortexFile,
) -> Result<SchemaRef> {
    use vortex::array::arrow::ArrowSessionExt;
    let schema = session.arrow().to_arrow_schema(file.dtype())?;
    Ok(Arc::new(schema))
}

// --------------------------------------------------------------------------- //
// Shared helpers
// --------------------------------------------------------------------------- //
async fn measure(ctx: &SessionContext, sql: &str, iterations: usize) -> Result<(Vec<u64>, u64)> {
    let mut iters_ns = Vec::with_capacity(iterations);
    let mut result_rows = 0u64;
    for _ in 0..iterations {
        let t = bench_core::Timer::start();
        result_rows = count(ctx, sql).await?;
        iters_ns.push(t.elapsed_ns());
    }
    Ok((iters_ns, result_rows))
}

/// Run a `count(*)`-style query and return the single integer result.
async fn count(ctx: &SessionContext, sql: &str) -> Result<u64> {
    let batches = ctx.sql(sql).await?.collect().await?;
    for b in &batches {
        if b.num_rows() == 0 {
            continue;
        }
        let arr = b
            .column(0)
            .as_any()
            .downcast_ref::<Int64Array>()
            .ok_or_else(|| anyhow::anyhow!("expected Int64 count column"))?;
        return Ok(arr.value(0).max(0) as u64);
    }
    bail!("query returned no rows: {sql}")
}

/// Capture the physical plan text via `EXPLAIN`.
async fn physical_plan(ctx: &SessionContext, sql: &str) -> Result<String> {
    let batches = ctx.sql(&format!("EXPLAIN {sql}")).await?.collect().await?;
    let mut out = String::new();
    for b in &batches {
        let kinds = b.column(0).as_any().downcast_ref::<StringArray>();
        let plans = b.column(1).as_any().downcast_ref::<StringArray>();
        if let (Some(kinds), Some(plans)) = (kinds, plans) {
            for i in 0..b.num_rows() {
                if kinds.value(i) == "physical_plan" {
                    out.push_str(plans.value(i));
                    out.push('\n');
                }
            }
        }
    }
    Ok(out.trim().to_string())
}

/// Vortex pushed the predicate onto compressed data iff no FilterExec remains.
fn vortex_pushdown(plan: &str) -> bool {
    !plan.contains("FilterExec")
}

/// Prefix the recorded plan with a short pushdown tag for human reading.
fn tag(args: &BenchArgs, plan: String) -> String {
    let kind = match args.format {
        Format::Vortex if vortex_pushdown(&plan) => "[vortex-native pushdown]",
        Format::Vortex => "[vortex decode fallback]",
        Format::Parquet => "[parquet baseline]",
        Format::Raw => "[raw]",
    };
    let first = plan.lines().next().unwrap_or("").trim().to_string();
    format!("{kind} {first}")
}
