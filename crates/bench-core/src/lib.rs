//! Shared contract for every likebench engine binary.
//!
//! Engines are *dumb executors*: parse the uniform CLI, parse one self-describing
//! query spec, run it `iterations` times, and print one [`BenchOutput`] JSON
//! object. All warmup/aggregation/statistics live in the Python harness.
//!
//! This crate centralizes the three things that MUST agree across engines:
//!   * the [`BenchOutput`] JSON shape,
//!   * the [`checksum`] used for the cross-engine correctness gate, and
//!   * the predicate grammar [`Pred`] — a small AST (`prefix`/`suffix`/
//!     `contains`/`multi-contains`). SQL engines *convert* it to a `LIKE` clause
//!     ([`Pred::to_sql`]); scan engines (and the C++ codecs) *walk* it directly
//!     ([`Matcher`]). The AST is the source of truth — `LIKE` is only ever
//!     generated, never parsed back.

use std::io::Read;
use std::path::PathBuf;
use std::time::Instant;

use anyhow::{bail, Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

/// The uniform CLI shared by `bench-<engine>` binaries.
#[derive(Debug, Parser)]
#[command(about = "likebench engine binary (uniform contract)")]
pub struct BenchArgs {
    /// `parquet` (decompressed baseline) or `vortex` (compressed pushdown).
    #[arg(long)]
    pub format: Format,

    /// Path to the input file (a single-column Parquet or Vortex file).
    #[arg(long)]
    pub input: PathBuf,

    /// `in-mem` (load once, time compute) or `full-query` (time IO+scan+compute).
    #[arg(long)]
    pub mode: Mode,

    /// Inline JSON query spec, or `-` to read the spec JSON from stdin.
    #[arg(long = "query-spec")]
    pub query_spec: String,

    /// Total iterations to run. The harness sets this to warmup + measured.
    #[arg(long)]
    pub iterations: usize,

    /// Also emit the physical plan + pushdown flag.
    #[arg(long, default_value_t = false)]
    pub explain: bool,

    /// Codec selector for the standalone string-codec binaries (e.g. `fsst`,
    /// `onpair16`). Ignored by the query-engine binaries.
    #[arg(long, default_value = None)]
    pub codec: Option<String>,

    /// Output format. Only `json` is supported.
    #[arg(long, default_value = "json")]
    pub output: String,
}

impl BenchArgs {
    /// Resolve the spec, reading from stdin when `--query-spec -` is given.
    pub fn read_spec(&self) -> Result<QuerySpec> {
        let text = if self.query_spec == "-" {
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .context("reading query spec from stdin")?;
            buf
        } else {
            self.query_spec.clone()
        };
        QuerySpec::parse(&text)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Parquet,
    Vortex,
    /// The dependency-free `.strings` interchange file (see [`strings`]). Used by
    /// the standalone string-codec binaries (`bench-compress-cpp`, `bench-onpair`)
    /// that compress raw bytes themselves rather than reading Parquet/Vortex.
    Raw,
}

impl std::str::FromStr for Format {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self> {
        match s {
            "parquet" => Ok(Format::Parquet),
            "vortex" => Ok(Format::Vortex),
            "raw" | "strings" => Ok(Format::Raw),
            other => bail!("unknown --format {other:?} (expected parquet|vortex|raw)"),
        }
    }
}

impl Format {
    pub fn as_str(self) -> &'static str {
        match self {
            Format::Parquet => "parquet",
            Format::Vortex => "vortex",
            Format::Raw => "raw",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    InMem,
    FullQuery,
}

impl std::str::FromStr for Mode {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self> {
        match s {
            "in-mem" => Ok(Mode::InMem),
            "full-query" => Ok(Mode::FullQuery),
            other => bail!("unknown --mode {other:?} (expected in-mem|full-query)"),
        }
    }
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::InMem => "in-mem",
            Mode::FullQuery => "full-query",
        }
    }
}

// ---------------------------------------------------------------------------
// Query spec (self-describing). Unknown fields (selectivity, ...) are ignored.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum QuerySpec {
    Synthetic(SyntheticSpec),
    Real(RealSpec),
}

impl QuerySpec {
    pub fn parse(text: &str) -> Result<Self> {
        serde_json::from_str(text).with_context(|| format!("parsing query spec: {text}"))
    }

