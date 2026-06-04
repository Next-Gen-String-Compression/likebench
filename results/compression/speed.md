# Throughput (MB/s of uncompressed payload, mean across columns)

| algorithm | source | compress | decompress | random-access |
|---|---|---|---|---|
| Dictionary | ported | 507.5 | 2769.1 | 40133.8 |
| LZ4 | ported | 484.7 | 1819.8 | 3573.0 |
| Parquet+zstd | new | 365.8 | — | — |
| Vortex | new | 341.2 | — | — |
| FSST12 | ported | 195.5 | 1357.7 | 10047.7 |
| FSST | ported | 170.3 | 1301.9 | 9088.8 |
| OnPair16 (Rust) | new | 117.6 | 2218.1 | 18003.5 |
| OnPairMini12 | ported | 107.2 | 1811.2 | 13801.3 |
| OnPair16 (C++) | ported | 91.4 | 2285.2 | 17528.3 |
| OnPairMini10 | ported | 78.1 | 1623.6 | 10458.4 |
| OnPair (Rust) | new | 60.2 | 1744.9 | 18433.1 |
| OnPairMini14 | ported | 37.6 | 1903.1 | 16043.4 |
| OnPair (C++) | ported | 37.0 | 1765.7 | 18924.7 |
