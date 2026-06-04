# Throughput (MB/s of uncompressed payload, mean across columns)

| algorithm | source | compress | decompress | random-access |
|---|---|---|---|---|
| Dictionary | ported | 481.0 | 2611.3 | 37396.9 |
| LZ4 | ported | 470.4 | 1745.0 | 3516.7 |
| Parquet+zstd | new | 352.9 | — | — |
| Vortex | new | 219.2 | — | — |
| FSST12 | ported | 189.5 | 1327.5 | 9028.6 |
| FSST | ported | 168.0 | 1270.8 | 7888.1 |
| OnPair16 (Rust) | new | 115.1 | 2148.9 | 15358.0 |
| OnPairMini12 | ported | 106.2 | 1713.3 | 12713.3 |
| OnPair16 (C++) | ported | 87.7 | 2168.8 | 16179.0 |
| OnPairMini10 | ported | 78.8 | 1556.9 | 9469.9 |
| OnPair (Rust) | new | 58.3 | 1637.9 | 16289.8 |
| OnPair (C++) | ported | 37.2 | 1683.3 | 16327.9 |
| OnPairMini14 | ported | 36.6 | 1825.6 | 13534.2 |
