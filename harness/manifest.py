"""Parse ``benchmarks.toml`` into engine descriptors.

This is the only place the harness learns which engines exist. Adding an engine
is: drop a crate + add a ``[[binary]]`` row. No code changes here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

VALID_MODES = {"in-mem", "full-query"}
VALID_FORMATS = {"parquet", "vortex", "raw"}
VALID_KINDS = {"synthetic", "real"}
VALID_LANGS = {"rust", "cpp"}


@dataclass(frozen=True)
class Binary:
    """One engine binary and the matrix cells it supports."""

    name: str
    bin_path: str
    lang: str
    modes: tuple[str, ...]
    formats: tuple[str, ...]
    kinds: tuple[str, ...]
    enabled: bool = True
    # Optional explicit path to the executable, relative to the repo root. When
    # set it overrides the lang-based default, letting several manifest rows
    # share one physical binary (each selecting a different ``codec``).
    bin: str | None = None
    # Optional codec selector passed through as ``--codec <codec>``. Used by the
    # standalone string-codec binaries (bench-compress-cpp, bench-onpair).
    codec: str | None = None

    def supports(self, *, mode: str, fmt: str, kind: str) -> bool:
        return mode in self.modes and fmt in self.formats and kind in self.kinds

    def binary_path(self, repo_root: Path, *, profile: str = "release") -> Path:
        """Path to the built executable.

        An explicit ``bin`` wins. Otherwise Rust binaries land in the workspace
        ``target/<profile>/<name>`` and C++ binaries at ``<bin_path>/build/<name>``.
        """
        if self.bin:
            return repo_root / self.bin
        if self.lang == "rust":
            return repo_root / "target" / profile / self.name
        return repo_root / self.bin_path / "build" / self.name

    def extra_args(self) -> list[str]:
        """Per-row CLI flags appended after the standard arguments."""
        return ["--codec", self.codec] if self.codec else []


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(f"benchmarks.toml: {msg}")


def load_manifest(path: str | Path) -> list[Binary]:
    """Load + validate the engine manifest."""
    path = Path(path)
    with path.open("rb") as fh:
        doc = tomllib.load(fh)

    binaries: list[Binary] = []
    seen: set[str] = set()
    for row in doc.get("binary", []):
        name = row["name"]
        _require(name not in seen, f"duplicate binary name {name!r}")
        seen.add(name)

        lang = row.get("lang", "rust")
        _require(lang in VALID_LANGS, f"{name}: bad lang {lang!r}")
        modes = tuple(row.get("modes", []))
        formats = tuple(row.get("formats", []))
        kinds = tuple(row.get("kinds", []))
        _require(set(modes) <= VALID_MODES, f"{name}: bad modes {modes}")
        _require(set(formats) <= VALID_FORMATS, f"{name}: bad formats {formats}")
        _require(set(kinds) <= VALID_KINDS, f"{name}: bad kinds {kinds}")
        _require(bool(modes and formats and kinds), f"{name}: empty modes/formats/kinds")

        binaries.append(
            Binary(
                name=name,
                bin_path=row.get("bin_path", ""),
                lang=lang,
                modes=modes,
                formats=formats,
                kinds=kinds,
                enabled=bool(row.get("enabled", True)),
                bin=row.get("bin"),
                codec=row.get("codec"),
            )
        )
    return binaries


def enabled_binaries(path: str | Path) -> list[Binary]:
    return [b for b in load_manifest(path) if b.enabled]
