//! `convert` — the binary where compression metrics are measured.
//!
//! Reads csv/json/parquet into Arrow, then writes either canonical Parquet (with
//! a chosen codec) or a Vortex file (default BtrBlocks cascade), reporting:
//!
//! ```json
//! { "encode_ns":..., "input_bytes":..., "uncompressed_bytes":..., "output_bytes":..., "ratio":..., "rows":... }
//! ```
//!
//! `ratio = uncompressed_bytes / output_bytes`, where `uncompressed_bytes` is the
//! in-memory Arrow size — a fair, format-independent baseline.

use std::fs::File;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use arrow::array::RecordBatch;
use arrow::datatypes::SchemaRef;
use clap::Parser;
use serde::Serialize;

#[derive(Debug, Parser)]
#[command(about = "Transcode csv/json/parquet to canonical Parquet or Vortex")]
struct Args {
    #[arg(long)]
    input: PathBuf,
    #[arg(long = "input-format", default_value = "auto")]
    input_format: String, // auto|csv|json|parquet
    /// Destination file path.
    #[arg(long)]
    output: PathBuf,
    #[arg(long = "output-format")]
    output_format: String, // parquet|vortex
    /// parquet: zstd|snappy|none ; vortex: default (cascade)
    #[arg(long, default_value = "zstd")]
    compression: String,
    /// Stdout report format (only `json` is supported). Kept for contract
    /// symmetry with the bench binaries (`--report json`).
    #[arg(long, default_value = "json")]
    report: String,
}

#[derive(Serialize)]
struct Report {
    encode_ns: u64,
    input_bytes: u64,
    uncompressed_bytes: u64,
    output_bytes: u64,
    /// Fair, framing-free compressed footprint for cross-format comparison:
    /// the sum of the in-memory compressed buffers. For Vortex this is the
    /// array-tree `nbytes()` (every buffer in the compressed tree); for Parquet
    /// it is the summed compressed column-chunk size (data pages, no footer).
    inmem_compressed_bytes: u64,
    ratio: f64,
    rows: u64,
    output_format: String,
    compression: String,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let report = run(&args)?;
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}

fn detect_format(path: &Path, explicit: &str) -> Result<&'static str> {
    if explicit != "auto" {
        return Ok(match explicit {
            "csv" => "csv",
            "json" => "json",
            "parquet" => "parquet",
            other => bail!("unknown --input-format {other:?}"),
        });
    }
    match path.extension().and_then(|e| e.to_str()) {
        Some("csv") => Ok("csv"),
        Some("json") | Some("ndjson") => Ok("json"),
        Some("parquet") => Ok("parquet"),
        other => bail!("cannot auto-detect input format from extension {other:?}"),
    }
}

/// Read the whole input into Arrow batches plus schema, row count, and the
/// in-memory (uncompressed) size.
fn read_input(path: &Path, fmt: &str) -> Result<(SchemaRef, Vec<RecordBatch>, u64, u64)> {
    match fmt {
        "parquet" => {
            use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
            let file = File::open(path).with_context(|| format!("open {path:?}"))?;
            let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
            let schema = builder.schema().clone();
            let reader = builder.build()?;
            let mut batches = Vec::new();
            for b in reader {
                batches.push(b?);
            }
            let (rows, mem) = totals(&batches);
            Ok((schema, batches, rows, mem))
        }
        "csv" => {
            use arrow::csv::reader::{Format, ReaderBuilder};
            let file = File::open(path)?;
            let format = Format::default().with_header(true);
            let (schema, _) = format.infer_schema(File::open(path)?, Some(1024))?;
            let schema = SchemaRef::new(schema);
            let reader = ReaderBuilder::new(schema.clone())
                .with_format(format)
                .build(file)?;
            let batches = reader.collect::<Result<Vec<_>, _>>()?;
            let (rows, mem) = totals(&batches);
            Ok((schema, batches, rows, mem))
        }
        "json" => {
            use arrow::json::reader::{infer_json_schema_from_seekable, ReaderBuilder};
            use std::io::BufReader;
            let mut buf = BufReader::new(File::open(path)?);
            let (schema, _) = infer_json_schema_from_seekable(&mut buf, None)?;
            let schema = SchemaRef::new(schema);
            let reader =
                ReaderBuilder::new(schema.clone()).build(BufReader::new(File::open(path)?))?;
            let batches = reader.collect::<Result<Vec<_>, _>>()?;
            let (rows, mem) = totals(&batches);
            Ok((schema, batches, rows, mem))
        }
        other => bail!("unsupported input format {other:?}"),
    }
}

fn totals(batches: &[RecordBatch]) -> (u64, u64) {
    let rows = batches.iter().map(|b| b.num_rows() as u64).sum();
    let mem = batches
        .iter()
        .map(|b| b.get_array_memory_size() as u64)
        .sum();
    (rows, mem)
}

