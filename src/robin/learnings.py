"""Staged → promoted learning store (ROBIN-SPEC §5, §6.4 — the M4 learning loop).

Robin writes ONLY to var/learnings/staged/ — one insight per file, dated, read-back
verified (§6.4). Everything past staged/ is a HUMAN action via the CLI
(`python -m robin.learnings`), never a chat command: §6.5 forbids chat content from
promoting memories, so the affordance is deliberately out-of-band (SSH to the host).

Promotion is also routing (§6.4): a staged learning becomes (a) a promoted memory rule
— loaded into every future session by agent._system_prompt —, (b) an update to Robin's
own skills/procedures, or (c) a proposed knowledge-repo change through the normal PR
flow. Routes (b)/(c) move the file to routed/ as an audit trail; applying the content
is the human's job — Robin never writes the KB (§4). Nothing is deleted: rejected
candidates go to rejected/.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import RobinConfig, load_config

ROUTES = ("memory", "skill", "kb")
_ROUTE_DEST = {"memory": "promoted", "skill": "routed", "kb": "routed"}

# Bounds for the system-prompt block: promoted rules must not crowd out the answer
# rules, and the block must stay cache-friendly (it changes only on promotion).
_MAX_RULES = 20
_MAX_RULE_CHARS = 600


def _store(config: RobinConfig, kind: str) -> Path:
    return config.var_dir / "learnings" / kind


def _slug(text: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-zа-яё0-9]+", text.lower())[:max_words]
    return "-".join(words) or "learning"


def stage(
    config: RobinConfig,
    *,
    question: str | None,
    comment: str | None,
    fail_signal: str,
    surface: str,
    requester: str | None = None,
    context: str | None = None,
) -> Path:
    """Write one staged learning candidate (dated, one insight per file) and verify the
    write by reading it back (§6.4 MUST — staged writes once failed silently for weeks)."""
    directory = _store(config, "staged")
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo(config.tz))
    base = f"{now:%Y-%m-%d}-{_slug(comment or question or fail_signal)}"
    path = directory / f"{base}.md"
    counter = 1
    while path.exists():  # one insight per file — never append to an existing one
        counter += 1
        path = directory / f"{base}-{counter}.md"
    content = "\n".join(
        [
            f"# Staged learning — {now:%Y-%m-%d %H:%M %Z}",
            "",
            f"- signal: {fail_signal} (surface: {surface}"
            + (f", requester: {requester})" if requester else ")"),
            *([f"- context: {context}"] if context else []),
            f"- question: {question or '(unknown — feedback on an older answer)'}",
            f"- what to do differently: {comment or '(no comment — human to fill in)'}",
            "",
            "Promote (human, out-of-band): `python -m robin.learnings promote "
            f"{path.name} --route memory|skill|kb` — or `reject {path.name}`.",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    if path.read_text(encoding="utf-8") != content:  # §6.4, not optional
        raise RuntimeError(f"read-back verification failed for {path}")
    return path


def _rule_text(raw: str) -> str:
    """Distill a promoted file into one clean rule line: the correction itself first,
    the triggering question as context; staging metadata (signal/requester/context ids)
    and boilerplate never reach the system prompt. Freeform human edits pass through."""
    question: str | None = None
    correction: str | None = None
    body: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith("Promote (human")
        ):
            continue
        if stripped.startswith("- signal:") or stripped.startswith("- context:"):
            continue
        if stripped.startswith("- question:"):
            question = stripped.removeprefix("- question:").strip()
            continue
        if stripped.startswith("- what to do differently:"):
            correction = stripped.removeprefix("- what to do differently:").strip()
            continue
        body.append(stripped.removeprefix("- ").strip())
    parts: list[str] = []
    if correction and "human to fill in" not in correction:
        parts.append(correction)
    if question and not question.startswith("(unknown"):
        parts.append(f'(triggered by: "{question}")')
    parts.extend(body)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def load_promoted(config: RobinConfig) -> list[str]:
    """Promoted rules for the system prompt (§5: MUST load into every future session).
    One rule per file, distilled by _rule_text, bounded so a rule stays one line."""
    directory = _store(config, "promoted")
    if not directory.is_dir():
        return []
    rules: list[str] = []
    for path in sorted(directory.glob("*.md"))[:_MAX_RULES]:
        text = _rule_text(path.read_text(encoding="utf-8", errors="ignore"))
        if text:
            rules.append(text[:_MAX_RULE_CHARS])
    return rules


def list_staged(config: RobinConfig) -> list[Path]:
    directory = _store(config, "staged")
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def _move_verified(source: Path, dest_dir: Path) -> Path:
    """Move with §6.4 read-back: write the copy, verify, only then remove the original."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / source.name
    content = source.read_text(encoding="utf-8")
    destination.write_text(content, encoding="utf-8")
    if destination.read_text(encoding="utf-8") != content:
        raise RuntimeError(f"read-back verification failed for {destination}")
    source.unlink()
    return destination