    pub fn label(&self) -> String {
        match self {
            QuerySpec::Synthetic(s) => s.label.clone().unwrap_or_else(|| s.op_label()),
            QuerySpec::Real(r) => r.label.clone().unwrap_or_else(|| "real".to_string()),
        }
    }

    /// The op tag for grouping/plots (e.g. `prefix`, `multi-contains`); see
    /// [`Pred::op`]. `None` for real specs.
    pub fn op(&self) -> Option<String> {
        match self {
            QuerySpec::Synthetic(s) => Some(s.op_label()),
            QuerySpec::Real(_) => None,
        }
    }

    pub fn column(&self) -> Option<String> {
        match self {
            QuerySpec::Synthetic(s) => Some(s.column.clone()),
            QuerySpec::Real(r) => r.column.clone(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct SyntheticSpec {
    pub column: String,
    /// The predicate AST to evaluate against `column`.
    pub predicate: Pred,
    #[serde(default)]
    pub label: Option<String>,
}

/// The predicate grammar (AST) — the source of truth for both the SQL path
/// ([`Pred::to_sql`]) and the scan path ([`Matcher::from_pred`]). Internally
/// tagged on `op`:
///
/// ```json
/// {"op": "prefix",         "value":  "http"}
/// {"op": "suffix",         "value":  ".com"}
/// {"op": "contains",       "value":  "google"}
/// {"op": "multi-contains", "values": ["a", "b"]}
/// ```
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case")]
pub enum Pred {
    /// `col LIKE 'value%'` — `value` is a prefix.
    Prefix { value: String },
    /// `col LIKE '%value'` — `value` is a suffix.
    Suffix { value: String },
    /// `col LIKE '%value%'` — `value` is a substring.
    Contains { value: String },
    /// `col LIKE '%a%b%…%'` — each value appears, in order (match `a`, then `b`
    /// after it, …). A single ordered pattern, *not* an AND of substrings.
    MultiContains { values: Vec<String> },
}

impl Pred {
    /// The op tag (`prefix`/`suffix`/`contains`/`multi-contains`).
    pub fn op(&self) -> &'static str {
        match self {
            Pred::Prefix { .. } => "prefix",
            Pred::Suffix { .. } => "suffix",
            Pred::Contains { .. } => "contains",
            Pred::MultiContains { .. } => "multi-contains",
        }
    }
}

impl SyntheticSpec {
    /// The op label for grouping/plots (see [`Pred::op`]).
    pub fn op_label(&self) -> String {
        self.predicate.op().to_string()
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct RealSpec {
    pub sql: String,
    #[serde(default)]
    pub column: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
}

// ---------------------------------------------------------------------------
// Matcher: the AST compiled to native string kernels, for engines that scan
// decompressed strings directly (e.g. bench-arrow; the C++ codecs mirror this).
// Built straight from the [`Pred`] AST — there is no LIKE parsing.
// ---------------------------------------------------------------------------

/// A predicate compiled to native string kernels.
#[derive(Debug, Clone)]
pub enum Matcher {
    Prefix(String),
    Suffix(String),
    Contains(String),
    /// Each value occurs, in order — the scan equivalent of `LIKE '%a%b%…%'`.
    MultiContains(Vec<String>),
}

impl Matcher {
    pub fn from_spec(spec: &SyntheticSpec) -> Result<Matcher> {
        Ok(Matcher::from_pred(&spec.predicate))
    }

    pub fn from_pred(pred: &Pred) -> Matcher {
        match pred {
            Pred::Prefix { value } => Matcher::Prefix(value.clone()),
            Pred::Suffix { value } => Matcher::Suffix(value.clone()),
            Pred::Contains { value } => Matcher::Contains(value.clone()),
            Pred::MultiContains { values } => Matcher::MultiContains(values.clone()),
        }
    }

    /// Reference implementation using std string ops + `memchr` (the hot path in
    /// bench-arrow prebuilds `memchr::memmem` finders from the same kernels).
    pub fn matches(&self, s: &str) -> bool {
        match self {
            Matcher::Prefix(p) => s.starts_with(p.as_str()),
            Matcher::Suffix(p) => s.ends_with(p.as_str()),
            Matcher::Contains(p) => memchr::memmem::find(s.as_bytes(), p.as_bytes()).is_some(),
            // `%a%b%…%`: find each value in order, each after the previous match.
            Matcher::MultiContains(values) => {
                let bytes = s.as_bytes();
                let mut start = 0usize;
                for v in values {
                    match memchr::memmem::find(&bytes[start..], v.as_bytes()) {
                        Some(pos) => start += pos + v.len(),
                        None => return false,
                    }
                }
                true
            }
        }
    }
}

// ---------------------------------------------------------------------------
// LIKE escaping + SQL building (for engines that go through SQL: DF, DuckDB)
// ---------------------------------------------------------------------------

/// Escape the LIKE metacharacters `\`, `%`, `_` in a *literal* value, so that
/// the value matches verbatim once wrapped in wildcards. Used by [`Pred::to_sql`]
/// (e.g. `escape_like("a%b") -> "a\\%b"`). Pair with `ESCAPE '\'`.
pub fn escape_like(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 8);
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '%' => out.push_str("\\%"),
            '_' => out.push_str("\\_"),
            other => out.push(other),
        }
    }
    out
}

/// Escape a string for use inside a single-quoted SQL literal.
fn sql_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

/// `"col" LIKE '<pattern>' ESCAPE '\'` for one LIKE pattern.
fn like_sql(col: &str, pattern: &str) -> String {
    format!("\"{col}\" LIKE {} ESCAPE '\\'", sql_quote(pattern))
}

impl Pred {
    /// Convert the AST into a SQL predicate over `col` as a single
    /// `LIKE '<pattern>' ESCAPE '\'`. Wildcards are added and the literal is
    /// escaped here, so every SQL engine builds the identical clause and a scan
    /// engine's [`Matcher`] result agrees with it.
    pub fn to_sql(&self, col: &str) -> String {
        match self {
            Pred::Prefix { value } => like_sql(col, &format!("{}%", escape_like(value))),
            Pred::Suffix { value } => like_sql(col, &format!("%{}", escape_like(value))),
            Pred::Contains { value } => like_sql(col, &format!("%{}%", escape_like(value))),
            // `%a%b%…%`: escaped values joined by `%`, wrapped in `%…%`.
            Pred::MultiContains { values } => {
                let joined = values
                    .iter()
                    .map(|v| escape_like(v))
                    .collect::<Vec<_>>()
                    .join("%");
                like_sql(col, &format!("%{joined}%"))
            }
        }
    }
}

/// Build a `SELECT count(*) FROM <table> WHERE <predicate>` for a synthetic
/// query, by converting its AST via [`Pred::to_sql`].
pub fn synthetic_to_sql(spec: &SyntheticSpec, table: &str) -> Result<String> {
    Ok(format!(
        "SELECT count(*) FROM {table} WHERE {}",
        spec.predicate.to_sql(&spec.column)
    ))
}

// ---------------------------------------------------------------------------
// Output + checksum + timing
// ---------------------------------------------------------------------------

/// The single JSON object every engine prints to stdout.
#[derive(Debug, Clone, Default, Serialize)]
pub struct BenchOutput {
    pub engine: String,
    pub format: String,
    pub mode: String,
    pub label: String,
    pub op: Option<String>,
    pub column: Option<String>,
    pub rows: u64,
    pub result_rows: u64,
    pub result_checksum: String,
    pub file_bytes: u64,
    pub in_memory_bytes: u64,
    pub load_ns: u64,
    pub decompress_ns: u64,
    pub pushdown: bool,
    pub plan: String,
    pub iters_ns: Vec<u64>,

    // ---- compression-quality extension (optional; only the standalone string
    // codec binaries populate these). Older engines omit them entirely, so the
    // JSON shape stays backwards compatible. ----
    /// Which codec produced these numbers (e.g. "fsst", "onpair16").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub codec: Option<String>,
    /// Nanoseconds to compress the whole column once.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compress_ns: Option<u64>,
    /// Total compressed footprint in bytes (codes + dictionary + lengths).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub compressed_bytes: Option<u64>,
    /// Nanoseconds to decode N random single rows (point-access workload).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub decompress_random_ns: Option<u64>,
}

impl BenchOutput {
    pub fn print(&self) -> Result<()> {
        println!("{}", serde_json::to_string(self)?);
        Ok(())
    }
}

/// FNV-1a over the 8-byte little-endian encoding of `result_rows`.
///
/// Identical across engines (and the Python reference engine), so equal counts
/// produce equal checksums — the basis of the correctness gate.
pub fn checksum(result_rows: u64) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in result_rows.to_le_bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("0x{h:016x}")
}

/// Monotonic nanosecond stopwatch.
pub struct Timer(Instant);

impl Timer {
    pub fn start() -> Self {
        Timer(Instant::now())
    }
    pub fn elapsed_ns(&self) -> u64 {
        self.0.elapsed().as_nanos() as u64
    }
}

/// Return the file size in bytes (0 if it cannot be stat'd).
pub fn file_bytes(path: &std::path::Path) -> u64 {
    std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)
}

