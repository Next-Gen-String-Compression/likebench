# Throughput (MB/s of uncompressed payload, mean across columns)

| algorithm | source | compress | decompress | random-access |
|---|---|---|---|---|
| Dictionary | ported | 581.6 | 3304.2 | 70357.9 |
| LZ4 | ported | 450.1 | 1709.6 | 3371.2 |
| Parquet+zstd | new | 340.2 | — | — |
| FSST | ported | 157.7 | 1298.1 | 13163.1 |
| FSST12 | ported | 147.3 | 1421.7 | 13641.2 |
| OnPair16 (Rust) | new | 120.0 | 2321.0 | 19507.3 |
| Vortex | new | 113.0 | — | — |
| OnPairMini12 | ported | 109.7 | 1703.4 | 18111.7 |
| OnPair16 (C++) | ported | 88.1 | 2377.4 | 19053.5 |
| OnPairMini10 | ported | 81.4 | 1649.5 | 13079.0 |
| OnPair (Rust) | new | 66.6 | 1730.1 | 19832.2 |
| OnPairMini14 | ported | 38.8 | 1800.5 | 18146.5 |
| OnPair (C++) | ported | 37.3 | 1666.8 | 16700.2 |
