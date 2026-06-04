#ifndef ONPAIR_MINI_H
#define ONPAIR_MINI_H

#include <cstring>
#include <robin_hood.h>
#include <random>
#include <queue>
#include "lpm.h"
#include "pair_hash.h"

template<size_t BITS_PER_TOKEN = 12>
class OnPairMini {
private:
    static constexpr size_t FAST_ACCESS_SIZE = 16;
    static constexpr uint32_t MAX_TOKEN_ID = (1 << BITS_PER_TOKEN) - 1;

    // Compressed data storage
    std::vector<uint16_t> compressed_data;      // Sequence of token IDs
    std::vector<size_t> string_boundaries;      // End positions for each string

    // Dictionary storage
    std::vector<uint8_t> dictionary;            // Raw token data
    std::vector<uint16_t> token_boundaries;     // Token end positions in dictionary

public:
    /**
     * @brief Default constructor with no pre-allocation
     * 
     * Creates an OnPairMini compressor with empty vectors. Memory will be allocated
     * dynamically as needed during compression.
     */
    OnPairMini() = default;

    /**
     * @brief Construct a new OnPairMini compressor
     * 
     * @param num_strings Expected number of strings (for capacity optimization)
     * @param total_bytes Expected total size of all strings in bytes
     */
    OnPairMini(size_t num_strings, size_t total_bytes);

    /**
     * @brief Destroy the OnPairMini compressor
     */
    ~OnPairMini() = default;


    /**
     * @brief Move constructor
     */
    OnPairMini(OnPairMini&& other) noexcept = default;

    /**
     * @brief Move assignment operator
     */
    OnPairMini& operator=(OnPairMini&& other) noexcept = default;

    // Disable copy constructor and copy assignment
    OnPairMini(const OnPairMini&) = delete;
    OnPairMini& operator=(const OnPairMini&) = delete;

    /**
     * @brief Compress a collection of strings
     *
     * @param strings Vector of strings to compress
     */
    void compress_strings(const std::vector<std::string>& strings);

    /**
     * @brief Compress raw string data
     * 
     * @param data Pointer to concatenated string data
     * @param end_positions Vector of end positions for each string
     */
    void compress_bytes(const uint8_t* data, const std::vector<size_t>& end_positions);

    /**
     * @brief Decompress a specific string by index
     * 
     * @warning BUFFER SAFETY REQUIREMENT: This method uses optimized memory operations
     * that initially copy 16 bytes for each token regardless of the actual token length,
     * then copy any remaining bytes if the token is longer than 16 bytes.
     * **The buffer must have sufficient space beyond the actual decompressed data to
     * accommodate the initial 16-byte copy for the last token, or undefined behavior
     * will occur.**
     * 
     * @param index Index of the string to decompress
     * @param buffer Buffer to store the decompressed string (must be large enough + 16 bytes padding)
     * @return Size of the decompressed data in bytes
     */
    size_t decompress_string(size_t index, uint8_t* buffer) const;

    /**
     * @brief Decompress all strings
     * 
     * @warning BUFFER SAFETY REQUIREMENT: This method uses optimized memory operations
     * that initially copy 16 bytes for each token regardless of the actual token length,
     * then copy any remaining bytes if the token is longer than 16 bytes.
     * **The buffer must have sufficient space beyond the actual decompressed data to
     * accommodate the initial 16-byte copy for the last token, or undefined behavior
     * will occur.**
     * 
     * @param buffer Buffer to store all decompressed strings concatenated (must be large enough + 16 bytes padding)
     * @return Total size of decompressed data in bytes
     */
    size_t decompress_all(uint8_t* buffer) const;

    /**
     * @brief Get the total space used by the compressed data
     * 
     * @return Total memory usage in bytes
     */
    size_t space_used() const;

    size_t space_used_data_codes() const {
        return (compressed_data.size() * BITS_PER_TOKEN) / 8;
    }

    size_t space_used_dict_strings() const {
        return dictionary.size();
    }

    size_t space_used_dict_lengths() const {
        return token_boundaries.size() * sizeof(uint32_t);
    }

    std::vector<size_t> compressed_string_lengths() const {
        std::vector<size_t> lengths;
        lengths.reserve(string_boundaries.size() - 1);
        for (size_t i = 0; i < string_boundaries.size() - 1; i++) {
            lengths.push_back(string_boundaries[i + 1] - string_boundaries[i]);
        }
        return lengths;
    }
    /**
     * @brief Shrinks all internal buffers to fit their current contents
     * 
     * Reduces the capacity of all internal vectors to match their current size,
     * potentially freeing unused memory. This is useful after compression
     * is complete to minimize memory usage.
     */
    void shrink_to_fit();

private:
    /**
     * @brief Flattens a collection of strings into a single byte array with boundary positions
     * 
     * Converts a vector of strings into the internal representation used by OnPairMini:
     * a contiguous byte array with end positions marking string boundaries.
     * 
     * @param strings Vector of strings to flatten
     * @return Pair of (flattened_data, end_positions) where end_positions is a 
     *         prefix sum array starting with 0
     */
    static std::pair<std::vector<uint8_t>, std::vector<size_t>> flatten_strings(const std::vector<std::string>& strings);

