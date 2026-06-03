// bench-compress-cpp — the standalone C++ string codecs (FSST, FSST12,
// Dictionary, LZ4) ported from CompressionBenchmark into likebench's uniform
// CLI/JSON contract. It reads the dependency-free `.strings` column, compresses
// it with the chosen codec, then for each iteration decodes every row and runs
// the synthetic matcher, so result_rows/result_checksum line up with the Rust
// engines. Compression metrics (ratio, encode/decode ns) ride along.
//
//   bench-compress-cpp --format raw --input col.strings --mode in-mem \
//       --query-spec '<json>' --iterations K --codec fsst|fsst12|dictionary|lz4
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "../common/bench_output.hpp"
#include "../common/checksum.hpp"
#include "../common/matcher.hpp"
#include "../common/spec.hpp"
#include "../common/strings_file.hpp"
#include "codecs/codecs.hpp"

using namespace likebench;

namespace {

constexpr size_t RANDOM_ROWS = 50000;

uint64_t now_ns() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration_cast<std::chrono::nanoseconds>(clock::now().time_since_epoch())
        .count();
}

std::string require_arg(const std::vector<std::string> &a, const std::string &key) {
    for (size_t i = 0; i + 1 < a.size(); ++i)
        if (a[i] == key) return a[i + 1];
    throw std::runtime_error("missing required argument " + key);
}
std::optional<std::string> opt_arg(const std::vector<std::string> &a, const std::string &key) {
    for (size_t i = 0; i + 1 < a.size(); ++i)
        if (a[i] == key) return a[i + 1];
    return std::nullopt;
}

// Deterministic, sorted random indices in [0, max) (mirrors the Rust engine and
// CompressionBenchmark's GenerateRandomIndices: sorted for cache locality).
std::vector<size_t> random_indices(size_t count, size_t max) {
    uint64_t state = 0x9E3779B97F4A7C15ULL;
    auto next = [&]() {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        return state;
    };
    std::vector<size_t> idx(count);
    for (auto &v : idx) v = next() % max;
    std::sort(idx.begin(), idx.end());
    return idx;
}

uint64_t file_size(const std::string &path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    return f ? static_cast<uint64_t>(f.tellg()) : 0;
}

} // namespace

int main(int argc, char **argv) {
    try {
        std::vector<std::string> args(argv + 1, argv + argc);
        const std::string format = require_arg(args, "--format");
        if (format != "raw")
            throw std::runtime_error("bench-compress-cpp only supports --format raw (.strings)");
        const std::string mode = require_arg(args, "--mode");
        if (mode != "in-mem")
            throw std::runtime_error("bench-compress-cpp only supports --mode in-mem");
        const std::string input = require_arg(args, "--input");
        const std::string spec_text = require_arg(args, "--query-spec");
        const size_t iterations = std::stoul(require_arg(args, "--iterations"));
        const std::string codec_name = opt_arg(args, "--codec").value_or("fsst");

        const SyntheticSpec spec = parse_synthetic_spec(spec_text);

        // ---- load: read column + compress ----
        const uint64_t t_load0 = now_ns();
        StringColumn col = read_strings(input);
        const size_t n = col.size();
        auto codec = make_codec(codec_name);
        const uint64_t t_comp0 = now_ns();
        codec->compress(col);
        const uint64_t compress_ns = now_ns() - t_comp0;
        const uint64_t load_ns = now_ns() - t_load0;
        const uint64_t compressed_bytes = codec->compressed_bytes();

        std::vector<uint8_t> scratch(codec->decompress_capacity());

        // ---- full decode once: decompress_ns + losslessness check ----
        const uint64_t t_dec0 = now_ns();
        for (size_t i = 0; i < n; ++i) {
            const size_t len = codec->decompress_one(i, scratch.data());
            if (len != col.length(i) || std::memcmp(scratch.data(), col.ptr(i), len) != 0)
                throw std::runtime_error("roundtrip mismatch at row " + std::to_string(i) +
                                         " (codec " + codec_name + ")");
        }
        const uint64_t decompress_ns = now_ns() - t_dec0;

        // ---- random point-access decode latency ----
        std::optional<uint64_t> decompress_random_ns;
        if (n > 0) {
            const auto idxs = random_indices(RANDOM_ROWS, n);
            const uint64_t t = now_ns();
            for (const auto i : idxs) codec->decompress_one(i, scratch.data());
            decompress_random_ns = now_ns() - t;
        }

        // ---- measured iterations: decode + match ----
        std::vector<uint64_t> iters_ns;
        iters_ns.reserve(iterations);
        uint64_t result_rows = 0;
        for (size_t it = 0; it < iterations; ++it) {
            const uint64_t t = now_ns();
            uint64_t count = 0;
            for (size_t i = 0; i < n; ++i) {
                const size_t len = codec->decompress_one(i, scratch.data());
                if (matches(spec, std::string_view(reinterpret_cast<char *>(scratch.data()), len)))
                    ++count;
            }
            iters_ns.push_back(now_ns() - t);
            result_rows = count;
        }

        BenchOutput out;
        out.engine = "compress-cpp";
        out.format = "raw";
        out.mode = "in-mem";
        out.label = spec.label;
        out.op = spec.op;
        out.column = spec.column.empty() ? std::nullopt : std::optional<std::string>(spec.column);
        out.rows = n;
        out.result_rows = result_rows;
        out.result_checksum = checksum(result_rows);
        out.file_bytes = file_size(input);
        // Uncompressed payload size, so ratio = in_memory_bytes / compressed_bytes.
        out.in_memory_bytes = col.total_bytes();
        out.load_ns = load_ns;
        out.decompress_ns = decompress_ns;
        out.pushdown = false;
        out.plan = "CppDecode[" + codec_name + "] -> matcher[" + spec.op +
                   "] (decompress-then-scan)";
        out.iters_ns = iters_ns;
        out.codec = codec_name;
        out.compress_ns = compress_ns;
        out.compressed_bytes = compressed_bytes;
        out.decompress_random_ns = decompress_random_ns;
        out.print();
        return 0;
    } catch (const std::exception &e) {
        fprintf(stderr, "bench-compress-cpp error: %s\n", e.what());
        return 1;
    }
}
