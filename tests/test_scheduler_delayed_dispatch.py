"""Regression tests for GitHub dispatches queued beyond their recovery window."""
import json
from datetime import datetime, timezone

from app import scheduled_runner
from app.schedule_slots import (
    slot_already_completed, slot_from_dispatch, validate_accepted_slot,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_november_9_delayed_dispatch_retains_1530_eastern_slot():
    dispatched = _utc("2026-11-09T21:22:10Z")  # 16:22 EST
    runner_started = _utc("2026-11-09T22:12:42Z")  # 17:12 EST
    slot = slot_from_dispatch(dispatched)
    assert slot == "2026-11-09T15:30"
    assert validate_accepted_slot(slot, runner_started) == slot


def test_daylight_saving_transition_keeps_0730_eastern_schedule():
    assert slot_from_dispatch(_utc("2026-10-30T11:42:10Z")) == "2026-10-30T07:30"
    assert slot_from_dispatch(_utc("2026-11-02T12:42:10Z")) == "2026-11-02T07:30"


def test_friday_2100_slot_survives_utc_saturday_and_eastern_midnight():
    slot = slot_from_dispatch(_utc("2026-11-07T02:42:10Z"))
    assert slot == "2026-11-06T21:00"
    assert validate_accepted_slot(slot, _utc("2026-11-07T05:15:00Z")) == slot


def test_weekend_and_out_of_window_dispatches_are_not_production_slots():
    assert slot_from_dispatch(_utc("2026-11-08T14:42:00Z")) is None
    assert slot_from_dispatch(_utc("2026-11-09T22:07:00Z")) is None


def test_duplicate_or_superseded_slots_do_not_replay():
    assert slot_already_completed("2026-11-09T15:30", "2026-11-09T15:30")
    assert slot_already_completed("2026-11-09T18:30", "2026-11-09T15:30")
    assert not slot_already_completed("2026-11-04T15:30", "2026-11-09T15:30")


def test_invalid_future_weekend_and_old_slots_are_not_accepted():
    now = _utc("2026-11-09T22:12:42Z")
    assert validate_accepted_slot("2026-11-09T18:30", now) is None
    assert validate_accepted_slot("2026-11-08T10:00", now) is None
    assert validate_accepted_slot("2026-11-04T15:30", now) is None


def test_scheduled_runner_executes_valid_delayed_friday_slot_on_saturday(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduled_runner, "STATE_PATH", tmp_path / "scheduler_state.json")
    monkeypatch.setattr(scheduled_runner, "ROOT", tmp_path)
    (tmp_path / "sources.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scheduler_state.json").write_text(json.dumps({
        "last_run_at": "2026-11-06T18:30:00-05:00",
        "last_completed_slot": "2026-11-06T18:30",
    }), encoding="utf-8")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 7, 0, 15, tzinfo=tz)

    monkeypatch.setattr(scheduled_runner, "datetime", Clock)
    called = []
    monkeypatch.setattr(scheduled_runner, "run_cycle", lambda **kwargs: (
        called.append(kwargs) or {
            "cycle_id": "late-friday",
            "source_status": {},
            "source_errors": {},
        }
    ))
    result = scheduled_runner.run_scheduled(
        "sources.json", generate_resumes=False, accepted_slot="2026-11-06T21:00"
    )
    assert result["cycle_id"] == "late-friday"
    assert called
    state = json.loads((tmp_path / "scheduler_state.json").read_text(encoding="utf-8"))
    assert state["last_completed_slot"] == "2026-11-06T21:00"


def test_scheduled_runner_skips_older_delayed_slot(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduled_runner, "STATE_PATH", tmp_path / "scheduler_state.json")
    (tmp_path / "scheduler_state.json").write_text(json.dumps({
        "last_completed_slot": "2026-11-09T18:30",
    }), encoding="utf-8")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 9, 20, 10, tzinfo=tz)

    monkeypatch.setattr(scheduled_runner, "datetime", Clock)
    monkeypatch.setattr(scheduled_runner, "run_cycle", lambda **kw: (_ for _ in ()).throw(
        AssertionError("Superseded slot must not generate duplicate work")
    ))
    result = scheduled_runner.run_scheduled(accepted_slot="2026-11-09T15:30")
    assert result["status"] == "SLOT_ALREADY_COMPLETED"
