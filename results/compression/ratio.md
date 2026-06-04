# Compression ratio (× vs raw UTF-8 payload)

| algorithm | source | URL | Title | Referer | SearchPhrase | **mean** |
|---|---|---|---|---|---|---|
| Vortex | new | 5.37 | 14.25 | 5.32 | 5.58 | **7.63** |
| Parquet+zstd | new | 5.88 | 11.70 | 5.58 | 6.25 | **7.36** |
| OnPair (Rust) | new | 6.35 | 11.20 | 5.23 | 4.13 | **6.73** |
| OnPair (C++) | ported | 6.37 | 11.36 | 5.26 | 2.24 | **6.31** |
| OnPairMini14 | ported | 4.21 | 5.30 | 3.68 | 4.75 | **4.48** |
| OnPair16 (Rust) | new | 4.44 | 5.56 | 3.92 | 3.68 | **4.40** |
| OnPairMini12 | ported | 4.01 | 5.09 | 3.45 | 5.02 | **4.39** |
| OnPair16 (C++) | ported | 4.39 | 5.56 | 3.90 | 2.90 | **4.19** |
| LZ4 | ported | 3.92 | 6.91 | 3.84 | 1.82 | **4.12** |
| Dictionary | ported | 2.33 | 9.31 | 2.56 | 1.20 | **3.85** |
| OnPairMini10 | ported | 2.61 | 3.15 | 2.46 | 3.98 | **3.05** |
| FSST12 | ported | 1.86 | 2.37 | 1.81 | 1.31 | **1.84** |
| FSST | ported | 1.72 | 1.90 | 1.68 | 1.32 | **1.65** |
