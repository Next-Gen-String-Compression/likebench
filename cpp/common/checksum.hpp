// FNV-1a over the 8-byte little-endian encoding of `result_rows`, byte-identical
// to `bench_core::checksum` and tests/fake_engine.py so the cross-engine
// correctness gate compares C++, Rust and Python results directly.
#pragma once
#include <cstdint>
#include <cstdio>
#include <string>

namespace likebench {

inline std::string checksum(uint64_t result_rows) {
    uint64_t h = 0xcbf29ce484222325ULL;
    for (int i = 0; i < 8; ++i) {
        uint8_t b = static_cast<uint8_t>((result_rows >> (8 * i)) & 0xff);
        h ^= b;
        h *= 0x00000100000001b3ULL; // wraps mod 2^64
    }
    char buf[19];
    std::snprintf(buf, sizeof(buf), "0x%016llx", static_cast<unsigned long long>(h));
    return std::string(buf);
}

} // namespace likebench
