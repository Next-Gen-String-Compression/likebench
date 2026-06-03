//! Shared contract for every likebench engine binary.
//!
//! Engines are *dumb executors*: parse the uniform CLI, parse one self-describing
//! query spec, run it `iterations` times, and print one [`BenchOutput`] JSON
//! object. All warmup/aggregation/statistics live in the Python harness.
//!
//! This crate centralizes the three things that MUST agree across engines:
//!   * the [`BenchOutput`] JSON shape,
//!   * the [`checksum`] used for the cross-engine correctness gate, and
//!   * how a synthetic op becomes a correct, escaped `LIKE` pattern / predicate.

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
}

impl std::str::FromStr for Format {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self> {
        match s {
            "parquet" => Ok(Format::Parquet),
            "vortex" => Ok(Format::Vortex),
            other => bail!("unknown --format {other:?} (expected parquet|vortex)"),
        }
    }
}

impl Format {
    pub fn as_str(self) -> &'static str {
        match self {
            Format::Parquet => "parquet",
            Format::Vortex => "vortex",
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
            QuerySpec::Synthetic(s) => s.label.clone().unwrap_or_else(|| s.op.clone()),
            QuerySpec::Real(r) => r.label.clone().unwrap_or_else(|| "real".to_string()),
        }
    }

    pub fn op(&self) -> Option<String> {
        match self {
            QuerySpec::Synthetic(s) => Some(s.op.clone()),
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
    pub op: String,
    pub column: String,
    #[serde(default)]
    pub value: Option<String>,
    #[serde(default)]
    pub values: Option<Vec<String>>,
    /// "all" (AND) or "any" (OR) for multicontains.
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub predicate: Option<Predicate>,
    #[serde(default)]
    pub label: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RealSpec {
    pub sql: String,
    #[serde(default)]
    pub column: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
}

/// A composite predicate for `op == "expr"`: exactly one of `and`/`or` is set.
#[derive(Debug, Clone, Deserialize)]
pub struct Predicate {
    #[serde(default)]
    pub and: Option<Vec<Term>>,
    #[serde(default)]
    pub or: Option<Vec<Term>>,
}

/// A single term of a composite predicate: exactly one of these is set.
#[derive(Debug, Clone, Deserialize)]
pub struct Term {
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub suffix: Option<String>,
    #[serde(default)]
    pub contains: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TermKind {
    Prefix,
    Suffix,
    Contains,
}

impl Term {
    pub fn parse(&self) -> Result<(TermKind, &str)> {
        match (&self.prefix, &self.suffix, &self.contains) {
            (Some(p), None, None) => Ok((TermKind::Prefix, p)),
            (None, Some(s), None) => Ok((TermKind::Suffix, s)),
            (None, None, Some(c)) => Ok((TermKind::Contains, c)),
            _ => bail!("predicate term must have exactly one of prefix/suffix/contains"),
        }
    }
}

// ---------------------------------------------------------------------------
// Compiled predicate (for engines that scan strings directly, e.g. bench-arrow)
// ---------------------------------------------------------------------------

/// A parsed predicate ready to evaluate against a `&str`.
#[derive(Debug, Clone)]
pub enum Matcher {
    Prefix(String),
    Suffix(String),
    Contains(String),
    Multi {
        values: Vec<String>,
        all: bool,
    },
    Expr {
        terms: Vec<(TermKind, String)>,
        all: bool,
    },
}

impl Matcher {
    pub fn from_spec(spec: &SyntheticSpec) -> Result<Matcher> {
        let val = || -> Result<String> {
            spec.value
                .clone()
                .with_context(|| format!("op {:?} requires `value`", spec.op))
        };
        Ok(match spec.op.as_str() {
            "prefix" => Matcher::Prefix(val()?),
            "suffix" => Matcher::Suffix(val()?),
            "contains" => Matcher::Contains(val()?),
            "multicontains" => {
                let values = spec
                    .values
                    .clone()
                    .context("op multicontains requires `values`")?;
                let all = spec.mode.as_deref() != Some("any");
                Matcher::Multi { values, all }
            }
            "expr" => {
                let pred = spec
                    .predicate
                    .as_ref()
                    .context("op expr requires `predicate`")?;
                let (terms_raw, all) = match (&pred.and, &pred.or) {
                    (Some(t), None) => (t, true),
                    (None, Some(t)) => (t, false),
                    _ => bail!("expr predicate must have exactly one of and/or"),
                };
                let mut terms = Vec::new();
                for t in terms_raw {
                    let (k, v) = t.parse()?;
                    terms.push((k, v.to_string()));
                }
                Matcher::Expr { terms, all }
            }
            other => bail!("unknown synthetic op {other:?}"),
        })
    }

    /// Reference implementation using std string ops (used in tests; the hot
    /// path in bench-arrow uses prebuilt `memchr::memmem` finders).
    pub fn matches(&self, s: &str) -> bool {
        match self {
            Matcher::Prefix(p) => s.starts_with(p.as_str()),
            Matcher::Suffix(p) => s.ends_with(p.as_str()),
            Matcher::Contains(p) => memchr::memmem::find(s.as_bytes(), p.as_bytes()).is_some(),
            Matcher::Multi { values, all } => {
                let hit = |v: &String| memchr::memmem::find(s.as_bytes(), v.as_bytes()).is_some();
                if *all {
                    values.iter().all(hit)
                } else {
                    values.iter().any(hit)
                }
            }
            Matcher::Expr { terms, all } => {
                let hit = |(k, v): &(TermKind, String)| match k {
                    TermKind::Prefix => s.starts_with(v.as_str()),
                    TermKind::Suffix => s.ends_with(v.as_str()),
                    TermKind::Contains => {
                        memchr::memmem::find(s.as_bytes(), v.as_bytes()).is_some()
                    }
                };
                if *all {
                    terms.iter().all(hit)
                } else {
                    terms.iter().any(hit)
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// LIKE escaping + SQL building (for engines that go through SQL: DF, DuckDB)
// ---------------------------------------------------------------------------

/// Escape the LIKE metacharacters `\`, `%`, `_` in a literal value, so that the
/// value is matched verbatim. Pair with `ESCAPE '\'`.
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

/// `col LIKE '<wildcarded escaped value>' ESCAPE '\'` for one term kind.
fn term_sql(col: &str, kind: TermKind, value: &str) -> String {
    let esc = escape_like(value);
    let pat = match kind {
        TermKind::Prefix => format!("{esc}%"),
        TermKind::Suffix => format!("%{esc}"),
        TermKind::Contains => format!("%{esc}%"),
    };
    format!("\"{col}\" LIKE {} ESCAPE '\\'", sql_quote(&pat))
}

/// Build a `SELECT count(*) FROM <table> WHERE <predicate>` for a synthetic op.
///
/// Each engine adds wildcards + escapes here (identically) so cross-engine
/// checksums agree regardless of which kernel actually runs underneath.
pub fn synthetic_to_sql(spec: &SyntheticSpec, table: &str) -> Result<String> {
    let col = &spec.column;
    let pred = match spec.op.as_str() {
        "prefix" => term_sql(
            col,
            TermKind::Prefix,
            spec.value.as_deref().context("value")?,
        ),
        "suffix" => term_sql(
            col,
            TermKind::Suffix,
            spec.value.as_deref().context("value")?,
        ),
        "contains" => term_sql(
            col,
            TermKind::Contains,
            spec.value.as_deref().context("value")?,
        ),
        "multicontains" => {
            let values = spec.values.as_ref().context("values")?;
            let joiner = if spec.mode.as_deref() == Some("any") {
                " OR "
            } else {
                " AND "
            };
            values
                .iter()
                .map(|v| term_sql(col, TermKind::Contains, v))
                .collect::<Vec<_>>()
                .join(joiner)
        }
        "expr" => {
            let pred = spec.predicate.as_ref().context("predicate")?;
            let (terms, joiner) = match (&pred.and, &pred.or) {
                (Some(t), None) => (t, " AND "),
                (None, Some(t)) => (t, " OR "),
                _ => bail!("expr predicate must have exactly one of and/or"),
            };
            terms
                .iter()
                .map(|t| {
                    let (k, v) = t.parse()?;
                    Ok(term_sql(col, k, v))
                })
                .collect::<Result<Vec<_>>>()?
                .join(joiner)
        }
        other => bail!("unknown synthetic op {other:?}"),
    };
    Ok(format!("SELECT count(*) FROM {table} WHERE {pred}"))
}

// ---------------------------------------------------------------------------
// Output + checksum + timing
// ---------------------------------------------------------------------------

/// The single JSON object every engine prints to stdout.
#[derive(Debug, Clone, Serialize)]
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

    #[test]
    fn synthetic_contains_sql() {
        let spec = SyntheticSpec {
            op: "contains".into(),
            column: "URL".into(),
            value: Some("a%b".into()),
            values: None,
            mode: None,
            predicate: None,
            label: None,
        };
        let sql = synthetic_to_sql(&spec, "hits").unwrap();
        assert_eq!(
            sql,
            "SELECT count(*) FROM hits WHERE \"URL\" LIKE '%a\\%b%' ESCAPE '\\'"
        );
    }

    #[test]
    fn matcher_semantics() {
        let m = Matcher::Contains("oo".into());
        assert!(m.matches("food"));
        assert!(!m.matches("bar"));
        let any = Matcher::Multi {
            values: vec!["x".into(), "y".into()],
            all: false,
        };
        assert!(any.matches("axe"));
        let all = Matcher::Multi {
            values: vec!["a".into(), "z".into()],
            all: true,
        };
        assert!(!all.matches("axe"));
    }

    #[test]
    fn spec_parsing_ignores_unknown_fields() {
        let spec = QuerySpec::parse(
            r#"{"kind":"synthetic","op":"prefix","column":"URL","value":"http",
                "label":"l","selectivity":0.1,"selectivity_bucket":"p10"}"#,
        )
        .unwrap();
        assert_eq!(spec.op().as_deref(), Some("prefix"));
        assert_eq!(spec.column().as_deref(), Some("URL"));
    }
}