fn run(args: &Args) -> Result<Report> {
    let in_fmt = detect_format(&args.input, &args.input_format)?;
    let input_bytes = bench_core::file_bytes(&args.input);
    let (schema, batches, rows, uncompressed_bytes) = read_input(&args.input, in_fmt)?;

    let (encode_ns, inmem_compressed_bytes) = match args.output_format.as_str() {
        "parquet" => write_parquet(&args.output, schema, &batches, &args.compression)?,
        "vortex" => write_vortex(&args.output, schema, batches)?,
        other => bail!("unknown --output-format {other:?} (expected parquet|vortex)"),
    };

    let output_bytes = bench_core::file_bytes(&args.output);
    // Ratio uses the fair, framing-free in-memory compressed footprint.
    let ratio = if inmem_compressed_bytes > 0 {
        uncompressed_bytes as f64 / inmem_compressed_bytes as f64
    } else {
        0.0
    };

    Ok(Report {
        encode_ns,
        input_bytes,
        uncompressed_bytes,
        output_bytes,
        inmem_compressed_bytes,
        ratio,
        rows,
        output_format: args.output_format.clone(),
        compression: args.compression.clone(),
    })
}

fn write_parquet(
    out: &Path,
    schema: SchemaRef,
    batches: &[RecordBatch],
    codec: &str,
) -> Result<(u64, u64)> {
    use parquet::arrow::ArrowWriter;
    use parquet::basic::{Compression, ZstdLevel};
    use parquet::file::properties::WriterProperties;

    let compression = match codec {
        "zstd" | "default" => Compression::ZSTD(ZstdLevel::default()),
        "snappy" => Compression::SNAPPY,
        "none" | "uncompressed" => Compression::UNCOMPRESSED,
        other => bail!("unknown parquet compression {other:?}"),
    };
    let props = WriterProperties::builder()
        .set_compression(compression)
        .build();

    let timer = bench_core::Timer::start();
    let file = File::create(out).with_context(|| format!("create {out:?}"))?;
    let mut writer = ArrowWriter::try_new(file, schema, Some(props))?;
    for b in batches {
        writer.write(b)?;
    }
    let meta = writer.close()?;
    let encode_ns = timer.elapsed_ns();

    // Fair compressed footprint: summed compressed column-chunk sizes (the data
    // pages), excluding the file footer/schema framing.
    let inmem: i64 = meta
        .row_groups
        .iter()
        .flat_map(|rg| rg.columns.iter())
        .map(|c| c.meta_data.as_ref().map(|m| m.total_compressed_size).unwrap_or(0))
        .sum();
    Ok((encode_ns, inmem.max(0) as u64))
}

fn write_vortex(out: &Path, schema: SchemaRef, batches: Vec<RecordBatch>) -> Result<(u64, u64)> {
    use vortex::array::arrays::ChunkedArray;
    use vortex::array::arrow::FromArrowArray;
    use vortex::array::{ArrayRef, IntoArray, VortexSessionExecute};
    use vortex::dtype::arrow::FromArrowType;
    use vortex::dtype::DType;
    use vortex::file::WriteOptionsSessionExt;
    use vortex::io::session::RuntimeSessionExt;
    use vortex::session::VortexSession;
    use vortex::VortexSessionDefault;
    use vortex_btrblocks::BtrBlocksCompressor;

    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;

    // Everything that touches the vortex runtime happens inside `block_on` so
    // `with_tokio()` (which captures the current tokio handle) is valid.
    //
    // The workspace pins vortex with `unstable_encodings` (+ `zstd`), so the
    // default Btrblocks `ALL_SCHEMES` cascade used here includes Vortex's
    // unstable string schemes (OnPairScheme, ZstdBuffersScheme). Every Vortex run
    // in this repo uses them — see README "Vortex encodings".
    let (encode_ns, buf, inmem) = rt.block_on(async move {
        let session = VortexSession::default().with_tokio();
        let dtype = DType::from_arrow(schema.clone());

        // Fair compressed footprint: compress the *whole column as one array*
        // (matching the string codecs' single global dictionary), then sum every
        // buffer in the resulting compressed array tree (`nbytes`). This is the
        // framing-free in-memory size, not the serialized file size.
        let single_batch = arrow::compute::concat_batches(&schema, &batches)?;
        let single = ArrayRef::from_arrow(single_batch, false)?;
        let compressor = BtrBlocksCompressor::default();
        let mut ctx = session.create_execution_ctx();
        let inmem = compressor.compress(&single, &mut ctx)?.nbytes();

        // Encode the (chunked) file the query engines read.
        let timer = bench_core::Timer::start();
        let chunks: Vec<ArrayRef> = batches
            .into_iter()
            .map(|b| ArrayRef::from_arrow(b, false))
            .collect::<Result<_, _>>()?;
        let array = ChunkedArray::try_new(chunks, dtype)?.into_array();
        let mut buf: Vec<u8> = Vec::new();
        session
            .write_options()
            .write(&mut buf, array.to_array_stream())
            .await?;
        anyhow::Ok((timer.elapsed_ns(), buf, inmem))
    })?;

    std::fs::write(out, &buf)?;
    Ok((encode_ns, inmem))
}
