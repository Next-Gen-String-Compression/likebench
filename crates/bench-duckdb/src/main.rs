//! `bench-duckdb` — DuckDB engine via the `duckdb` crate.
//!
//! Parquet path uses `read_parquet`; the Vortex path uses `read_vortex` from the
//! `vortex` extension, loaded at runtime. A `hits` relation is created once per
//! run so both synthetic (`FROM hits`) and real SQL work uniformly:
//!
//! * `in-mem` — `CREATE TABLE hits AS SELECT * FROM read_X(...)` (materialize
//!   into DuckDB once), then query K×.
//! * `full-query` — `CREATE VIEW hits AS SELECT * FROM read_X(...)`, so each
//!   query re-scans the file (predicate pushdown applies).
//!
//! NOTE: this binary is disabled by default in `benchmarks.toml`. It compiles
//! DuckDB from source (the `bundled` feature). The Vortex path needs the `vortex`
//! extension; because the community build lags DuckDB (none for 1.5.x), point
//! `VORTEX_DUCKDB_EXTENSION` at a locally-built `.duckdb_extension` — see
//! `scripts/build_vortex_duckdb_extension.sh`.

use anyhow::{Context, Result};
use clap::Parser;
use duckdb::{Config, Connection};

use bench_core::{checksum, synthetic_to_sql, BenchArgs, BenchOutput, Format, Mode, QuerySpec};

const TABLE: &str = "hits";

fn main() -> Result<()> {
    let args = BenchArgs::parse();
    let spec = args.read_spec()?;
    let sql = match &spec {
        QuerySpec::Synthetic(s) => synthetic_to_sql(s, TABLE),
        QuerySpec::Real(r) => Ok(r.sql.clone()),
    }?;

    // allow_unsigned_extensions lets us LOAD a locally-built (unsigned) vortex
    // extension. See `load_vortex_extension` for why that is the reliable path.
    let config = Config::default().allow_unsigned_extensions()?;
    let conn = Connection::open_in_memory_with_flags(config)?;
    if args.format == Format::Vortex {
        load_vortex_extension(&conn)?;
    }

    let path = args.input.to_string_lossy().to_string();
    let reader = match args.format {
        Format::Parquet => format!("read_parquet('{path}')"),
        Format::Vortex => format!("read_vortex('{path}')"),
    };

    // Build the `hits` relation. in-mem materializes; full-query is a view.
    let load = bench_core::Timer::start();
    let ddl = match args.mode {
        Mode::InMem => format!("CREATE TABLE {TABLE} AS SELECT * FROM {reader}"),
        Mode::FullQuery => format!("CREATE VIEW {TABLE} AS SELECT * FROM {reader}"),
    };
    conn.execute_batch(&ddl)?;
    let load_ns = load.elapsed_ns();

    let rows = count(&conn, &format!("SELECT count(*) FROM {TABLE}"))?;
    let plan = explain(&conn, &sql).unwrap_or_default();
    let pushdown = args.format == Format::Vortex && plan_shows_pushdown(&plan);

    let mut iters_ns = Vec::with_capacity(args.iterations);
    let mut result_rows = 0u64;
    for _ in 0..args.iterations {
        let t = bench_core::Timer::start();
        result_rows = count(&conn, &sql)?;
        iters_ns.push(t.elapsed_ns());
    }

    let decompress_ns = match args.mode {
        Mode::InMem => load_ns, // materialize == decode into DuckDB
        Mode::FullQuery => 0,   // decode folded into each measured iteration
    };

    let out = BenchOutput {
        engine: "duckdb".into(),
        format: args.format.as_str().into(),
        mode: args.mode.as_str().into(),
        label: spec.label(),
        op: spec.op(),
        column: spec.column(),
        rows,
        result_rows,
        result_checksum: checksum(result_rows),
        file_bytes: bench_core::file_bytes(&args.input),
        in_memory_bytes: 0,
        load_ns,
        decompress_ns,
        pushdown,
        plan: format!(
            "{} {}",
            pushdown_tag(args.format, pushdown),
            plan.lines().next().unwrap_or("")
        ),
        iters_ns,
    };
    out.print()
}

/// Make the `vortex` extension available before a Vortex-format run.
///
/// Prefer a locally-built loadable extension pointed to by the
/// `VORTEX_DUCKDB_EXTENSION` env var (path to a `vortex.duckdb_extension`); this
/// is the reliable path because the DuckDB *community* build of the extension
/// lags upstream — there is no published build for DuckDB 1.5.x, and older
/// builds cannot read files written by current Vortex (the `vortex.variant`
/// encoding). Build a matching extension from source with
/// `scripts/build_vortex_duckdb_extension.sh`. If the env var is unset we fall
/// back to the community registry (works only where a build is published).
fn load_vortex_extension(conn: &Connection) -> Result<()> {
    match std::env::var("VORTEX_DUCKDB_EXTENSION") {
        Ok(path) if !path.is_empty() => {
            let esc = path.replace('\'', "''");
            conn.execute_batch(&format!("LOAD '{esc}';"))
                .with_context(|| format!("failed to LOAD local vortex extension {path:?}"))
        }
        _ => {
            conn.execute_batch(
                "SET autoinstall_known_extensions=true; SET autoload_known_extensions=true;",
            )
            .ok();
            conn.execute_batch("INSTALL vortex FROM community; LOAD vortex;")
                .context(
                    "failed to LOAD the duckdb `vortex` community extension. No community build \
                 exists for DuckDB 1.5.x; build one from source and point \
                 VORTEX_DUCKDB_EXTENSION at it (see scripts/build_vortex_duckdb_extension.sh).",
                )
        }
    }
}

fn count(conn: &Connection, sql: &str) -> Result<u64> {
    let n: i64 = conn
        .query_row(sql, [], |row| row.get(0))
        .with_context(|| format!("query failed: {sql}"))?;
    Ok(n.max(0) as u64)
}

fn explain(conn: &Connection, sql: &str) -> Result<String> {
    let mut stmt = conn.prepare(&format!("EXPLAIN {sql}"))?;
    let mut rows = stmt.query([])?;
    let mut plan = String::new();
    while let Some(row) = rows.next()? {
        // EXPLAIN yields (explain_key, explain_value).
        if let Ok(v) = row.get::<_, String>(1) {
            plan.push_str(&v);
            plan.push('\n');
        }
    }
    Ok(plan)
}

/// Heuristic: a pushed-down filter shows up attached to the scan ("Filters:")
/// rather than as a separate FILTER operator.
fn plan_shows_pushdown(plan: &str) -> bool {
    let p = plan.to_uppercase();
    p.contains("FILTERS:") || (p.contains("VORTEX") && !p.contains("\nFILTER"))
}

fn pushdown_tag(fmt: Format, pushdown: bool) -> &'static str {
    match fmt {
        Format::Vortex if pushdown => "[vortex-native pushdown]",
        Format::Vortex => "[vortex decode fallback]",
        Format::Parquet => "[parquet baseline]",
    }
}
