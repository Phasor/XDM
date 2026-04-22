"""
Tweet scheduler. Plans a random distribution of draft-creation slots within
configured daily windows, respecting a minimum gap between slots. Crash-safe:
on restart it reconstructs today's plan deterministically, then skips the
first N slots that have already been accounted for by drafts created today.

One call each main-loop tick: `should_fire()` returns True at most once per
scheduled slot. The main loop enqueues a compose request when that fires.

Timezone: configurable via IANA name (e.g. "Europe/London"). Defaults to
UTC so VPS-local behavior is predictable.
"""

import logging
import random
from datetime import datetime, time as dtime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # <3.9 fallback — not expected on modern VPS
    ZoneInfo = None


class TweetScheduler:
    "Plans and fires draft-creation slots within daily windows."

    MAX_ATTEMPTS = 200  # tries to satisfy min-gap before giving up

    def __init__(self, config, supabase):
        self.logger = logging.getLogger("SCHEDULER")
        self.supabase = supabase

        tw = config["tweeting"]
        self.enabled = bool(tw.get("schedule_enabled", False))
        self.character_name = tw.get("character_name", "default")
        self.target_count = int(tw.get("posts_per_day_target", 4))
        self.min_gap_minutes = int(tw.get("min_gap_minutes", 30))
        self.tz = self._resolve_tz(tw.get("timezone", "UTC"))
        self.windows = [
            self._parse_window(w) for w in tw.get("windows", [])
        ]

        self._planned_date = None
        self._slots = []  # list of {"at": datetime, "fired": bool}

    def should_fire(self):
        """Return True if a scheduled draft should be generated now. Returns
        True at most once per slot (state is marked fired)."""
        if not self.enabled:
            return False
        if not self.windows or self.target_count <= 0:
            return False

        self._ensure_today_planned()

        now = datetime.now(self.tz)
        for slot in self._slots:
            if not slot["fired"] and now >= slot["at"]:
                slot["fired"] = True
                self.logger.info(
                    "Scheduled slot firing: %s (now=%s)",
                    slot["at"].strftime("%H:%M"),
                    now.strftime("%H:%M"),
                )
                return True
        return False

    # =========================
    # Planning
    # =========================

    def _ensure_today_planned(self):
        today = datetime.now(self.tz).date()
        if today == self._planned_date:
            return
        self._slots = self._plan_today(today)
        self._planned_date = today
        # Count drafts already created today (survives restart) and mark an
        # equivalent number of earliest slots as fired so we don't double-fire.
        start_of_day = datetime.combine(
            today, dtime(0, 0), tzinfo=self.tz,
        ).astimezone(timezone.utc).isoformat()
        already = self.supabase.count_drafts_since(
            self.character_name, start_of_day,
        )
        for slot in self._slots[: min(already, len(self._slots))]:
            slot["fired"] = True

        if self._slots:
            preview = ", ".join(s["at"].strftime("%H:%M") for s in self._slots)
            self.logger.info(
                "Planned %d slots for %s [%s] (already fired: %d)",
                len(self._slots), today, preview, min(already, len(self._slots)),
            )

    def _plan_today(self, date):
        "Produce target_count random slot times within windows, respecting min_gap."
        intervals = self._window_intervals(date)
        if not intervals:
            return []

        total_seconds = sum((end - start).total_seconds() for start, end in intervals)
        if total_seconds <= 0:
            return []

        min_gap = timedelta(minutes=self.min_gap_minutes)
        best = []

        for _ in range(self.MAX_ATTEMPTS):
            candidate = sorted(
                self._sample_within(intervals, total_seconds)
                for _ in range(self.target_count)
            )
            ok = all(
                candidate[i] - candidate[i - 1] >= min_gap
                for i in range(1, len(candidate))
            )
            if ok:
                best = candidate
                break
            # Keep the best-spaced fallback: maximize the minimum gap.
            if not best or self._min_gap(candidate) > self._min_gap(best):
                best = candidate

        if best and not all(
            best[i] - best[i - 1] >= min_gap for i in range(1, len(best))
        ):
            self.logger.warning(
                "Could not space %d slots by %dm; using best-effort plan.",
                self.target_count, self.min_gap_minutes,
            )

        return [{"at": t, "fired": False} for t in best]

    def _window_intervals(self, date):
        "Convert config window strings to concrete (start, end) datetimes for today."
        out = []
        for start_t, end_t in self.windows:
            start_dt = datetime.combine(date, start_t, tzinfo=self.tz)
            end_dt = datetime.combine(date, end_t, tzinfo=self.tz)
            if end_dt <= start_dt:
                continue
            out.append((start_dt, end_dt))
        return out

    def _sample_within(self, intervals, total_seconds):
        "Uniform sample across all windows, weighted by duration."
        r = random.random() * total_seconds
        cursor = 0.0
        for start, end in intervals:
            duration = (end - start).total_seconds()
            if r <= cursor + duration:
                return start + timedelta(seconds=r - cursor)
            cursor += duration
        return intervals[-1][1]  # edge case: rounding past the end

    @staticmethod
    def _min_gap(slots):
        if len(slots) < 2:
            return timedelta(days=1)
        return min(slots[i] - slots[i - 1] for i in range(1, len(slots)))

    # =========================
    # Config parsing
    # =========================

    @staticmethod
    def _parse_window(window_str):
        "Parse 'HH:MM-HH:MM' into (time, time)."
        try:
            start_s, end_s = window_str.split("-", 1)
            sh, sm = start_s.strip().split(":")
            eh, em = end_s.strip().split(":")
            return dtime(int(sh), int(sm)), dtime(int(eh), int(em))
        except (ValueError, AttributeError) as e:
            raise ValueError(f"Invalid window {window_str!r}: {e}") from e

    @staticmethod
    def _resolve_tz(name):
        if ZoneInfo is None or not name or name.upper() == "UTC":
            return timezone.utc
        try:
            return ZoneInfo(name)
        except Exception:
            return timezone.utc