// ---------------------------------------------------------------------------
// `.strings` interchange format
// ---------------------------------------------------------------------------

/// The dependency-free column interchange used by the standalone string codecs.
///
/// A direct port of CompressionBenchmark's `StringCollector` layout, so a C++
/// and a Rust codec see byte-identical input. On-disk layout (all little-endian):
///
/// ```text
///   magic   : b"STRZ"          (4 bytes)
///   version : u32 = 1          (4 bytes)
///   n        : u64             number of strings
///   nbytes   : u64             total payload bytes
///   offsets  : (n+1) * u64     offsets[0]=0, offsets[n]=nbytes
///   data     : nbytes          concatenated UTF-8 payloads (nulls -> empty)
/// ```
pub mod strings {
    use super::{Context, Result};
    use std::path::Path;

    const MAGIC: &[u8; 4] = b"STRZ";
    const VERSION: u32 = 1;

    /// An owned, decoded column: concatenated bytes plus `n+1` offsets.
    pub struct StringColumn {
        pub data: Vec<u8>,
        pub offsets: Vec<u64>,
    }

    impl StringColumn {
        pub fn len(&self) -> usize {
            self.offsets.len().saturating_sub(1)
        }
        pub fn is_empty(&self) -> bool {
            self.len() == 0
        }
        pub fn total_bytes(&self) -> u64 {
            *self.offsets.last().unwrap_or(&0)
        }
        /// The i-th value as bytes.
        #[inline]
        pub fn bytes(&self, i: usize) -> &[u8] {
            let (a, b) = (self.offsets[i] as usize, self.offsets[i + 1] as usize);
            &self.data[a..b]
        }
        /// The i-th value as `&str` (payloads are written as valid UTF-8).
        #[inline]
        pub fn get(&self, i: usize) -> &str {
            // SAFETY-equivalent: payloads come from UTF-8 sources; fall back lossily
            // only on malformed input rather than panicking the whole run.
            std::str::from_utf8(self.bytes(i)).unwrap_or("")
        }
    }

