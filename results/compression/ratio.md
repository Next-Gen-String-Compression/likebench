# Compression ratio (× vs raw UTF-8 payload)

| algorithm | source | URL | Title | Referer | SearchPhrase | **mean** |
|---|---|---|---|---|---|---|
| Vortex | new | 5.37 | 14.21 | 5.34 | 5.53 | **7.61** |
| Parquet+zstd | new | 5.88 | 11.70 | 5.58 | 6.25 | **7.36** |
| OnPair (Rust) | new | 6.34 | 11.31 | 5.23 | 4.16 | **6.76** |
| OnPair (C++) | ported | 6.37 | 11.43 | 5.26 | 2.23 | **6.32** |
| OnPairMini14 | ported | 4.22 | 5.30 | 3.68 | 4.76 | **4.49** |
| OnPair16 (Rust) | new | 4.46 | 5.56 | 3.91 | 3.70 | **4.41** |
| OnPairMini12 | ported | 4.00 | 5.06 | 3.43 | 5.02 | **4.38** |
| OnPair16 (C++) | ported | 4.47 | 5.57 | 3.95 | 2.91 | **4.22** |
| LZ4 | ported | 3.92 | 6.91 | 3.84 | 1.82 | **4.12** |
| Dictionary | ported | 2.33 | 9.31 | 2.56 | 1.20 | **3.85** |
| OnPairMini10 | ported | 2.64 | 3.10 | 2.49 | 3.98 | **3.05** |
| FSST12 | ported | 1.86 | 2.37 | 1.81 | 1.31 | **1.84** |
| FSST | ported | 1.72 | 1.90 | 1.68 | 1.32 | **1.65** |
