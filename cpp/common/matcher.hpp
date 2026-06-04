// The synthetic-op matcher, semantically identical to bench_core::Matcher so a
// C++ codec's `result_rows` equals the Rust/DataFusion engines' for the same op.
#pragma once
#include <string_view>

#include "spec.hpp"

namespace likebench {

inline bool starts_with(std::string_view s, std::string_view p) {
    return s.size() >= p.size() && s.compare(0, p.size(), p) == 0;
}
inline bool ends_with(std::string_view s, std::string_view p) {
    return s.size() >= p.size() && s.compare(s.size() - p.size(), p.size(), p) == 0;
}
inline bool contains(std::string_view s, std::string_view p) {
    return s.find(p) != std::string_view::npos;
}

inline bool term_hit(const Term &t, std::string_view s) {
    switch (t.kind) {
        case TermKind::Prefix: return starts_with(s, t.value);
        case TermKind::Suffix: return ends_with(s, t.value);
        case TermKind::Contains: return contains(s, t.value);
    }
    return false;
}

// Evaluate a parsed synthetic spec against one value.
inline bool matches(const SyntheticSpec &spec, std::string_view s) {
    if (spec.op == "prefix") return starts_with(s, spec.value);
    if (spec.op == "suffix") return ends_with(s, spec.value);
    if (spec.op == "contains") return contains(s, spec.value);
    if (spec.op == "multicontains") {
        if (spec.all) {
            for (const auto &v : spec.values)
                if (!contains(s, v)) return false;
            return true;
        }
        for (const auto &v : spec.values)
            if (contains(s, v)) return true;
        return false;
    }
    if (spec.op == "expr") {
        if (spec.all) {
            for (const auto &t : spec.terms)
                if (!term_hit(t, s)) return false;
            return true;
        }
        for (const auto &t : spec.terms)
            if (term_hit(t, s)) return true;
        return false;
    }
    return false;
}

} // namespace likebench
