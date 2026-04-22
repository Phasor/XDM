"""Unit tests for TweetScheduler — planning, min-gap, restart survival,
timezone, edge cases."""

from datetime import date, datetime, time as dtime, timedelta, timezone

import pytest

from tweeting.scheduler import TweetScheduler


class FakeSupabase:
    "Minimal supabase stub exposing only count_drafts_since."

    def __init__(self, count=0):
        self.count = count
        self.calls = []

    def count_drafts_since(self, character_name, since_iso):
        self.calls.append((character_name, since_iso))
        return self.count


def make_scheduler(
    windows=None,
    target=4,
    gap=60,
    tz="UTC",
    enabled=True,
    count=0,
):
    cfg = {"tweeting": {
        "schedule_enabled": enabled,
        "character_name": "alice",
        "posts_per_day_target": target,
        "min_gap_minutes": gap,
        "timezone": tz,
        "windows": windows if windows is not None else [
            "08:00-10:30", "12:30-14:30", "18:00-20:00", "21:30-23:00",
        ],
    }}
    return TweetScheduler(cfg, FakeSupabase(count=count))


# =========================
# Config parsing
# =========================

def test_parse_window_valid():
    assert TweetScheduler._parse_window("08:00-10:30") == (dtime(8, 0), dtime(10, 30))
    assert TweetScheduler._parse_window("00:00-23:59") == (dtime(0, 0), dtime(23, 59))


def test_parse_window_invalid_raises():
    with pytest.raises(ValueError):
        TweetScheduler._parse_window("not a window")
    with pytest.raises(ValueError):
        TweetScheduler._parse_window("08:00")  # missing end


def test_resolve_tz_utc_default():
    assert TweetScheduler._resolve_tz("UTC") is timezone.utc
    assert TweetScheduler._resolve_tz("") is timezone.utc


def test_resolve_tz_bad_name_falls_back_to_utc():
    assert TweetScheduler._resolve_tz("Not/A/Zone") is timezone.utc


def test_resolve_tz_valid_iana_name():
    tz = TweetScheduler._resolve_tz("Europe/London")
    # ZoneInfo instance, not UTC
    assert tz is not timezone.utc


# =========================
# Gating (enabled, windows)
# =========================

def test_disabled_schedule_never_fires():
    s = make_scheduler(enabled=False)
    assert s.should_fire() is False


def test_empty_windows_never_fires():
    s = make_scheduler(windows=[])
    assert s.should_fire() is False


def test_zero_target_count_never_fires():
    s = make_scheduler(target=0)
    assert s.should_fire() is False


def test_invalid_window_end_before_start_skipped():
    "A window whose end <= start produces no interval and no slots."
    s = make_scheduler(windows=["10:00-09:00"], target=1)
    assert s._window_intervals(date(2026, 1, 1)) == []


# =========================
# Planning produces correct count & placement
# =========================

def test_plan_produces_target_count_slots():
    s = make_scheduler(target=4)
    slots = s._plan_today(date(2026, 1, 1))
    assert len(slots) == 4


def test_plan_slots_within_windows():
    s = make_scheduler(target=4)
    slots = s._plan_today(date(2026, 1, 1))

    intervals = s._window_intervals(date(2026, 1, 1))
    for slot in slots:
        within = any(start <= slot["at"] <= end for start, end in intervals)
        assert within, f"slot {slot['at']} outside all configured windows"


def test_plan_slots_sorted_chronologically():
    s = make_scheduler(target=4)
    slots = s._plan_today(date(2026, 1, 1))
    times = [slot["at"] for slot in slots]
    assert times == sorted(times)


def test_plan_respects_min_gap_when_feasible():
    "With generous windows and small target, all slots must be >= min_gap apart."
    s = make_scheduler(target=3, gap=30, windows=["08:00-20:00"])
    # Run many plans to guard against flaky randomness
    for _ in range(20):
        slots = s._plan_today(date(2026, 1, 1))
        gaps = [
            (slots[i]["at"] - slots[i - 1]["at"]).total_seconds() / 60
            for i in range(1, len(slots))
        ]
        assert all(g >= 30 for g in gaps), f"gap violation: {gaps}"


