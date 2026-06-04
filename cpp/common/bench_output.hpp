// The single JSON object every likebench engine prints to stdout. Mirrors
// bench_core::BenchOutput, including the optional compression-metric fields.
#pragma once
#include <cstdint>
#include <optional>
#include <string>

#include "../external/json/json.hpp"

namespace likebench {

struct BenchOutput {
    std::string engine;
    std::string format;
    std::string mode;
    std::string label;
    std::optional<std::string> op;
    std::optional<std::string> column;
    uint64_t rows = 0;
    uint64_t result_rows = 0;
    std::string result_checksum;
    uint64_t file_bytes = 0;
    uint64_t in_memory_bytes = 0;
    uint64_t load_ns = 0;
    uint64_t decompress_ns = 0;
    bool pushdown = false;
    std::string plan;
    std::vector<uint64_t> iters_ns;

    // compression-quality extension (optional)
    std::optional<std::string> codec;
    std::optional<uint64_t> compress_ns;
    std::optional<uint64_t> compressed_bytes;
    std::optional<uint64_t> decompress_random_ns;

    void print() const {
        nlohmann::json j;
        j["engine"] = engine;
        j["format"] = format;
        j["mode"] = mode;
        j["label"] = label;
        j["op"] = op ? nlohmann::json(*op) : nlohmann::json(nullptr);
        j["column"] = column ? nlohmann::json(*column) : nlohmann::json(nullptr);
        j["rows"] = rows;
        j["result_rows"] = result_rows;
        j["result_checksum"] = result_checksum;
        j["file_bytes"] = file_bytes;
        j["in_memory_bytes"] = in_memory_bytes;
        j["load_ns"] = load_ns;
        j["decompress_ns"] = decompress_ns;
        j["pushdown"] = pushdown;
        j["plan"] = plan;
        j["iters_ns"] = iters_ns;
        if (codec) j["codec"] = *codec;
        if (compress_ns) j["compress_ns"] = *compress_ns;
        if (compressed_bytes) j["compressed_bytes"] = *compressed_bytes;
        if (decompress_random_ns) j["decompress_random_ns"] = *decompress_random_ns;
        printf("%s\n", j.dump().c_str());
    }
};

} // namespace likebench
