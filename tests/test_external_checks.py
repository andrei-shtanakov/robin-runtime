"""External check liveness by receipts (issue #71)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from robin import external_checks as ec
from robin.config import RobinConfig

CONTRACT = Path(__file__).parent.parent / "contracts" / "r16-receipt"
EXAMPLES = CONTRACT / "v1" / "examples"
# Tuesday 2026-09-29 is a cycle start; Wednesday noon Tbilisi = 08:00 UTC.
WED = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)


def _example(name: str, **changes) -> dict:
    return {**json.loads((EXAMPLES / f"{name}.json").read_text()), **changes}


def _write(receipts: Path, receipt: dict | str) -> None:
    receipts.mkdir(parents=True, exist_ok=True)
    cycle = receipt["cycle_id"] if isinstance(receipt, dict) else "2026-09-29"
    body = receipt if isinstance(receipt, str) else json.dumps(receipt)
    (receipts / f"{cycle}.json").write_text(body)


def _config(tmp_path: Path) -> RobinConfig:
    return RobinConfig(
        vault_path=tmp_path,
        repo_paths=[],
        var_dir=tmp_path / "var",
        r16_receipts_dir=tmp_path / "receipts",
    )


def test_vendored_contract_matches_the_pin():
    pins = {}
    for line in (CONTRACT / "VENDORED.md").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            pins[parts[1]] = parts[0]
    files = sorted(p for p in (CONTRACT / "v1").rglob("*") if p.is_file())
    assert {str(p.relative_to(CONTRACT)) for p in files} == set(pins)
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == pins[str(path.relative_to(CONTRACT))], path


@pytest.mark.parametrize("name", ["completed-ok", "failed", "missed"])
def test_contract_examples_conform(name):
    assert ec.contract_violation(_example(name)) is None


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (WED, date(2026, 9, 29)),
        # Tuesday 10:00 Tbilisi: the new cycle is still inside its grace
        (datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc), date(2026, 9, 22)),
        # Tuesday 15:30 Tbilisi: grace over, the new cycle is expected
        (datetime(2026, 9, 29, 11, 30, tzinfo=timezone.utc), date(2026, 9, 29)),
        (datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc), date(2026, 9, 29)),
    ],
)
def test_expected_cycle(now, expected):
    assert ec.expected_cycle(now) == expected


def test_clean_cycle(tmp_path):
    _write(tmp_path, _example("completed-ok"))
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.CLEAN


def test_findings_carry_count_deadline_and_link(tmp_path):
    url = "https://github.com/o/prograph-vault/issues/7"
    _write(
        tmp_path,
        _example(
            "completed-ok",
            problems={"claims": 1, "revisions": 0, "coverage": 0},
            delivery={"action": "created", "issue": 7, "issue_url": url, "error": None},
        ),
    )
    verdict = ec.classify(tmp_path, "2026-09-29", WED)
    assert verdict.kind == ec.FINDINGS
    assert verdict.text == f"R16: находок 1 ждут разбора до 06.10 → {url}"


def test_failed_and_undelivered_are_broken(tmp_path):
    _write(tmp_path, _example("failed"))
    verdict = ec.classify(tmp_path, "2026-09-29", WED)
    assert verdict.kind == ec.BROKEN
    assert "audit did not complete" in verdict.text
    delivery = {"action": "failed", "issue": None, "issue_url": None, "error": "gh 502"}
    _write(tmp_path, _example("completed-ok", ok=False, delivery=delivery))
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.BROKEN


def test_missing_and_missed_are_no_run(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    verdict = ec.classify(tmp_path, "2026-09-29", WED)
    assert (verdict.kind, verdict.text) == (
        ec.NO_RUN,
        "R16: за цикл 2026-09-29 квитанции нет",
    )
    _write(tmp_path, _example("missed", cycle_id="2026-09-29"))
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.NO_RUN


@pytest.mark.parametrize(
    "receipt",
    [
        "{not json",
        _example("completed-ok", schema_version=2),
        _example("completed-ok", surprise=1),  # closed schema
        _example("missed", cycle_id="2026-09-29", producer={"host": "vps"}),
    ],
)
def test_unreadable_or_off_contract_is_unknown_not_clean(tmp_path, receipt):
    _write(tmp_path, receipt)
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.UNKNOWN


def test_receipt_of_another_cycle_is_unknown(tmp_path):
    receipt = _example("completed-ok", cycle_id="2026-09-22")
    (tmp_path / "2026-09-29.json").write_text(json.dumps(receipt))
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.UNKNOWN


def test_missing_directory_is_unknown(tmp_path):
    verdict = ec.classify(tmp_path / "nope", "2026-09-29", WED)
    assert verdict.kind == ec.UNKNOWN
    assert "каталог квитанций" in verdict.text


def test_pre_contract_receipt_is_read_not_rejected(tmp_path):
    # the real Mac receipt: producer without host, timestamps without an offset
    real = Path(__file__).parent / "fixtures" / "r16-pre-contract-2026-09-22.json"
    _write(tmp_path, json.loads(real.read_text()))
    assert ec.classify(tmp_path, "2026-09-22", WED).kind == ec.CLEAN


def test_pre_contract_relaxes_only_producer_host(tmp_path):
    receipt = _example("completed-ok", problems={})
    del receipt["producer"]["host"]
    _write(tmp_path, receipt)
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.UNKNOWN


def test_failure_before_the_last_attempt_is_pending_until_the_retry_is_late(tmp_path):
    failed = _example("failed", attempt=1, finished_at="2026-09-30T11:00:00+04:00")
    _write(tmp_path, failed)
    soon = datetime(2026, 9, 30, 8, 30, tzinfo=timezone.utc)  # 12:30 Tbilisi
    verdict = ec.classify(tmp_path, "2026-09-29", soon)
    assert verdict.kind == ec.PENDING
    assert ec.decide(None, verdict, soon) == (None, None)
    late = soon + timedelta(hours=2)
    assert ec.classify(tmp_path, "2026-09-29", late).kind == ec.BROKEN
    _write(tmp_path, {**failed, "attempt": 3})
    assert ec.classify(tmp_path, "2026-09-29", soon).kind == ec.BROKEN


def _v(kind: str, cycle: str = "2026-09-29") -> ec.Verdict:
    return ec.Verdict(cycle, kind, f"R16: {kind}")


def test_decide_alerts_once_per_cycle_and_kind():
    message, state = ec.decide(None, _v(ec.NO_RUN), WED)
    assert message == "⚠️ R16: no-run"
    assert ec.decide(state, _v(ec.NO_RUN), WED + timedelta(hours=1)) == (None, state)
    # a different kind or a new cycle is a new transition
    assert ec.decide(state, _v(ec.BROKEN), WED)[0] == "⚠️ R16: broken"
    assert ec.decide(state, _v(ec.NO_RUN, "2026-10-06"), WED)[0] is not None


def test_decide_reminds_once_before_deadline():
    _, state = ec.decide(None, _v(ec.FINDINGS), WED)
    monday = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 10:00 Tbilisi
    message, state = ec.decide(state, _v(ec.FINDINGS), monday)
    assert message == "⏰ Напоминание. R16: findings"
    later = monday + timedelta(hours=2)
    assert ec.decide(state, _v(ec.FINDINGS), later) == (None, state)


def test_first_alert_inside_the_reminder_window_is_not_repeated():
    monday = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # 10:00 Tbilisi
    message, state = ec.decide(None, _v(ec.UNKNOWN), monday)
    assert message == "⚠️ R16: unknown"
    later = monday + timedelta(hours=1)
    assert ec.decide(state, _v(ec.UNKNOWN), later) == (None, state)


def test_receipt_finished_in_the_future_is_unknown_not_pending(tmp_path):
    _write(tmp_path, _example("failed", attempt=1, finished_at="2026-10-02T10:00:00"))
    assert ec.classify(tmp_path, "2026-09-29", WED).kind == ec.UNKNOWN


def test_foreign_error_text_is_clipped(tmp_path):
    delivery = {"action": "skipped", "issue": None, "issue_url": None}
    _write(tmp_path, _example("failed", delivery={**delivery, "error": "x" * 5000}))
    verdict = ec.classify(tmp_path, "2026-09-29", WED)
    assert verdict.kind == ec.BROKEN
    assert len(verdict.text) < 300
    long_url = "https://github.com/o/r/issues/7?" + "q" * 5000
    findings = _example(
        "completed-ok",
        problems={"claims": 1, "revisions": 0, "coverage": 0},
        delivery={
            **delivery,
            "action": "created",
            "issue_url": long_url,
            "error": None,
        },
    )
    _write(tmp_path, findings)
    verdict = ec.classify(tmp_path, "2026-09-29", WED)
    assert verdict.kind == ec.FINDINGS
    assert len(verdict.text) < 300
    # the validator echoes the offending value: an off-contract receipt is clipped too
    _write(tmp_path, {**findings, "attempt": "x" * 5000})
    verdict = ec.classify(tmp_path, "2026-09-29", WED)
    assert verdict.kind == ec.UNKNOWN
    assert len(verdict.text) < 400


def test_decide_recovery_only_after_a_problem():
    assert ec.decide(None, _v(ec.CLEAN), WED)[0] is None
    _, state = ec.decide(None, _v(ec.UNKNOWN), WED)
    message, state = ec.decide(state, _v(ec.CLEAN), WED)
    assert message == "✅ R16: clean (восстановлено)"
    assert ec.decide(state, _v(ec.CLEAN), WED)[0] is None


def _collect(sent: list[str]):
    return lambda text: sent.append(text) or True


def test_run_dedups_across_runs(tmp_path):
    config = _config(tmp_path)
    sent: list[str] = []
    assert ec.run(config, _collect(sent), WED)
    assert ec.run(config, _collect(sent), WED + timedelta(hours=1))
    assert sent == [
        "⚠️ R16: состояние цикла 2026-09-29 неизвестно — "
        f"каталог квитанций {tmp_path / 'receipts'} недоступен"
    ]
    _write(tmp_path / "receipts", _example("completed-ok"))
    assert ec.run(config, _collect(sent), WED + timedelta(hours=2))
    assert sent[-1] == "✅ R16: цикл 2026-09-29 закрыт, находок нет (восстановлено)"


def test_failed_send_is_retried_next_run(tmp_path):
    config = _config(tmp_path)

    def boom(_: str) -> None:
        raise TimeoutError("telegram did not answer")

    assert not ec.run(config, boom, WED)
    sent: list[str] = []
    assert ec.run(config, _collect(sent), WED)
    assert len(sent) == 1


def test_log_only_delivery_is_not_recorded(tmp_path):
    # notify() returns False without a maintainer chat: the transition must not be
    # spent on a log line, or the alert never reaches the DM once a chat exists.
    config = _config(tmp_path)
    assert not ec.run(config, lambda _: False, WED)
    assert not (config.var_dir / ec.STATE_FILE).exists()
    sent: list[str] = []
    assert ec.run(config, _collect(sent), WED)
    assert len(sent) == 1


def test_reader_off_by_default(tmp_path):
    config = RobinConfig(vault_path=tmp_path, repo_paths=[], var_dir=tmp_path)
    assert ec.run(config, pytest.fail, WED)
    assert not (tmp_path / ec.STATE_FILE).exists()