def promote(config: RobinConfig, name: str, route: str) -> Path:
    """HUMAN action (§5): route a staged learning. memory → promoted/ (enters every
    session); skill/kb → routed/ (audit trail; the human applies it via a PR)."""
    if route not in ROUTES:
        raise ValueError(f"unknown route {route!r}; expected one of {ROUTES}")
    source = _store(config, "staged") / Path(name).name
    if not source.is_file():
        raise FileNotFoundError(f"no staged learning named {Path(name).name}")
    destination = _move_verified(source, _store(config, _ROUTE_DEST[route]))
    _audit(config, action="promote", name=destination.name, route=route)
    return destination


def reject(config: RobinConfig, name: str) -> Path:
    """HUMAN action: archive a candidate. Nothing is deleted (KB constitution habit)."""
    source = _store(config, "staged") / Path(name).name
    if not source.is_file():
        raise FileNotFoundError(f"no staged learning named {Path(name).name}")
    destination = _move_verified(source, _store(config, "rejected"))
    _audit(config, action="reject", name=destination.name, route=None)
    return destination


def _audit(config: RobinConfig, **fields: object) -> None:
    directory = config.var_dir / "learnings"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"ts": int(time.time()), **fields}, ensure_ascii=False) + "\n"
        )


def main() -> None:
    """CLI: list | show <name> | promote <name> --route memory|skill|kb | reject <name>."""
    config = load_config()
    args = sys.argv[1:]
    command = args[0] if args else "list"
    if command == "list":
        staged = list_staged(config)
        promoted = load_promoted(config)
        print(f"staged: {len(staged)}  promoted rules in effect: {len(promoted)}")
        for path in staged:
            print(f"  {path.name}")
        if staged:
            print(
                "\nreview: python -m robin.learnings show <name>\n"
                "then:   python -m robin.learnings promote <name> --route "
                "memory|skill|kb  (or reject <name>)"
            )
    elif command == "show" and len(args) >= 2:
        print((_store(config, "staged") / Path(args[1]).name).read_text())
    elif command == "promote" and len(args) >= 2:
        route = "memory"
        if "--route" in args:
            index = args.index("--route")
            if index + 1 >= len(args):
                raise SystemExit(
                    "--route needs a value: memory | skill | kb "
                    "(e.g. promote <name> --route memory)"
                )
            route = args[index + 1]
        destination = promote(config, args[1], route)
        print(f"promoted → {destination} (route: {route})")
        if route != "memory":
            print(
                "apply it by hand via the normal PR flow — Robin never writes "
                "the KB or its own skills (§4/§6.4)."
            )
    elif command == "reject" and len(args) >= 2:
        print(f"rejected → {reject(config, args[1])}")
    else:
        raise SystemExit(
            "usage: python -m robin.learnings "
            "[list | show <name> | promote <name> --route memory|skill|kb | reject <name>]"
        )


if __name__ == "__main__":
    main()
