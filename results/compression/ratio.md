# Compression ratio (× vs raw UTF-8 payload)

| algorithm | source | URL | Title | Referer | SearchPhrase | **mean** |
|---|---|---|---|---|---|---|
| Vortex | new | 5.36 | 24.92 | 5.89 | 5.47 | **10.41** |
| Parquet+zstd | new | 5.89 | 11.72 | 5.58 | 6.29 | **7.37** |
| OnPair (Rust) | new | 6.34 | 11.32 | 5.23 | 4.14 | **6.76** |
| OnPair (C++) | ported | 6.38 | 11.45 | 5.27 | 2.24 | **6.33** |
| OnPairMini14 | ported | 4.21 | 5.30 | 3.67 | 4.73 | **4.48** |
| OnPair16 (Rust) | new | 4.45 | 5.58 | 3.90 | 3.71 | **4.41** |
| OnPairMini12 | ported | 4.03 | 5.06 | 3.43 | 5.03 | **4.39** |
| OnPair16 (C++) | ported | 4.41 | 5.55 | 3.95 | 2.93 | **4.21** |
| LZ4 | ported | 3.92 | 6.91 | 3.84 | 1.82 | **4.12** |
| Dictionary | ported | 2.33 | 9.31 | 2.56 | 1.20 | **3.85** |
| OnPairMini10 | ported | 2.61 | 3.15 | 2.48 | 3.94 | **3.05** |
| FSST12 | ported | 1.86 | 2.37 | 1.81 | 1.31 | **1.84** |
| FSST | ported | 1.72 | 1.90 | 1.68 | 1.32 | **1.65** |
