// Reader for the dependency-free `.strings` interchange format (STRZ), the exact
// byte layout produced by likebench's `bench_core::strings` writer and a direct
// port of CompressionBenchmark's StringCollector (offsets + concatenated bytes).
#pragma once
#include <cstdint>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace likebench {

struct StringColumn {
    std::vector<uint8_t> data;     // concatenated payloads (+ trailing slack)
    std::vector<uint64_t> offsets; // n+1 prefix-sum offsets

    size_t size() const { return offsets.empty() ? 0 : offsets.size() - 1; }
    uint64_t total_bytes() const { return offsets.empty() ? 0 : offsets.back(); }

    std::string_view view(size_t i) const {
        const auto a = offsets[i], b = offsets[i + 1];
        return {reinterpret_cast<const char *>(data.data()) + a, static_cast<size_t>(b - a)};
    }
    const uint8_t *ptr(size_t i) const { return data.data() + offsets[i]; }
    size_t length(size_t i) const { return offsets[i + 1] - offsets[i]; }
};

// Read a STRZ file. `slack` extra zero bytes are appended to `data` so codecs
// that over-read a word past a value's end stay in-bounds.
inline StringColumn read_strings(const std::string &path, size_t slack = 32) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot open strings file: " + path);
    std::vector<uint8_t> buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 24 || std::memcmp(buf.data(), "STRZ", 4) != 0)
        throw std::runtime_error(path + " is not a STRZ strings file");
    uint32_t version;
    std::memcpy(&version, buf.data() + 4, 4);
    if (version != 1) throw std::runtime_error("unsupported STRZ version");
    uint64_t n, nbytes;
    std::memcpy(&n, buf.data() + 8, 8);
    std::memcpy(&nbytes, buf.data() + 16, 8);
    const size_t off_start = 24;
    const size_t off_end = off_start + (n + 1) * 8;
    if (buf.size() < off_end + nbytes) throw std::runtime_error(path + " truncated");

    StringColumn col;
    col.offsets.resize(n + 1);
    std::memcpy(col.offsets.data(), buf.data() + off_start, (n + 1) * 8);
    col.data.resize(nbytes + slack, 0);
    std::memcpy(col.data.data(), buf.data() + off_end, nbytes);
    return col;
}

} // namespace likebench
