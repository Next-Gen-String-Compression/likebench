//! `bench-onpair` — the SpiralDB-adjacent OnPair string codec, in likebench form.
//!
//! OnPair (Gargiulo et al., "Short Strings Compression for Fast Random Access")
//! is a dictionary scheme tuned for *random access*: each string decodes
//! independently. This binary ports the author's Rust implementation
//! (`onpair_rs`) into likebench's uniform contract:
//!
//!   1. read the dependency-free `.strings` column (`--format raw`),
//!   2. compress it once (recording `compress_ns` + `compressed_bytes`),
//!   3. full-decode once to measure `decompress_ns` and prove losslessness,
//!   4. decode N random rows to measure point-access latency, and
//!   5. for each measured iteration, decode every row and run the synthetic
//!      matcher, so `result_rows`/`result_checksum` match the other engines.
//!
//! Supports: `--mode in-mem --format raw` with `kind == synthetic`.
//! Codecs (`--codec`): `onpair` (default), `onpair16`.

use anyhow::{bail, Result};
use bench_core::{checksum, strings, BenchArgs, BenchOutput, Format, Matcher, Mode, QuerySpec};
use clap::Parser;
use onpair_rs::{OnPair, OnPair16};

/// OnPair's pair-merge frequency threshold (matches the upstream examples).
const THRESHOLD: u16 = 5;
/// How many random single-row decodes to time for the point-access metric.
const RANDOM_ROWS: usize = 50_000;

/// Object-safe surface over the two OnPair variants so the run loop is generic.
trait Codec {
    fn compress(&mut self, data: &[u8], offsets: &[usize]);
    fn decompress_into(&mut self, index: usize, buf: &mut [u8]) -> usize;
    fn space_used(&self) -> usize;
}

impl Codec for OnPair {
    fn compress(&mut self, data: &[u8], offsets: &[usize]) {
        self.compress_bytes(data, offsets);
    }
    fn decompress_into(&mut self, index: usize, buf: &mut [u8]) -> usize {
        self.decompress_string(index, buf)
    }
    fn space_used(&self) -> usize {
        OnPair::space_used(self)
    }
}

impl Codec for OnPair16 {
    fn compress(&mut self, data: &[u8], offsets: &[usize]) {
        self.compress_bytes(data, offsets);
    }
    fn decompress_into(&mut self, index: usize, buf: &mut [u8]) -> usize {
        self.decompress_string(index, buf)
    }
    fn space_used(&self) -> usize {
        OnPair16::space_used(self)
    }
}

