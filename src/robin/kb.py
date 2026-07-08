"""Read-only KB tool layer for Robin (ROBIN-SPEC §3 Tool Layer, §4 read-only).

Searches the mounted knowledge repo and ecosystem repos. Never writes. Runs OUTSIDE the
agent sandbox: the agent asks, the orchestrator (this module) executes. `_cowork_output/` is
never read (ecosystem rule: runtime never reads dev-scratch; robin/duties.md).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".toml", ".sql", ".txt", ".json", ".py", ".rs"}
SKIP_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache",
    ".mypy_cache", ".hypothesis", "target", "build", "dist", ".worktrees", ".obsidian",
    "_cowork_output",  # runtime MUST NOT read dev-scratch (ROBIN-SPEC; robin/duties.md)
}
MAX_FILE_BYTES = 512 * 1024

# Robin's own generated files — never grounding for an answer (finding #8: else Robin cites itself).
SCAFFOLDING_FILES = {"KICKOFF.md", "ROBIN-SPEC.local.md", "soul.md", "QUESTIONS.md"}
SCAFFOLDING_DIRS = {"robin"}


@dataclass(frozen=True)
class Hit:
    """One search match — a citable source location."""

    path: str  # root-relative
    line: int
    text: str


class Kb:
    """Read-only search/read over a fixed set of allowed roots."""

    def __init__(self, roots: Iterable[Path]) -> None:
        self._roots = [r.resolve() for r in roots]

    def search(self, query: str, *, max_hits: int = 50) -> list[Hit]:
        """Case-insensitive literal search. Returns citable hits, capped at ``max_hits``."""
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        hits: list[Hit] = []
        for root in self._roots:
            for file in self._walk(root):
                try:
                    lines = file.read_text(errors="ignore").splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, 1):
                    if pattern.search(line):
                        rel = file.relative_to(root)
                        hits.append(Hit(str(rel), number, line.strip()[:240]))
                        if len(hits) >= max_hits:
                            return hits
        return hits

    def grounding_hits(self, query: str, *, max_hits: int = 12) -> list[Hit]:
        """Term-based grounding retrieval. Tokenizes the question, scores each line by how many
        query terms it contains, excludes Robin's own scaffolding (finding #8), and ranks
        authoritative sources first — decisions/rules/spec over notes over derived (§4).

        Naive linear scan (fine at this scale); a production build wants an index.
        """
        terms = _terms(query)
        if not terms:
            return []
        scored: list[tuple[int, int, Hit]] = []  # (authority, -term_matches, hit)
        for root in self._roots:
            for file in self._walk(root):
                rel = str(file.relative_to(root))
                if _is_scaffolding(rel):
                    continue
                try:
                    lines = file.read_text(errors="ignore").splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, 1):
                    low = line.lower()
                    matches = sum(1 for term in terms if term in low)
                    if matches:
                        hit = Hit(rel, number, line.strip()[:240])
                        scored.append((_authority(rel), -matches, hit))
        scored.sort(key=lambda s: (s[0], s[1], s[2].path, s[2].line))
        return [hit for _, _, hit in scored[:max_hits]]

    def read(self, root_relative: str) -> str:
        """Read a file, refusing any path that escapes the allowed roots (no traversal)."""
        for root in self._roots:
            candidate = (root / root_relative).resolve()
            if _is_within(candidate, root) and candidate.is_file():
                return candidate.read_text(errors="ignore")
        raise PermissionError(f"not under an allowed root: {root_relative}")

    def _walk(self, root: Path) -> Iterable[Path]:
        if not root.is_dir():
            return
        for path in root.rglob("*"):
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                    yield path
            except OSError:
                continue


def _is_within(candidate: Path, root: Path) -> bool:
    return root == candidate or root in candidate.parents


_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "is", "are", "which", "who", "what", "where", "when",
    "how", "and", "or", "for", "on", "by", "with", "does", "do", "own", "owns", "it", "its",
    "this", "that", "was", "were", "has", "have",
}


def _terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_.\-]+", query.lower())
    return [w for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _is_scaffolding(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return parts[0] in SCAFFOLDING_DIRS or parts[-1] in SCAFFOLDING_FILES


def _authority(path: str) -> int:
    """Lower is more authoritative. Grounds finding #8's source-priority ranking (§4)."""
    p = path.replace("\\", "/")
    if p.startswith(("authored/decisions/", "authored/rules/")):
        return 0
    if "/spec/" in p or p.endswith("-spec.md"):
        return 0
    if p == "CLAUDE.md" or p.endswith("/CLAUDE.md") or p.startswith("derived/contracts/"):
        return 1
    if p.startswith("authored/"):
        return 2
    if p.startswith("derived/"):
        return 3
    return 4


def _main() -> None:
    import sys

    from .config import load_config

    config = load_config()
    kb = Kb(config.read_roots())
    query = " ".join(sys.argv[1:]) or "agents-catalog.toml"
    for hit in kb.search(query, max_hits=20):
        print(f"{hit.path}:{hit.line}: {hit.text}")


if __name__ == "__main__":
    _main()
