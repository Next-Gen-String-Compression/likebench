//! `tpch-gen` — the base dataset source: a simple, **offline, deterministic**
//! TPC-H generator. It writes one TPC-H table to Parquet using `tpchgen-arrow`
//! (pure Rust, byte-for-byte `dbgen`-compatible), so likebench has string-heavy
//! columns (`l_comment`, `l_shipinstruct`, …) to benchmark without any download.
//!
//!   tpch-gen --table lineitem --scale 0.1 --output data.parquet
//!
//! The harness then treats the output like any other Parquet source: discover
//! its Utf8 columns, mine predicates, convert to Parquet/Vortex, run the matrix.

use std::fs::File;
use std::path::PathBuf;

use anyhow::{bail, Result};
use clap::Parser;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::properties::WriterProperties;
use tpchgen::generators::{
    CustomerGenerator, LineItemGenerator, OrderGenerator, PartGenerator, SupplierGenerator,
};
use tpchgen_arrow::{
    CustomerArrow, LineItemArrow, OrderArrow, PartArrow, RecordBatchIterator, SupplierArrow,
};

#[derive(Debug, Parser)]
#[command(about = "Generate a TPC-H table to Parquet (offline, deterministic)")]
struct Args {
    /// Which TPC-H table to generate (string-heavy tables recommended).
    #[arg(long, default_value = "lineitem")]
    table: String,
    /// TPC-H scale factor (0.01 ≈ 60k lineitem rows; 1.0 ≈ 6M).
    #[arg(long, default_value_t = 0.1)]
    scale: f64,
    /// Output Parquet path.
    #[arg(long)]
    output: PathBuf,
    /// RecordBatch size.
    #[arg(long, default_value_t = 8192)]
    batch_size: usize,
}

fn main() -> Result<()> {
    let args = Args::parse();
    // `part`/`part_count` = 1/1 generates the whole table in one shard.
    let rows = match args.table.as_str() {
        "lineitem" => write(
            LineItemArrow::new(LineItemGenerator::new(args.scale, 1, 1))
                .with_batch_size(args.batch_size),
            &args,
        )?,
        "orders" => write(
            OrderArrow::new(OrderGenerator::new(args.scale, 1, 1)).with_batch_size(args.batch_size),
            &args,
        )?,
        "customer" => write(
            CustomerArrow::new(CustomerGenerator::new(args.scale, 1, 1))
                .with_batch_size(args.batch_size),
            &args,
        )?,
        "part" => write(
            PartArrow::new(PartGenerator::new(args.scale, 1, 1)).with_batch_size(args.batch_size),
            &args,
        )?,
        "supplier" => write(
            SupplierArrow::new(SupplierGenerator::new(args.scale, 1, 1))
                .with_batch_size(args.batch_size),
            &args,
        )?,
        other => bail!("unknown --table {other:?} (try lineitem|orders|customer|part|supplier)"),
    };
    // A tiny machine-readable line the harness parses for the row count.
    println!(
        "{{\"table\":\"{}\",\"scale\":{},\"rows\":{}}}",
        args.table, args.scale, rows
    );
    Ok(())
}

/// Stream every RecordBatch from a generator into a zstd Parquet file.
fn write<G>(gen: G, args: &Args) -> Result<u64>
where
    G: RecordBatchIterator,
{
    let schema = gen.schema().clone();
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::default()))
        .build();
    let file = File::create(&args.output)?;
    let mut writer = ArrowWriter::try_new(file, schema, Some(props))?;
    let mut rows = 0u64;
    for batch in gen {
        rows += batch.num_rows() as u64;
        writer.write(&batch)?;
    }
    writer.close()?;
    Ok(rows)
}
