// Standalone string codecs ported from CompressionBenchmark, stripped of their
// DuckDB coupling and re-expressed against the dependency-free StringColumn.
//
// Each codec implements ICodec: compress the whole column once, report the
// compressed footprint (broken down the same way CompressionBenchmark does:
// dictionary + codes + lengths), and decode any single row on demand. The
// bench loop then decodes + matches to produce a result_rows comparable to
// every other likebench engine.
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "../../common/strings_file.hpp"
#include "../../external/fsst/fsst.h"
#include "../../external/fsst12/fsst12.h"
#include "../../external/lz4/lz4.h"
#include "../../external/onpair/include/onpair.h"
#include "../../external/onpair/include/onpair16.h"
#include "../../external/onpair/include/onpair_mini.h"
#include "../../external/robin_hood/robin_hood.h"

namespace likebench {

// --- bit-packing size accounting (port of BitPackingUtils) ------------------
inline uint8_t bits_per_value(uint64_t range) {
    if (range == 0) return 1;
    return static_cast<uint8_t>(std::ceil(std::log2(static_cast<double>(range) + 1.0)));
}
inline uint64_t bitpacked_size(uint64_t range, uint64_t n_values) {
    return (static_cast<uint64_t>(bits_per_value(range)) * n_values + 7) / 8;
}

struct ICodec {
    virtual ~ICodec() = default;
    virtual void compress(const StringColumn &col) = 0;
    virtual uint64_t compressed_bytes() const = 0;
    // Decode row i into `out` (capacity >= decompress_capacity()); return length.
    virtual size_t decompress_one(size_t i, uint8_t *out) = 0;
    // Worst-case decode buffer size a caller must provide.
    virtual size_t decompress_capacity() const = 0;
};

// ---------------------------------------------------------------------------
// FSST
// ---------------------------------------------------------------------------
class FsstCodec : public ICodec {
    fsst_encoder_t *encoder_ = nullptr;
    fsst_decoder_t decoder_{};
    std::vector<uint8_t> out_buf_;
    std::vector<size_t> clen_, coff_;
    uint64_t symbol_table_bytes_ = 0, max_len_ = 0, max_clen_ = 0;
    size_t n_ = 0;

public:
    ~FsstCodec() override {
        if (encoder_) fsst_destroy(encoder_);
    }
    void compress(const StringColumn &col) override {
        n_ = col.size();
        std::vector<size_t> lens(n_);
        std::vector<const unsigned char *> ptrs(n_);
        for (size_t i = 0; i < n_; ++i) {
            lens[i] = col.length(i);
            ptrs[i] = col.ptr(i);
            max_len_ = std::max(max_len_, static_cast<uint64_t>(lens[i]));
        }
        encoder_ = fsst_create(n_, lens.data(), ptrs.data(), 0);
        out_buf_.resize(2 * col.total_bytes() + 7 * n_ + 1024);
        clen_.resize(n_);
        std::vector<unsigned char *> cptr(n_);
        size_t got = fsst_compress(encoder_, n_, lens.data(), ptrs.data(), out_buf_.size(),
                                   out_buf_.data(), clen_.data(), cptr.data());
        if (got != n_) throw std::runtime_error("fsst_compress did not fit all strings");
        coff_.resize(n_);
        for (size_t i = 0; i < n_; ++i) {
            coff_[i] = static_cast<size_t>(cptr[i] - out_buf_.data());
            max_clen_ = std::max(max_clen_, static_cast<uint64_t>(clen_[i]));
        }
        decoder_ = fsst_decoder(encoder_);
        unsigned char hdr[FSST_MAXHEADER];
        symbol_table_bytes_ = fsst_export(encoder_, hdr);
    }
    uint64_t compressed_bytes() const override {
        uint64_t data_codes = 0;
        for (auto l : clen_) data_codes += l;
        uint64_t data_lengths = bitpacked_size(max_clen_, n_);
        return symbol_table_bytes_ + data_codes + data_lengths;
    }
    size_t decompress_one(size_t i, uint8_t *out) override {
        return fsst_decompress(&decoder_, clen_[i], out_buf_.data() + coff_[i],
                               decompress_capacity(), out);
    }
    size_t decompress_capacity() const override { return max_len_ + 32; }
};

// ---------------------------------------------------------------------------
// FSST12 (12-bit symbol variant)
// ---------------------------------------------------------------------------
class Fsst12Codec : public ICodec {
    fsst12_encoder_t *encoder_ = nullptr;
    fsst12_decoder_t decoder_{};
    std::vector<uint8_t> out_buf_;
    std::vector<size_t> clen_, coff_;
    uint64_t symbol_table_bytes_ = 0, max_len_ = 0, max_clen_ = 0;
    size_t n_ = 0;

public:
    ~Fsst12Codec() override {
        if (encoder_) fsst12_destroy(encoder_);
    }
    void compress(const StringColumn &col) override {
        n_ = col.size();
        std::vector<size_t> lens(n_);
        std::vector<const unsigned char *> ptrs(n_);
        for (size_t i = 0; i < n_; ++i) {
            lens[i] = col.length(i);
            ptrs[i] = col.ptr(i);
            max_len_ = std::max(max_len_, static_cast<uint64_t>(lens[i]));
        }
        encoder_ = fsst12_create(n_, lens.data(), ptrs.data(), 0);
        out_buf_.resize(2 * col.total_bytes() + 8 * n_ + 1024);
        clen_.resize(n_);
        std::vector<unsigned char *> cptr(n_);
        size_t got = fsst12_compress(encoder_, n_, lens.data(), ptrs.data(), out_buf_.size(),
                                     out_buf_.data(), clen_.data(), cptr.data());
        if (got != n_) throw std::runtime_error("fsst12_compress did not fit all strings");
        coff_.resize(n_);
        for (size_t i = 0; i < n_; ++i) {
            coff_[i] = static_cast<size_t>(cptr[i] - out_buf_.data());
            max_clen_ = std::max(max_clen_, static_cast<uint64_t>(clen_[i]));
        }
        decoder_ = fsst12_decoder(encoder_);
        unsigned char hdr[FSST12_MAXHEADER];
        symbol_table_bytes_ = fsst12_export(encoder_, hdr);
    }
    uint64_t compressed_bytes() const override {
        uint64_t data_codes = 0;
        for (auto l : clen_) data_codes += l;
        return symbol_table_bytes_ + data_codes + bitpacked_size(max_clen_, n_);
    }
    size_t decompress_one(size_t i, uint8_t *out) override {
        return fsst12_decompress(&decoder_, clen_[i], out_buf_.data() + coff_[i],
                                 decompress_capacity(), out);
    }
    size_t decompress_capacity() const override { return max_len_ + 32; }
};

// ---------------------------------------------------------------------------
// Dictionary (one code per string)
// ---------------------------------------------------------------------------
class DictionaryCodec : public ICodec {
    robin_hood::unordered_map<std::string, uint32_t> dict_;
    std::vector<std::string> order_;
    std::vector<uint32_t> codes_;
    uint64_t dict_strings_ = 0, max_dict_len_ = 0, max_len_ = 0;
    size_t n_ = 0;

public:
    void compress(const StringColumn &col) override {
        n_ = col.size();
        codes_.resize(n_);
        for (size_t i = 0; i < n_; ++i) {
            std::string s(col.view(i));
            max_len_ = std::max(max_len_, static_cast<uint64_t>(s.size()));
            auto [it, inserted] = dict_.try_emplace(s, static_cast<uint32_t>(order_.size()));
            if (inserted) {
                dict_strings_ += s.size();
                max_dict_len_ = std::max(max_dict_len_, static_cast<uint64_t>(s.size()));
                order_.push_back(std::move(s));
            }
            codes_[i] = it->second;
        }
    }
    uint64_t compressed_bytes() const override {
        uint64_t dict_lengths = bitpacked_size(max_dict_len_, order_.size());
        uint64_t codes = bitpacked_size(order_.empty() ? 0 : order_.size() - 1, n_);
        return dict_strings_ + dict_lengths + codes;
    }
    size_t decompress_one(size_t i, uint8_t *out) override {
        const std::string &s = order_[codes_[i]];
        std::memcpy(out, s.data(), s.size());
        return s.size();
    }
    size_t decompress_capacity() const override { return max_len_ + 32; }
};

// ---------------------------------------------------------------------------
// LZ4 (block-wise so individual rows remain decodable)
// ---------------------------------------------------------------------------
class Lz4Codec : public ICodec {
    static constexpr size_t BLOCK = 2048;
    struct Block {
        std::vector<uint8_t> comp;       // LZ4 frame for this block's raw bytes
        std::vector<uint32_t> row_off;   // n_rows+1 prefix offsets within block
        uint32_t raw_size = 0;
    };
    std::vector<Block> blocks_;
    std::vector<uint8_t> block_buf_; // scratch for the currently-decoded block
    long cached_ = -1;
    uint64_t max_len_ = 0, max_raw_ = 0;
    size_t n_ = 0;

public:
    void compress(const StringColumn &col) override {
        n_ = col.size();
        std::vector<uint8_t> raw;
        for (size_t start = 0; start < n_; start += BLOCK) {
            const size_t end = std::min(start + BLOCK, n_);
            Block b;
            b.row_off.push_back(0);
            raw.clear();
            for (size_t i = start; i < end; ++i) {
                const auto v = col.view(i);
                max_len_ = std::max(max_len_, static_cast<uint64_t>(v.size()));
                raw.insert(raw.end(), v.begin(), v.end());
                b.row_off.push_back(static_cast<uint32_t>(raw.size()));
            }
            b.raw_size = static_cast<uint32_t>(raw.size());
            max_raw_ = std::max(max_raw_, static_cast<uint64_t>(b.raw_size));
            const int bound = LZ4_compressBound(static_cast<int>(raw.size()));
            b.comp.resize(static_cast<size_t>(bound));
            const int clen = LZ4_compress_default(reinterpret_cast<const char *>(raw.data()),
                                                  reinterpret_cast<char *>(b.comp.data()),
                                                  static_cast<int>(raw.size()), bound);
            if (clen <= 0 && raw.size() > 0) throw std::runtime_error("LZ4_compress_default failed");
            b.comp.resize(static_cast<size_t>(clen));
            blocks_.push_back(std::move(b));
        }
        block_buf_.resize(max_raw_ + 32);
    }
    uint64_t compressed_bytes() const override {
        uint64_t total = 0;
        for (const auto &b : blocks_) {
            uint32_t max_row = 0;
            for (size_t k = 1; k < b.row_off.size(); ++k)
                max_row = std::max(max_row, b.row_off[k] - b.row_off[k - 1]);
            total += b.comp.size() + bitpacked_size(max_row, b.row_off.size() - 1);
        }
        return total;
    }
    size_t decompress_one(size_t i, uint8_t *out) override {
        const long bi = static_cast<long>(i / BLOCK);
        const Block &b = blocks_[bi];
        if (cached_ != bi) {
            LZ4_decompress_safe(reinterpret_cast<const char *>(b.comp.data()),
                                reinterpret_cast<char *>(block_buf_.data()),
                                static_cast<int>(b.comp.size()), static_cast<int>(b.raw_size));
            cached_ = bi;
        }
        const size_t local = i % BLOCK;
        const uint32_t a = b.row_off[local], e = b.row_off[local + 1];
        std::memcpy(out, block_buf_.data() + a, e - a);
        return e - a;
    }
    size_t decompress_capacity() const override { return max_len_ + 32; }
};

// ---------------------------------------------------------------------------
// OnPair (C++) — the algorithm author's C++ implementation, vendored from
// onpair_cpp. Treated as a *distinct algorithm* from the Rust bench-onpair: same
// idea, different implementation, so likebench reports both. Covers OnPair,
// OnPair16 and the OnPairMini<BITS> family.
// ---------------------------------------------------------------------------
template <class OP>
class OnPairCppCodec : public ICodec {
    OP op_;
    std::vector<size_t> ends_; // n+1 prefix offsets (size_t for the onpair API)
    uint64_t max_len_ = 0, uncompressed_ = 0;

public:
    void compress(const StringColumn &col) override {
        const size_t n = col.size();
        ends_.assign(col.offsets.begin(), col.offsets.end());
        for (size_t i = 0; i < n; ++i)
            max_len_ = std::max(max_len_, static_cast<uint64_t>(col.length(i)));
        uncompressed_ = col.total_bytes();
        op_ = OP(n, uncompressed_);
        op_.compress_bytes(col.data.data(), ends_);
    }
    uint64_t compressed_bytes() const override { return op_.space_used(); }
    size_t decompress_one(size_t i, uint8_t *out) override {
        return op_.decompress_string(i, out);
    }
    // onpair_cpp requires >= decoded + 16 bytes of slack in the output buffer.
    size_t decompress_capacity() const override { return max_len_ + 32; }
};

inline std::unique_ptr<ICodec> make_codec(const std::string &name) {
    if (name == "fsst") return std::make_unique<FsstCodec>();
    if (name == "fsst12") return std::make_unique<Fsst12Codec>();
    if (name == "dictionary" || name == "dict") return std::make_unique<DictionaryCodec>();
    if (name == "lz4") return std::make_unique<Lz4Codec>();
    if (name == "onpair-cpp" || name == "onpaircpp")
        return std::make_unique<OnPairCppCodec<OnPair>>();
    if (name == "onpair16-cpp" || name == "onpair16cpp")
        return std::make_unique<OnPairCppCodec<OnPair16>>();
    if (name == "onpairmini10") return std::make_unique<OnPairCppCodec<OnPairMini<10>>>();
    if (name == "onpairmini12") return std::make_unique<OnPairCppCodec<OnPairMini<12>>>();
    if (name == "onpairmini14") return std::make_unique<OnPairCppCodec<OnPairMini<14>>>();
    throw std::runtime_error(
        "unknown --codec " + name +
        " (expected fsst|fsst12|dictionary|lz4|onpair-cpp|onpair16-cpp|onpairmini{10,12,14})");
}

} // namespace likebench