def test_plan_best_effort_when_gap_infeasible(caplog):
    "If target+gap cannot fit in windows, use best-effort and warn."
    import logging
    # 1h window, target 3, gap 60m -> impossible
    s = make_scheduler(target=3, gap=60, windows=["09:00-10:00"])
    with caplog.at_level(logging.WARNING):
        slots = s._plan_today(date(2026, 1, 1))
    assert len(slots) == 3  # still returns target count (best effort)


# =========================
# Restart survival: already-created drafts pre-mark slots fired
# =========================

def test_restart_with_no_prior_drafts_marks_none_fired():
    s = make_scheduler(count=0)
    s._ensure_today_planned()
    assert all(not slot["fired"] for slot in s._slots)


def test_restart_with_two_prior_drafts_marks_first_two_fired():
    s = make_scheduler(target=4, count=2)
    s._ensure_today_planned()
    fired = [slot["fired"] for slot in s._slots]
    assert fired == [True, True, False, False]


def test_restart_with_more_drafts_than_slots_marks_all_fired():
    s = make_scheduler(target=3, count=99)
    s._ensure_today_planned()
    assert all(slot["fired"] for slot in s._slots)


def test_restart_queries_supabase_with_start_of_day():
    fake_sb = FakeSupabase(count=0)
    cfg = {"tweeting": {
        "schedule_enabled": True,
        "character_name": "alice",
        "posts_per_day_target": 2,
        "min_gap_minutes": 30,
        "timezone": "UTC",
        "windows": ["08:00-10:00"],
    }}
    s = TweetScheduler(cfg, fake_sb)
    s._ensure_today_planned()

    assert len(fake_sb.calls) == 1
    character, since_iso = fake_sb.calls[0]
    assert character == "alice"
    # since_iso should parse as a midnight-UTC timestamp for today
    parsed = datetime.fromisoformat(since_iso)
    assert parsed.hour == 0 and parsed.minute == 0


# =========================
# should_fire state machine
# =========================

def test_should_fire_returns_true_once_per_slot():
    "A single slot fires exactly once, then stays fired."
    s = make_scheduler(target=1, windows=["00:00-23:59"], gap=1)
    s._ensure_today_planned()
    # Force the one slot into the past so it's due.
    s._slots[0]["at"] = datetime.now(timezone.utc) - timedelta(hours=1)

    assert s.should_fire() is True
    assert s.should_fire() is False
    assert s.should_fire() is False


def test_should_fire_skips_future_slots():
    s = make_scheduler(target=2, windows=["00:00-23:59"], gap=1)
    s._ensure_today_planned()
    # Make both slots in the future.
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    s._slots[0]["at"] = future
    s._slots[1]["at"] = future + timedelta(hours=1)
    assert s.should_fire() is False


def test_should_fire_skips_already_fired_slots():
    "A slot marked fired by restart-survival should never trigger."
    s = make_scheduler(target=2, count=1, windows=["00:00-23:59"], gap=1)
    s._ensure_today_planned()
    # Put the first (pre-marked-fired) slot in the past. It should NOT fire.
    s._slots[0]["at"] = datetime.now(timezone.utc) - timedelta(hours=2)
    s._slots[1]["at"] = datetime.now(timezone.utc) + timedelta(hours=5)
    assert s.should_fire() is False


# =========================
# Day rollover
# =========================

def test_new_day_replans_slots(monkeypatch):
    "Crossing midnight clears the previous day's plan and generates a new one."
    s = make_scheduler(target=3)

    import tweeting.scheduler as scheduler_mod

    # Day 1
    class FakeDT1:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 1, 1, 9, 0, tzinfo=tz)
        combine = datetime.combine

    monkeypatch.setattr(scheduler_mod, "datetime", FakeDT1)
    s._ensure_today_planned()
    day1_slots = list(s._slots)
    day1_date = s._planned_date

    # Day 2
    class FakeDT2:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 1, 2, 9, 0, tzinfo=tz)
        combine = datetime.combine

    monkeypatch.setattr(scheduler_mod, "datetime", FakeDT2)
    s._ensure_today_planned()
    day2_slots = list(s._slots)
    day2_date = s._planned_date

    assert day1_date != day2_date
    # All slots in day2 should be dated 2026-01-02
    for slot in day2_slots:
        assert slot["at"].date() == date(2026, 1, 2)
