# Throughput (MB/s of uncompressed payload, mean across columns)

| algorithm | source | compress | decompress | random-access |
|---|---|---|---|---|
| LZ4 | ported | 390.0 | 1496.1 | 2889.9 |
| Dictionary | ported | 376.8 | 1896.6 | 22408.1 |
| Parquet+zstd | new | 309.4 | — | — |
| Vortex | new | 170.0 | — | — |
| FSST12 | ported | 150.8 | 1101.9 | 6577.1 |
| FSST | ported | 150.3 | 1026.1 | 6869.4 |
| OnPair16 (Rust) | new | 91.4 | 1477.5 | 11003.5 |
| OnPairMini12 | ported | 87.7 | 1491.4 | 10938.3 |
| OnPair16 (C++) | ported | 67.2 | 1518.5 | 11269.5 |
| OnPairMini10 | ported | 64.0 | 1314.3 | 8186.3 |
| OnPair (Rust) | new | 46.6 | 1189.5 | 11463.8 |
| OnPairMini14 | ported | 30.1 | 1628.4 | 11277.6 |
| OnPair (C++) | ported | 30.0 | 1271.2 | 11850.4 |
