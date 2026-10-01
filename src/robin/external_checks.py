"""Liveness of external checks, read from their receipts (issue #71).

External weekly checks (R16 claim-freshness first) run outside Robin, under their own
user. Each attempt leaves a receipt in a service-state directory on the same VPS; Robin
only READS those receipts and tells the maintainer in Telegram when the expected cycle
is not closed cleanly. Robin executes nothing and gets no GitHub write access.

The receipt form is the vendored pinned contract `contracts/r16-receipt/` (SSOT:
devtools). Deterministic, no LLM on this path. Four non-clean states are kept apart:
no run, broken run, finished with findings, and source unavailable — the last one is
UNKNOWN, never clean. Alerts are deduplicated by check_id + cycle_id + kind: one
message on the transition, one reminder before the deadline, one on recovery. Delivery
is at-least-once: state is saved only after a send that did not raise, so an ambiguous
Telegram failure may repeat a message but never drops one.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .config import RobinConfig

logger = logging.getLogger("robin.external_checks")

CHECK_ID = "r16-kb-freshness"
LABEL = "R16"
# The cycle as the runner defines it: Tuesday 09:30 -> next Tuesday 09:30, Tbilisi
# time, cycle_id = the Tuesday's date (contract README).
CYCLE_TZ = ZoneInfo("Asia/Tbilisi")
CYCLE_WEEKDAY = 1  # Tuesday
CYCLE_START = time(9, 30)
CYCLE = timedelta(days=7)
# The runner retries hourly, at most 3 attempts (09:30..11:30); the rest is margin for
# a late timer. Before start + GRACE the previous cycle is still the expected one.
GRACE = timedelta(hours=6)
# The single reminder fires this long before the deadline (= next cycle start).
REMIND_BEFORE = timedelta(hours=24)
STATE_FILE = "external_checks.json"

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "r16-receipt"
SCHEMA_DIRS = {1: _CONTRACT / "v1"}

CLEAN = "clean"
NO_RUN = "no-run"
BROKEN = "broken"
FINDINGS = "findings"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    """State of one expected cycle; `text` is the one-line alert body."""

    cycle_id: str
    kind: str
    text: str


def expected_cycle(now: datetime) -> date:
    """The latest cycle whose start + GRACE has passed: the one that must be closed."""
    local = now.astimezone(CYCLE_TZ)
    back = (local.weekday() - CYCLE_WEEKDAY) % 7
    start = datetime.combine(local.date() - timedelta(days=back), CYCLE_START, CYCLE_TZ)
    while start + GRACE > local:
        start -= CYCLE
    return start.date()


def classify(receipts_dir: Path, cycle_id: str) -> Verdict:
    """Read the receipt of `cycle_id` and name its state (contract reading rules)."""

    def verdict(kind: str, text: str) -> Verdict:
        return Verdict(cycle_id, kind, f"{LABEL}: {text}")

    def unknown(reason: str) -> Verdict:
        return verdict(UNKNOWN, f"состояние цикла {cycle_id} неизвестно — {reason}")

    path = receipts_dir / f"{cycle_id}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not receipts_dir.is_dir():
            return unknown(f"каталог квитанций {receipts_dir} недоступен")
        return verdict(NO_RUN, f"за цикл {cycle_id} квитанции нет")
    except OSError as exc:
        return unknown(f"квитанция {path} не читается ({exc.strerror or exc})")
    try:
        receipt = json.loads(raw)
    except ValueError:
        return unknown(f"квитанция {path.name} — не JSON")
    violation = contract_violation(receipt)
    if violation:
        return unknown(f"квитанция {path.name} не по контракту: {violation}")
    if receipt["cycle_id"] != cycle_id or receipt["check_id"] != CHECK_ID:
        return unknown(f"квитанция {path.name} описывает чужой цикл или проверку")

    if receipt["execution"] == "missed":
        return verdict(NO_RUN, f"цикл {cycle_id} никто не запускал (missed)")
    delivery = receipt["delivery"]
    url = delivery.get("issue_url")
    if receipt["execution"] == "failed" or not receipt["ok"]:
        reason = delivery.get("error") or receipt["execution"]
        tail = f" → {url}" if url else ""
        attempt = receipt["attempt"]
        return verdict(
            BROKEN, f"цикл {cycle_id} сломан, попытка {attempt}/3: {reason}{tail}"
        )
    found = sum(receipt["problems"].values())
    if found:
        deadline = (date.fromisoformat(cycle_id) + CYCLE).strftime("%d.%m")
        return verdict(
            FINDINGS,
            f"находок {found} ждут разбора до {deadline} → {url or 'issue не указан'}",
        )
    return verdict(CLEAN, f"цикл {cycle_id} закрыт, находок нет")


def contract_violation(receipt: object) -> str | None:
    """Why `receipt` breaks the vendored contract, or None when it conforms."""
    if not isinstance(receipt, dict):
        return "не объект"
    version = receipt.get("schema_version")
    if version not in SCHEMA_DIRS:
        return f"schema_version {version!r} не поддерживается"
    if _is_pre_contract(receipt):
        return _pre_contract_violation(receipt)
    error = best_match(_validator(version).iter_errors(receipt))
    return error.message if error else None


def _is_pre_contract(receipt: dict) -> bool:
    """Executed attempts moved from the Mac lack producer.host (README): not corrupt.

    The rule never covers `missed`: producer is forbidden there by the schema."""
    if receipt.get("execution") not in ("completed", "failed"):
        return False
    producer = receipt.get("producer")
    return not (isinstance(producer, dict) and producer.get("host"))


def _pre_contract_violation(receipt: dict) -> str | None:
    """Pre-contract history is not schema-valid; check only the fields read here."""
    shape = {
        "check_id": str,
        "cycle_id": str,
        "attempt": int,
        "ok": bool,
        "problems": dict,
        "delivery": dict,
    }
    for key, kind in shape.items():
        if not isinstance(receipt.get(key), kind):
            return f"история до контракта без поля {key}"
    if not all(isinstance(n, int) for n in receipt["problems"].values()):
        return "история до контракта: problems не числа"
    return None


@cache
def _validator(version: int) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIRS[version] / "schema.json").read_text())
    return Draft202012Validator(schema)


def decide(
    prev: dict | None, verdict: Verdict, now: datetime
) -> tuple[str | None, dict]:
    """Message to send (or None) and the state to keep — dedup by cycle + kind."""
    state = {"cycle_id": verdict.cycle_id, "kind": verdict.kind, "reminded": False}
    if verdict.kind == CLEAN:
        if prev and prev.get("kind") != CLEAN:
            return f"✅ {verdict.text} (восстановлено)", state
        return None, state
    if not prev or (prev.get("cycle_id"), prev.get("kind")) != (
        verdict.cycle_id,
        verdict.kind,
    ):
        return f"⚠️ {verdict.text}", state
    deadline = datetime.combine(
        date.fromisoformat(verdict.cycle_id) + CYCLE, CYCLE_START, CYCLE_TZ
    )
    if not prev.get("reminded") and now >= deadline - REMIND_BEFORE:
        return f"⏰ Напоминание. {verdict.text}", {**state, "reminded": True}
    return None, prev


def run(config: RobinConfig, send: Callable[[str], bool], now: datetime) -> bool:
    """Check every external source once; False when a due message was not delivered.

    `send` returns False (or raises) when the message did not reach anyone, e.g. no
    maintainer chat is configured: the state is then kept, so the alert repeats."""
    if config.r16_receipts_dir is None:
        return True
    verdict = classify(config.r16_receipts_dir, expected_cycle(now).isoformat())
    states = _load_state(config.var_dir / STATE_FILE)
    message, new = decide(states.get(CHECK_ID), verdict, now)
    logger.info("%s cycle %s: %s", CHECK_ID, verdict.cycle_id, verdict.kind)
    if message:
        try:
            delivered = send(message)
        except Exception:  # ambiguous Telegram failures included: retry next run
            logger.exception("alert for %s not delivered; will retry", CHECK_ID)
            return False
        if not delivered:
            return False
    if new != states.get(CHECK_ID):
        _save_state(config.var_dir / STATE_FILE, {**states, CHECK_ID: new})
    return True


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("%s unreadable — starting from empty alert state", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, states: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
