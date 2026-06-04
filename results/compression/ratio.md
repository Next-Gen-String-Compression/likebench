# Compression ratio (× vs raw UTF-8 payload)

| algorithm | source | URL | Title | Referer | SearchPhrase | **mean** |
|---|---|---|---|---|---|---|
| Parquet+zstd | new | 5.88 | 11.70 | 5.58 | 6.25 | **7.36** |
| OnPair (Rust) | new | 6.34 | 11.26 | 5.24 | 4.15 | **6.75** |
| OnPair (C++) | ported | 6.38 | 11.38 | 5.25 | 2.24 | **6.31** |
| Vortex | new | 3.59 | 9.06 | 3.79 | 4.47 | **5.23** |
| OnPairMini14 | ported | 4.23 | 5.38 | 3.67 | 4.75 | **4.51** |
| OnPair16 (Rust) | new | 4.45 | 5.58 | 3.88 | 3.70 | **4.40** |
| OnPairMini12 | ported | 4.00 | 5.07 | 3.43 | 5.00 | **4.37** |
| OnPair16 (C++) | ported | 4.44 | 5.57 | 3.98 | 2.91 | **4.22** |
| LZ4 | ported | 3.92 | 6.91 | 3.84 | 1.82 | **4.12** |
| Dictionary | ported | 2.33 | 9.31 | 2.56 | 1.20 | **3.85** |
| OnPairMini10 | ported | 2.62 | 3.10 | 2.48 | 4.00 | **3.05** |
| FSST12 | ported | 1.86 | 2.37 | 1.81 | 1.31 | **1.84** |
| FSST | ported | 1.72 | 1.90 | 1.68 | 1.32 | **1.65** |