    /**
     * @brief Build the token dictionary using OnPairMini's pair discovery algorithm
     * 
     * Uses longest prefix matching to parse training data and identify frequent
     * adjacent token pairs.
     * 
     * Algorithm:
     * 1. Initialize 256 single-byte tokens  
     * 2. Parse shuffled training data with longest prefix matching
     * 3. Track adjacent token pair frequencies
     * 4. Merge frequent pairs into new tokens until dictionary full (65,536 tokens)
     * 
     * @param data Pointer to concatenated string data
     * @param end_positions Vector of end positions for each string
     * @return Configured LongestPrefixMatcher containing the discovered tokens
     */
    LongestPrefixMatcher<uint16_t> train_dictionary(const uint8_t* data, const std::vector<size_t>& end_positions);

    /**
     * @brief Compress strings using the learned dictionary
     * 
     * Compresses each string independently by greedily applying longest prefix matching
     * with the constructed dictionary. Each string becomes a sequence of token IDs.
     * 
     * @param data Pointer to concatenated string data
     * @param end_positions Vector of end positions for each string
     * @param lpm Trained LongestPrefixMatcher for pattern matching
     */
    void parse_data(const uint8_t* data, const std::vector<size_t>& end_positions, const LongestPrefixMatcher<uint16_t>& lpm);
};

// Implementation
using Pair = std::pair<uint16_t, uint16_t>;

template<size_t BITS_PER_TOKEN>
OnPairMini<BITS_PER_TOKEN>::OnPairMini(size_t num_strings, size_t total_bytes) {
    compressed_data.reserve(num_strings);
    string_boundaries.reserve(total_bytes);
    dictionary.reserve(128 * 1024); // 128 KiB
    token_boundaries.reserve(1 << BITS_PER_TOKEN);
}

template<size_t BITS_PER_TOKEN>
void OnPairMini<BITS_PER_TOKEN>::compress_strings(const std::vector<std::string>& strings) {
    auto [data, end_positions] = flatten_strings(strings);
    compress_bytes(data.data(), end_positions);
}

template<size_t BITS_PER_TOKEN>
void OnPairMini<BITS_PER_TOKEN>::compress_bytes(const uint8_t* data, const std::vector<size_t>& end_positions) {
    LongestPrefixMatcher<uint16_t> lpm = train_dictionary(data, end_positions);
    parse_data(data, end_positions, lpm);
}

// Assumes buffer has enough space to store the decompressed data
template<size_t BITS_PER_TOKEN>
size_t OnPairMini<BITS_PER_TOKEN>::decompress_string(size_t index, uint8_t* buffer) const {
    const uint8_t* dict_ptr = dictionary.data();
    const uint16_t* offsets_ptr = token_boundaries.data();
    size_t size = 0;

    size_t data_start = string_boundaries[index];
    size_t data_end = string_boundaries[index + 1];

    for (size_t i = data_start; i < data_end; i++) {
        uint16_t token_id = compressed_data[i];

        size_t dict_start = offsets_ptr[token_id];
        size_t dict_end = offsets_ptr[token_id + 1];
        size_t length = dict_end - dict_start;

        // Copy the dictionary entry to the buffer
        std::memcpy(buffer + size, dict_ptr + dict_start, FAST_ACCESS_SIZE);
        if(length > FAST_ACCESS_SIZE) {
            std::memcpy(buffer + size + FAST_ACCESS_SIZE, dict_ptr + dict_start + FAST_ACCESS_SIZE, length - FAST_ACCESS_SIZE);
        }

        size += length;
    }

    return size;
}

// Assumes buffer has enough space to store the decompressed data
template<size_t BITS_PER_TOKEN>
size_t OnPairMini<BITS_PER_TOKEN>::decompress_all(uint8_t* buffer) const {
    const uint8_t* dict_ptr = dictionary.data();
    const uint16_t* offsets_ptr = token_boundaries.data();
    size_t size = 0;

    for (uint16_t token_id : compressed_data) {
        size_t dict_start = offsets_ptr[token_id];
        size_t dict_end = offsets_ptr[token_id + 1];
        size_t length = dict_end - dict_start;

        std::memcpy(buffer + size, dict_ptr + dict_start, FAST_ACCESS_SIZE);
        if(length > FAST_ACCESS_SIZE) {
            std::memcpy(buffer + size + FAST_ACCESS_SIZE, dict_ptr + dict_start + FAST_ACCESS_SIZE, length - FAST_ACCESS_SIZE);
        }

        size += length;
    }

    return size;
}

