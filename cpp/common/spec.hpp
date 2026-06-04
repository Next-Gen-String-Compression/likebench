// Parse a self-describing likebench query spec (the inline JSON the harness hands
// every binary). Only `kind == "synthetic"` is supported by the codec binaries.
#pragma once
#include <stdexcept>
#include <string>
#include <vector>

#include "../external/json/json.hpp"

namespace likebench {

enum class TermKind { Prefix, Suffix, Contains };

struct Term {
    TermKind kind;
    std::string value;
};

struct SyntheticSpec {
    std::string op;      // prefix|suffix|contains|multicontains|expr
    std::string column;  // column name (cosmetic for codecs; data is the .strings file)
    std::string value;   // for prefix/suffix/contains
    std::vector<std::string> values; // for multicontains
    bool all = true;     // multicontains: AND (all) vs OR (any)
    std::vector<Term> terms; // for expr
    std::string label;
};

inline SyntheticSpec parse_synthetic_spec(const std::string &json_text) {
    using nlohmann::json;
    json j = json::parse(json_text);
    const std::string kind = j.value("kind", "");
    if (kind != "synthetic")
        throw std::runtime_error("bench-compress-cpp only supports synthetic specs, got kind=" + kind);

    SyntheticSpec s;
    s.op = j.at("op").get<std::string>();
    s.column = j.value("column", "");
    s.label = j.value("label", s.op);

    if (s.op == "prefix" || s.op == "suffix" || s.op == "contains") {
        s.value = j.at("value").get<std::string>();
    } else if (s.op == "multicontains") {
        s.values = j.at("values").get<std::vector<std::string>>();
        s.all = j.value("mode", "all") != "any";
    } else if (s.op == "expr") {
        const auto &pred = j.at("predicate");
        const json *terms = nullptr;
        if (pred.contains("and")) {
            terms = &pred.at("and");
            s.all = true;
        } else if (pred.contains("or")) {
            terms = &pred.at("or");
            s.all = false;
        } else {
            throw std::runtime_error("expr predicate must have exactly one of and/or");
        }
        for (const auto &t : *terms) {
            Term term;
            if (t.contains("prefix")) {
                term.kind = TermKind::Prefix;
                term.value = t.at("prefix").get<std::string>();
            } else if (t.contains("suffix")) {
                term.kind = TermKind::Suffix;
                term.value = t.at("suffix").get<std::string>();
            } else if (t.contains("contains")) {
                term.kind = TermKind::Contains;
                term.value = t.at("contains").get<std::string>();
            } else {
                throw std::runtime_error("predicate term needs one of prefix/suffix/contains");
            }
            s.terms.push_back(std::move(term));
        }
    } else {
        throw std::runtime_error("unknown synthetic op: " + s.op);
    }
    return s;
}

} // namespace likebench
