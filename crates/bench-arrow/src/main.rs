//! `bench-arrow` — the pure decompressed baseline.
//!
//! Decodes a single-column Parquet file into Arrow (this decode IS the
//! `decompress_ns` cost), keeps it resident, then for each iteration runs the
//! *strongest* native scan kernel for the op: `str::starts_with`/`ends_with`
//! for prefix/suffix and prebuilt `memchr::memmem` (SIMD substring) for
//! contains. No generic `LIKE` engine is involved — that would be a strawman.
//!
//! Supports: `--mode in-mem --format parquet` with `kind == synthetic`.

use std::fs::File;

use anyhow::{bail, Result};
use arrow::array::{Array, ArrayRef, LargeStringArray, StringArray, StringViewArray};
use clap::Parser;
use memchr::memmem::Finder;

use bench_core::{checksum, BenchArgs, BenchOutput, Format, Matcher, Mode, QuerySpec, TermKind};

/// A predicate compiled with prebuilt substring finders for the hot loop.
enum Compiled {
    Prefix(String),
    Suffix(String),
    Contains(Finder<'static>),
    Multi { finders: Vec<Finder<'static>>, all: bool },
    Expr { terms: Vec<CompiledTerm>, all: bool },
}

enum CompiledTerm {
    Prefix(String),
    Suffix(String),
    Contains(Finder<'static>),
}

impl CompiledTerm {
    fn from(kind: TermKind, v: &str) -> Self {
        match kind {
            TermKind::Prefix => CompiledTerm::Prefix(v.to_string()),
            TermKind::Suffix => CompiledTerm::Suffix(v.to_string()),
            TermKind::Contains => CompiledTerm::Contains(Finder::new(v).into_owned()),
        }
    }
    #[inline]
    fn hit(&self, s: &str) -> bool {
        match self {
            CompiledTerm::Prefix(p) => s.starts_with(p.as_str()),
            CompiledTerm::Suffix(p) => s.ends_with(p.as_str()),
            CompiledTerm::Contains(f) => f.find(s.as_bytes()).is_some(),
        }
    }
}

impl Compiled {
    fn from_matcher(m: &Matcher) -> Self {
        match m {
            Matcher::Prefix(p) => Compiled::Prefix(p.clone()),
            Matcher::Suffix(p) => Compiled::Suffix(p.clone()),
            Matcher::Contains(p) => Compiled::Contains(Finder::new(p).into_owned()),
            Matcher::Multi { values, all } => Compiled::Multi {
                finders: values.iter().map(|v| Finder::new(v).into_owned()).collect(),
                all: *all,
            },
            Matcher::Expr { terms, all } => Compiled::Expr {
                terms: terms.iter().map(|(k, v)| CompiledTerm::from(*k, v)).collect(),
                all: *all,
            },
        }
    }

    #[inline]
    fn hit(&self, s: &str) -> bool {
        match self {
            Compiled::Prefix(p) => s.starts_with(p.as_str()),
            Compiled::Suffix(p) => s.ends_with(p.as_str()),
            Compiled::Contains(f) => f.find(s.as_bytes()).is_some(),
            Compiled::Multi { finders, all } => {
                if *all {
                    finders.iter().all(|f| f.find(s.as_bytes()).is_some())
                } else {
                    finders.iter().any(|f| f.find(s.as_bytes()).is_some())
                }
            }
            Compiled::Expr { terms, all } => {
                if *all {
                    terms.iter().all(|t| t.hit(s))
                } else {
                    terms.iter().any(|t| t.hit(s))
                }
            }
        }
    }
}

fn main() -> Result<()> {
    let args = BenchArgs::parse();
    if args.format != Format::Parquet {
        bail!("bench-arrow only supports --format parquet (decompressed baseline)");
    }
    if args.mode != Mode::InMem {
        bail!("bench-arrow only supports --mode in-mem");
    }
    let spec = args.read_spec()?;
    let synth = match &spec {
        QuerySpec::Synthetic(s) => s.clone(),
        QuerySpec::Real(_) => bail!("bench-arrow only supports synthetic specs"),
    };
    let matcher = Matcher::from_spec(&synth)?;
    let compiled = Compiled::from_matcher(&matcher);

    // ---- load: decode the column into Arrow and keep it resident ----
    let load = bench_core::Timer::start();
    let arrays = read_column(&args.input, &synth.column)?;
    let load_ns = load.elapsed_ns();

    let rows: u64 = arrays.iter().map(|a| a.len() as u64).sum();
    let in_memory_bytes: u64 = arrays.iter().map(|a| a.get_array_memory_size() as u64).sum();

    // ---- measured iterations: scan + count, timing compute only ----
    let mut iters_ns = Vec::with_capacity(args.iterations);
    let mut result_rows = 0u64;
    for _ in 0..args.iterations {
        let t = bench_core::Timer::start();
        result_rows = count_matches(&arrays, &compiled);
        iters_ns.push(t.elapsed_ns());
    }

    let out = BenchOutput {
        engine: "arrow".into(),
        format: args.format.as_str().into(),
        mode: args.mode.as_str().into(),
        label: spec.label(),
        op: spec.op(),
        column: spec.column(),
        rows,
        result_rows,
        result_checksum: checksum(result_rows),
        file_bytes: bench_core::file_bytes(&args.input),
        in_memory_bytes,
        load_ns,
        // For the decompressed baseline the decode IS the decompression cost.
        decompress_ns: load_ns,
        pushdown: false,
        plan: format!("ArrowScan[{}] memmem/starts_with (decompressed)", synth.op),
        iters_ns,
    };
    out.print()
}

/// Read one column from a Parquet file as a vector of Arrow string arrays.
fn read_column(path: &std::path::Path, column: &str) -> Result<Vec<ArrayRef>> {
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
    use parquet::arrow::ProjectionMask;

    let file = File::open(path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.parquet_schema();
    let idx = builder
        .schema()
        .index_of(column)
        .map_err(|_| anyhow::anyhow!("column {column:?} not found in {path:?}"))?;
    let mask = ProjectionMask::roots(schema, [idx]);
    let reader = builder.with_projection(mask).build()?;

    let mut arrays: Vec<ArrayRef> = Vec::new();
    for batch in reader {
        let batch = batch?;
        // After projecting a single column it is at position 0.
        arrays.push(batch.column(0).clone());
    }
    Ok(arrays)
}

/// Count matching, non-null values across all chunks.
fn count_matches(arrays: &[ArrayRef], m: &Compiled) -> u64 {
    let mut count = 0u64;
    for a in arrays {
        count += count_in_array(a.as_ref(), m);
    }
    count
}

fn count_in_array(a: &dyn Array, m: &Compiled) -> u64 {
    if let Some(arr) = a.as_any().downcast_ref::<StringArray>() {
        return arr.iter().filter(|v| v.map(|s| m.hit(s)).unwrap_or(false)).count() as u64;
    }
    if let Some(arr) = a.as_any().downcast_ref::<LargeStringArray>() {
        return arr.iter().filter(|v| v.map(|s| m.hit(s)).unwrap_or(false)).count() as u64;
    }
    if let Some(arr) = a.as_any().downcast_ref::<StringViewArray>() {
        return arr.iter().filter(|v| v.map(|s| m.hit(s)).unwrap_or(false)).count() as u64;
    }
    panic!(
        "unsupported arrow string type: {:?} (expected Utf8/LargeUtf8/Utf8View)",
        a.data_type()
    );
}