    /// Read a `.strings` file written by [`write`] (or the Python harness).
    pub fn read(path: &Path) -> Result<StringColumn> {
        let buf = std::fs::read(path).with_context(|| format!("read strings file {path:?}"))?;
        if buf.len() < 24 || &buf[0..4] != MAGIC {
            anyhow::bail!("{path:?} is not a STRZ strings file");
        }
        let version = u32::from_le_bytes(buf[4..8].try_into().unwrap());
        if version != VERSION {
            anyhow::bail!("unsupported STRZ version {version}");
        }
        let n = u64::from_le_bytes(buf[8..16].try_into().unwrap()) as usize;
        let nbytes = u64::from_le_bytes(buf[16..24].try_into().unwrap()) as usize;
        let off_start = 24usize;
        let off_end = off_start + (n + 1) * 8;
        if buf.len() < off_end + nbytes {
            anyhow::bail!("{path:?} truncated: header promises more than file holds");
        }
        let mut offsets = Vec::with_capacity(n + 1);
        for k in 0..=n {
            let s = off_start + k * 8;
            offsets.push(u64::from_le_bytes(buf[s..s + 8].try_into().unwrap()));
        }
        let data = buf[off_end..off_end + nbytes].to_vec();
        Ok(StringColumn { data, offsets })
    }