fn main() -> Result<()> {
    let args = BenchArgs::parse();
    if args.format != Format::Raw {
        bail!("bench-onpair only supports --format raw (the .strings column)");
    }
    if args.mode != Mode::InMem {
        bail!("bench-onpair only supports --mode in-mem");
    }
    let spec = args.read_spec()?;
    let synth = match &spec {
        QuerySpec::Synthetic(s) => s.clone(),
        QuerySpec::Real(_) => bail!("bench-onpair only supports synthetic specs"),
    };
    let matcher = Matcher::from_spec(&synth)?;

    let codec_name = args.codec.as_deref().unwrap_or("onpair").to_string();

    // ---- load: read the column + train the dictionary + compress ----
    let load = bench_core::Timer::start();
    let mut col = strings::read(&args.input)?;
    let n = col.len();
    let offsets: Vec<usize> = col.offsets.iter().map(|&o| o as usize).collect();
    let max_len = (0..n).map(|i| col.bytes(i).len()).max().unwrap_or(0);
    // OnPair's longest-prefix matcher reads 8 bytes at a time and may over-read
    // up to 8 bytes past a token's end; pad the payload so that stays in-bounds.
    // (Logical lengths come from `offsets`, so the padding is never addressed.)
    let logical_bytes = col.data.len();
    col.data.resize(logical_bytes + 32, 0);

    let mut codec: Box<dyn Codec> = match codec_name.as_str() {
        "onpair" => Box::new(OnPair::with_capacity(
            THRESHOLD,
            n,
            col.total_bytes() as usize,
        )),
        "onpair16" => Box::new(OnPair16::with_capacity(
            THRESHOLD,
            n,
            col.total_bytes() as usize,
        )),
        other => bail!("unknown --codec {other:?} (expected onpair|onpair16)"),
    };

    let compress_t = bench_core::Timer::start();
    codec.compress(&col.data, &offsets);
    let compress_ns = compress_t.elapsed_ns();
    let load_ns = load.elapsed_ns();
    let compressed_bytes = codec.space_used() as u64;

    // ---- full decode once: measure decompress_ns + verify losslessness ----
    let mut scratch = vec![0u8; max_len + 32];
    let decompress_t = bench_core::Timer::start();
    let mut decoded_bytes = 0u64;
    for i in 0..n {
        let len = codec.decompress_into(i, &mut scratch);
        decoded_bytes += len as u64;
        if scratch[..len] != *col.bytes(i) {
            bail!("OnPair roundtrip mismatch at row {i} (codec {codec_name})");
        }
    }
    let decompress_ns = decompress_t.elapsed_ns();
    debug_assert_eq!(decoded_bytes, col.total_bytes());

    // ---- random point-access decode latency ----
    let decompress_random_ns = if n > 0 {
        let idxs = random_indices(RANDOM_ROWS, n);
        let t = bench_core::Timer::start();
        for &i in &idxs {
            let _ = codec.decompress_into(i, &mut scratch);
        }
        Some(t.elapsed_ns())
    } else {
        None
    };

    // ---- measured iterations: decode + match (decompress-then-scan cost) ----
    let mut iters_ns = Vec::with_capacity(args.iterations);
    let mut result_rows = 0u64;
    for _ in 0..args.iterations {
        let t = bench_core::Timer::start();
        result_rows = count_matches(codec.as_mut(), n, &mut scratch, &matcher);
        iters_ns.push(t.elapsed_ns());
    }

    let out = BenchOutput {
        engine: "onpair".into(),
        format: args.format.as_str().into(),
        mode: args.mode.as_str().into(),
        label: spec.label(),
        op: spec.op(),
        column: spec.column(),
        rows: n as u64,
        result_rows,
        result_checksum: checksum(result_rows),
        file_bytes: bench_core::file_bytes(&args.input),
        // Uncompressed payload size, so `ratio = in_memory_bytes/compressed_bytes`.
        in_memory_bytes: col.total_bytes(),
        load_ns,
        decompress_ns,
        pushdown: false,
        plan: format!("OnPairDecode[{codec_name}] -> matcher[{}] (decompress-then-scan)", synth.op),
        iters_ns,
        codec: Some(codec_name),
        compress_ns: Some(compress_ns),
        compressed_bytes: Some(compressed_bytes),
        decompress_random_ns,
    };
    out.print()
}

/// Decode every row into `scratch` and count those matching the predicate.
fn count_matches(codec: &mut dyn Codec, n: usize, scratch: &mut [u8], m: &Matcher) -> u64 {
    let mut count = 0u64;
    for i in 0..n {
        let len = codec.decompress_into(i, scratch);
        // OnPair payloads are valid UTF-8 (they came from a UTF-8 column).
        if let Ok(s) = std::str::from_utf8(&scratch[..len]) {
            if m.matches(s) {
                count += 1;
            }
        }
    }
    count
}

/// Deterministic, sorted random indices in `[0, max)` for cache-friendly access
/// (mirrors CompressionBenchmark's `GenerateRandomIndices`).
fn random_indices(count: usize, max: usize) -> Vec<usize> {
    // Small xorshift; seeded constant keeps the point-access workload reproducible.
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut next = || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    let mut idxs: Vec<usize> = (0..count).map(|_| (next() as usize) % max).collect();
    idxs.sort_unstable();
    idxs
}