template<size_t BITS_PER_TOKEN>
size_t OnPairMini<BITS_PER_TOKEN>::space_used() const {
    return ((compressed_data.size() * BITS_PER_TOKEN) / 8) +
            dictionary.size() +
            token_boundaries.size() * sizeof(uint16_t);
}

template<size_t BITS_PER_TOKEN>
void OnPairMini<BITS_PER_TOKEN>::shrink_to_fit() {
    compressed_data.shrink_to_fit();
    string_boundaries.shrink_to_fit();
    dictionary.shrink_to_fit();
    token_boundaries.shrink_to_fit();
}

template<size_t BITS_PER_TOKEN>
LongestPrefixMatcher<uint16_t> OnPairMini<BITS_PER_TOKEN>::train_dictionary(const uint8_t* data, const std::vector<size_t>& end_positions) {
    token_boundaries.push_back(0);

    robin_hood::unordered_map<std::pair<uint32_t, uint32_t>, uint16_t, PairHash> frequency;
    LongestPrefixMatcher<uint16_t> lpm;
    uint32_t next_token_id = 256;
    bool full_dictionary = false;

    // Initialize the dictionary with single-byte tokens
    for(uint32_t i=0; i<=255; i++) {
        uint8_t value = static_cast<uint8_t>(i);
        lpm.insert(&value, 1, i);
        dictionary.push_back(value);
        token_boundaries.push_back(dictionary.size());
    }

    // Shuffle entries
    std::vector<int> shuffled_indices;
    for (int i=0; i<end_positions.size()-1; i++) {
        shuffled_indices.push_back(i);
    }
    std::random_device rd;
    std::mt19937 g(rd());
    std::shuffle(shuffled_indices.begin(), shuffled_indices.end(), g);

    // Set the threshold for merging tokens
    size_t threshold = 10;

    // Iterate over entries
    for(auto index : shuffled_indices){
        size_t start = end_positions[index];
        size_t end = end_positions[index+1];

        if (full_dictionary) {
            break;
        }

        if (start == end) {
            continue;
        }

        auto match = lpm.find_longest_match(data + start, end - start);
        uint32_t previous_token_id = match.value().first;
        size_t previous_length = match.value().second;
        size_t pos = start + previous_length;

        while (pos < end) {
            // Find the longest match
            auto match = lpm.find_longest_match(data + pos, end - pos);
            uint32_t match_token_id = match.value().first;
            size_t match_length = match.value().second;

            // Update token frequency and possibly merge tokens
            auto token_pair = std::make_pair(previous_token_id, match_token_id);
            frequency[token_pair]++;

            // TODO: stop earlier if few bytes left in dictionary
            if (frequency[token_pair] >= threshold && dictionary.size() + previous_length + match_length <= std::numeric_limits<uint16_t>::max()) {
                lpm.insert(data + pos - previous_length, previous_length + match_length, next_token_id);
                dictionary.insert(dictionary.end(), data + pos - previous_length, data + pos + match_length);
                token_boundaries.push_back(dictionary.size());

                frequency.erase(token_pair);
                previous_token_id = next_token_id;
                previous_length += match_length;

                if (next_token_id == MAX_TOKEN_ID) {
                    full_dictionary = true;
                    break;
                }

                next_token_id++;
            }
            else {
                previous_token_id = match_token_id;
                previous_length = match_length;
            }

            pos += match_length;
        }
    }

    return std::move(lpm);
}

template<size_t BITS_PER_TOKEN>
void OnPairMini<BITS_PER_TOKEN>::parse_data(const uint8_t* data, const std::vector<size_t>& end_positions, const LongestPrefixMatcher<uint16_t>& lpm) {
    string_boundaries.push_back(0);

    for(int i=0; i<end_positions.size()-1; i++) {
        size_t start = end_positions[i];
        size_t end = end_positions[i+1];

        if (start == end) {
            string_boundaries.push_back(compressed_data.size());
            continue;
        }

        size_t pos = start;
        while (pos < end) {
            // Find the longest match
            auto match = lpm.find_longest_match(data + pos, end - pos);
            uint32_t token_id = match->first;
            size_t length = match->second;
            compressed_data.push_back(token_id);
            pos += length;
        }

        string_boundaries.push_back(compressed_data.size());
    }
}

template<size_t BITS_PER_TOKEN>
std::pair<std::vector<uint8_t>, std::vector<size_t>> OnPairMini<BITS_PER_TOKEN>::flatten_strings(const std::vector<std::string>& strings) {
    // Calculate total length for efficient allocation
    size_t total_len = 0;
    for (const auto& str : strings) {
        total_len += str.size();
    }

    std::vector<uint8_t> data;
    data.reserve(total_len);

    std::vector<size_t> end_positions;
    end_positions.reserve(strings.size() + 1);
    end_positions.push_back(0);

    for (const auto& str : strings) {
        data.insert(data.end(), str.begin(), str.end());
        end_positions.push_back(data.size());
    }

    return std::make_pair(std::move(data), std::move(end_positions));
}

#endif // ONPAIR_MINI_H