    /// Write a `.strings` file from any iterator of byte slices.
    pub fn write<'a, I>(path: &Path, values: I) -> Result<()>
    where
        I: IntoIterator<Item = &'a [u8]>,
    {
        let mut data: Vec<u8> = Vec::new();
        let mut offsets: Vec<u64> = vec![0];
        for v in values {
            data.extend_from_slice(v);
            offsets.push(data.len() as u64);
        }
        let n = (offsets.len() - 1) as u64;
        let mut out: Vec<u8> = Vec::with_capacity(24 + offsets.len() * 8 + data.len());
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&n.to_le_bytes());
        out.extend_from_slice(&(data.len() as u64).to_le_bytes());
        for o in &offsets {
            out.extend_from_slice(&o.to_le_bytes());
        }
        out.extend_from_slice(&data);
        std::fs::write(path, &out).with_context(|| format!("write strings file {path:?}"))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksum_matches_reference() {
        // Values computed by tests/fake_engine.py::fnv1a64 (cross-language gate).
        assert_eq!(checksum(0), "0xa8c7f832281a39c5");
        assert_eq!(checksum(7), "0x4bd7a317074c5b62");
        assert_eq!(checksum(9), "0x81a3697174a540ac");
        assert_eq!(checksum(41233), "0x925953b742572d6f");
    }

    #[test]
    fn escape_like_handles_metachars() {
        assert_eq!(escape_like("a%b_c\\d"), "a\\%b\\_c\\\\d");
    }

    fn syn(column: &str, predicate: Pred) -> SyntheticSpec {
        SyntheticSpec {
            column: column.into(),
            predicate,
            label: None,
        }
    }

    #[test]
    fn pred_to_sql_per_op() {
        // contains, with a literal `%` in the value -> escaped, wildcards added.
        assert_eq!(
            synthetic_to_sql(
                &syn(
                    "URL",
                    Pred::Contains {
                        value: "a%b".into()
                    }
                ),
                "hits"
            )
            .unwrap(),
            "SELECT count(*) FROM hits WHERE \"URL\" LIKE '%a\\%b%' ESCAPE '\\'"
        );
        assert_eq!(
            synthetic_to_sql(
                &syn(
                    "URL",
                    Pred::Prefix {
                        value: "http".into()
                    }
                ),
                "hits"
            )
            .unwrap(),
            "SELECT count(*) FROM hits WHERE \"URL\" LIKE 'http%' ESCAPE '\\'"
        );
        // multi-contains -> one ordered `%a%b%` pattern (not AND/OR of clauses).
        let multi = synthetic_to_sql(
            &syn(
                "URL",
                Pred::MultiContains {
                    values: vec!["a".into(), "b".into()],
                },
            ),
            "hits",
        )
        .unwrap();
        assert_eq!(
            multi,
            "SELECT count(*) FROM hits WHERE \"URL\" LIKE '%a%b%' ESCAPE '\\'"
        );
    }

    #[test]
    fn matcher_semantics() {
        assert!(Matcher::from_pred(&Pred::Contains { value: "oo".into() }).matches("food"));
        assert!(!Matcher::from_pred(&Pred::Prefix {
            value: "htt".into()
        })
        .matches("food"));
        assert!(Matcher::from_pred(&Pred::Suffix { value: "od".into() }).matches("food"));

        // `%a%b%`: a then b, in order.
        let m = Matcher::from_pred(&Pred::MultiContains {
            values: vec!["a".into(), "b".into()],
        });
        assert!(m.matches("xaybz")); // a … then b
        assert!(m.matches("ab"));
        assert!(!m.matches("xbya")); // b before a -> no ordered match
        assert!(!m.matches("a")); // missing b
    }

    #[test]
    fn op_label_is_the_ast_tag() {
        assert_eq!(
            syn("URL", Pred::Prefix { value: "h".into() }).op_label(),
            "prefix"
        );
        assert_eq!(
            syn("URL", Pred::Contains { value: "h".into() }).op_label(),
            "contains"
        );
        assert_eq!(
            syn(
                "URL",
                Pred::MultiContains {
                    values: vec!["a".into()],
                },
            )
            .op_label(),
            "multi-contains"
        );
    }

    #[test]
    fn spec_parses_ast_and_ignores_unknown_fields() {
        let spec = QuerySpec::parse(
            r#"{"kind":"synthetic","column":"URL","predicate":{"op":"prefix","value":"http"},
                "label":"l","selectivity":0.1,"selectivity_bucket":"p10"}"#,
        )
        .unwrap();
        assert_eq!(spec.op().as_deref(), Some("prefix"));
        assert_eq!(spec.column().as_deref(), Some("URL"));

        let multi = QuerySpec::parse(
            r#"{"kind":"synthetic","column":"URL",
                "predicate":{"op":"multi-contains","values":["a","b"]}}"#,
        )
        .unwrap();
        assert_eq!(multi.op().as_deref(), Some("multi-contains"));
    }
}
