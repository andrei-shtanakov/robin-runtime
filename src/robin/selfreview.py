"""Weekly self-review duty (duties.md #3, stage 3 of the self-improvement loop).

Run by a systemd timer: `python -m robin.selfreview`. Reads the stage-2 failure log
(var/gaps.jsonl) for the window since the last run (fallback: 7 days), clusters the
failures (answer_class × fail_signal), and renders a work order: each cluster carries the
artifact a human should produce (KB-gap PR candidate / spec-runner spec draft / prompt
rewrite candidates / manual triage). Deliberately deterministic — the report is a work
order, not prose, and must run even when the LLM budget is spent.

Invariant (duties.md #3): Robin never merges and never changes itself — it only proposes.
Persisted to var/selfreview/ (Robin's own store, never the KB); destination is the
maintainer DM, falling back to the team channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import fmt, learning_events
from .config import RobinConfig, load_config
from .gaps import read_gaps
from .log import setup_logging

logger = logging.getLogger("robin.selfreview")

WINDOW_DAYS = 7
_MAX_EXAMPLES = 3

# (fail_signal, answer_class) → the artifact a human should produce for this cluster.
# Falls back on fail_signal alone; unknown combos get manual triage (never silently dropped).
_SUGGESTIONS: dict[tuple[str, str | None], str] = {
    ("zero_retrieval", "temporal"): (
        "инструментальный гэп → черновик tasks.md-спеки для spec-runner "
        "(темпоральный вопрос без evidence — сенсор не покрывает период/источник)"
    ),
    ("zero_retrieval", "kb"): (
        "KB-гэп → PR-кандидат в prograph-vault (staged-learning, read-back verified §6.4)"
    ),
    ("reformulation", None): (
        "формулировки → кандидаты в synonyms/rewrite-подсказки промпта (PR в robin-runtime)"
    ),
    ("gap_command", None): "явный фидбек — разобрать вручную (комментарий в записи)",
    (
        "thumbs_down",
        None,
    ): "явный фидбек — разобрать вручную (найти ответ по message_id)",
}
_FALLBACK_SUGGESTION = "неизвестный класс — разобрать вручную"


@dataclass
class Cluster:
    """One failure class with evidence and a proposed artifact."""

    fail_signal: str
    answer_class: str | None
    count: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def suggestion(self) -> str:
        return _SUGGESTIONS.get(
            (self.fail_signal, self.answer_class),
            _SUGGESTIONS.get((self.fail_signal, None), _FALLBACK_SUGGESTION),
        )


def _review_dir(config: RobinConfig) -> Path:
    return config.var_dir / "selfreview"


def _marker(config: RobinConfig) -> Path:
    return _review_dir(config) / "last.txt"


def window(config: RobinConfig, *, now: datetime | None = None) -> tuple[int, int]:
    """(since_ts, until_ts) — since the last run's marker, fallback one week back."""
    zone = ZoneInfo(config.tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    since = now - timedelta(days=WINDOW_DAYS)
    marker = _marker(config)
    if marker.is_file():
        try:
            since = datetime.fromtimestamp(int(marker.read_text().strip()), zone)
        except ValueError:
            logger.warning(
                "unreadable marker %s; using %d-day fallback", marker, WINDOW_DAYS
            )
    return int(min(since, now).timestamp()), int(now.timestamp())


def in_window(records: list[dict], since_ts: int, until_ts: int) -> list[dict]:
    return [r for r in records if since_ts <= int(r.get("ts", 0)) < until_ts]


def cluster(records: list[dict]) -> list[Cluster]:
    """Group by (fail_signal, answer_class); keep up to _MAX_EXAMPLES distinct questions."""
    clusters: dict[tuple[str, str | None], Cluster] = {}
    for record in records:
        key = (str(record.get("fail_signal")), record.get("answer_class"))
        entry = clusters.get(key)
        if entry is None:
            entry = clusters[key] = Cluster(fail_signal=key[0], answer_class=key[1])
        entry.count += 1
        question = record.get("question")
        if (
            question
            and question not in entry.examples
            and len(entry.examples) < _MAX_EXAMPLES
        ):
            entry.examples.append(question)
    return sorted(clusters.values(), key=lambda c: -c.count)


def render(
    clusters: list[Cluster], *, total: int, since_ts: int, until_ts: int, tz: str
) -> str:
    """Markdown work order: «N провалов → M кластеров», each with its artifact."""
    zone = ZoneInfo(tz)
    since = datetime.fromtimestamp(since_ts, zone)
    until = datetime.fromtimestamp(until_ts, zone)
    lines = [
        f"# Self-review: {since:%Y-%m-%d} — {until:%Y-%m-%d}",
        "",
        f"Провалов за окно: **{total}** → кластеров: **{len(clusters)}**.",
    ]
    if not clusters:
        lines += [
            "",
            "Сигналов не зафиксировано. Это «лог пуст», а не «провалов не было»: "
            "неявные провалы без переформулировки и без фидбека сюда не попадают.",
        ]
    for index, item in enumerate(clusters, 1):
        answer_class = item.answer_class or "—"
        lines += [
            "",
            f"## {index}. {item.fail_signal} × {answer_class} — {item.count} шт.",
            f"**Предлагаемый артефакт:** {item.suggestion}",
        ]
        lines += [f"- пример: {example}" for example in item.examples]
    lines += [
        "",
        "---",
        "Robin не меняет себя сам: каждый пункт выше — предложение для человека "
        "(duties.md #3). Разобранные кейсы — в eval-набор atp-platform (ступень 4).",
        "",
    ]
    return "\n".join(lines)


def persist(config: RobinConfig, text: str, *, now: datetime | None = None) -> Path:
    """var/selfreview/YYYY-MM-DD.md + marker refresh; §6.4 read-back verification."""
    zone = ZoneInfo(config.tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    directory = _review_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%Y-%m-%d}.md"
    path.write_text(text)
    if path.read_text() != text:  # §6.4: read-back verification, not optional
        raise RuntimeError(f"read-back verification failed for {path}")
    _marker(config).write_text(str(int(now.timestamp())))
    return path


async def post(config: RobinConfig, text: str) -> None:
    """Maintainer DM first (duty destination); fallback: team channel; else persist-only."""
    destination = config.maintainer_chat or config.telegram_channel
    if not (config.telegram_token and destination):
        logger.warning(
            "no maintainer chat / channel configured; self-review persisted only"
        )
        return
    from telegram import Bot
    from telegram.error import BadRequest

    bot = Bot(config.telegram_token)
    html = f"<b>Robin — weekly self-review</b>\n\n{fmt.escape_html(text)}"
    for part in fmt.chunk(html):
        try:
            await bot.send_message(destination, part, parse_mode="HTML")
        except BadRequest as exc:
            logger.error("§6.7 formatting-rejected send: %s", exc)
            await bot.send_message(destination, part)


def run() -> None:
    config = load_config()
    since_ts, until_ts = window(config)
    records = in_window(read_gaps(config), since_ts, until_ts)
    clusters = cluster(records)
    text = render(
        clusters, total=len(records), since_ts=since_ts, until_ts=until_ts, tz=config.tz
    )
    path = persist(config, text)
    # RD-007 M1b: every windowed gap becomes an observational LearningEvent
    # in robin's own store — graduation to durable artifacts is PR-only.
    if records:
        events_path = learning_events.emit_events(config, records)
        logger.info("learning events emitted: %s (%d)", events_path, len(records))
    logger.info(
        "self-review persisted: %s (%d gaps, %d clusters)",
        path,
        len(records),
        len(clusters),
    )
    asyncio.run(post(config, text))
    # duty runs are interactions too (§7 observability)
    config.var_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": int(time.time()),
        "surface": "selfreview",
        "n_gaps": len(records),
        "n_clusters": len(clusters),
        "ok": True,
    }
    with (config.var_dir / "interactions.jsonl").open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    setup_logging()
    run()


if __name__ == "__main__":
    main()
