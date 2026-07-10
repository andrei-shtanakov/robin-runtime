"""Failure log — stage 2 of the self-improvement loop (proposal
devtools/proposals/2026-07-10-robin-self-improvement.md).

Append-only `var/gaps.jsonl` in Robin's own store, never the KB (§7). Each record marks
one suspected answer failure; the weekly self-review duty (stage 3) clusters them and the
clusters become eval cases (stage 4). Signals:

- ``zero_retrieval`` — retrieval produced no positive evidence (detected in agent.ask);
- ``reformulation`` — the same chat re-asked an overlapping question within the window
  (strongest implicit failure marker; detected in the chat adapter);
- ``gap_command`` — explicit `/gap <comment>` from a teammate;
- ``thumbs_down`` — 👎 reaction on one of Robin's messages.

PII (§6.5): `chat` and `requester` are numeric ids, never usernames; the question text is
stored (same precedent as interactions.jsonl) but truncated. Escalation-detection ("not in
the KB" answers) belongs to the answer layer and is logged there when it lands.
"""

from __future__ import annotations

import json
import time

from .config import RobinConfig
from .kb import Hit, _terms

GAP_FILE = "gaps.jsonl"
REFORMULATION_WINDOW_S = 5 * 60
_MAX_TEXT_CHARS = 500

FAIL_SIGNALS = ("zero_retrieval", "reformulation", "gap_command", "thumbs_down")


def log_gap(
    config: RobinConfig,
    *,
    surface: str,
    fail_signal: str,
    question: str | None,
    chat: str | None = None,
    requester: str | None = None,
    retrieval_hits: int | None = None,
    answer_class: str | None = None,
    comment: str | None = None,
) -> None:
    """Append one failure record. Single-line O_APPEND writes stay atomic at this scale."""
    if fail_signal not in FAIL_SIGNALS:
        raise ValueError(f"unknown fail_signal: {fail_signal!r}")
    config.var_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": int(time.time()),
        "surface": surface,
        "chat": chat,
        "requester": requester,
        "question": question[:_MAX_TEXT_CHARS] if question else None,
        "retrieval_hits": retrieval_hits,
        "answer_class": answer_class,
        "fail_signal": fail_signal,
        "comment": comment[:_MAX_TEXT_CHARS] if comment else None,
    }
    with (config.var_dir / GAP_FILE).open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def positive_hits(sources: list[Hit]) -> int:
    """Hits that are real evidence. Synthetic markers — '(no-changes-found)' and kin —
    are statements about the search, not sources (negative-evidence rule)."""
    return sum(1 for hit in sources if not hit.path.startswith("("))


def is_zero_retrieval(sources: list[Hit]) -> bool:
    return positive_hits(sources) == 0


def is_reformulation(
    previous_question: str,
    previous_ts: float,
    question: str,
    *,
    now: float | None = None,
) -> bool:
    """A follow-up in the same chat within the window that shares at least one significant
    term with the previous question — the asker likely retries a failed answer. Term overlap
    keeps ordinary topic changes ('спасибо, а теперь про arbiter') out of the log."""
    now = now if now is not None else time.time()
    if now - previous_ts > REFORMULATION_WINDOW_S:
        return False
    previous_terms = set(_terms(previous_question))
    current_terms = set(_terms(question))
    if not previous_terms or not current_terms:
        return False
    return any(_same_stem(a, b) for a in previous_terms for b in current_terms)


_STEM_PREFIX = 5  # «изменения»/«изменилось» → 'измен'; «проекте»/«проектах» → 'проек'


def _same_stem(a: str, b: str) -> bool:
    """Exact match, or a shared prefix long enough to survive RU/EN inflection."""
    if a == b:
        return True
    prefix = min(len(a), len(b), _STEM_PREFIX)
    return prefix >= _STEM_PREFIX and a[:prefix] == b[:prefix]


def read_gaps(config: RobinConfig) -> list[dict]:
    """All records (for the stage-3 self-review duty and tests)."""
    path = config.var_dir / GAP_FILE
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
