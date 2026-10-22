"""Tests for the driver notification of cell_phone events (utils/driver_alerts.py) and
its hook in _handle_event. No Postgres: the DB-facing functions are replaced with an
in-memory fake, the driver bot with a recorder, and utils.email_sender with a fake
module injected through sys.modules."""
import asyncio
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from aiogram.utils.exceptions import BotKicked, MigrateToChat

import utils
import utils.driver_alerts as da
import utils.driver_lookup as dl
import utils.webhook_handler as wh
from utils.db_api import driver_groups, violations
from utils.samsara import safety

ET = ZoneInfo("America/New_York")


class _FakeDriverBot:
    def __init__(self, fail_with=None):
        self.calls = []          # (kind, chat_id, caption_or_text)
        self.fail_with = list(fail_with or [])

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with.pop(0)

    async def send_video(self, chat_id, media, caption=None, parse_mode=None):
        self._maybe_fail()
        self.calls.append(("video", chat_id, caption))

    async def send_photo(self, chat_id, media, caption=None, parse_mode=None):
        self._maybe_fail()
        self.calls.append(("photo", chat_id, caption))

    async def send_media_group(self, chat_id, media):
        self._maybe_fail()
        self.calls.append(("media_group", chat_id, media[0].caption))

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self._maybe_fail()
        self.calls.append(("message", chat_id, text))


class _FakeStore:
    """In-memory stand-in for violations + driver_notifications + driver_groups."""
    def __init__(self):
        self.violations = []     # dicts: event_id, vehicle, occurred_at, driver_ref, driver_name
        self.notifications = {}  # event_id -> dict
        self.groups = {}         # vehicle -> group id
        self.deactivated = []
        self.migrated = []
        self.count_calls = []

    def add_violation(self, event_id, vehicle, occurred_at):
        self.violations.append({"event_id": event_id, "vehicle": vehicle,
                                "occurred_at": occurred_at, "driver_ref": None, "driver_name": None,
                                "coaching_state": None})

    def violation(self, event_id):
        return next(v for v in self.violations if v["event_id"] == event_id)

    def install(self, monkeypatch):
        async def ensure():
            return None

        async def claim(event_id, company_slug, vehicle):
            if event_id in self.notifications:
                return False
            self.notifications[event_id] = {"vehicle": vehicle}
            return True

        async def record_driver(event_id, driver_ref, ordinal):
            self.notifications[event_id].update(driver_ref=driver_ref, ordinal=ordinal)

        async def record_sent(event_id, sent_group, sent_email):
            self.notifications[event_id].update(sent_group=sent_group, sent_email=sent_email)

        async def update_driver(event_id, driver_ref, driver_name, driver_source, on_duty):
            for v in self.violations:
                if v["event_id"] == event_id:
                    v["driver_ref"], v["driver_name"] = driver_ref, driver_name

        async def count(company_slug, driver_name, driver_ref, vehicle_number, window_start,
                        before_ts, exclude_event_id):
            self.count_calls.append((window_start, before_ts))
            name_key = violations.normalize_driver_name(driver_name)

            def same_driver(v):
                if name_key:
                    return violations.normalize_driver_name(v["driver_name"]) == name_key
                if driver_ref:
                    return v["driver_ref"] == driver_ref
                return v["vehicle"] == vehicle_number

            def gate_passed(v):  # mirrors violations._coaching_clause
                return v["coaching_state"] is None or da.safety.is_allowed(v["coaching_state"])

            return sum(
                1 for v in self.violations
                if v["event_id"] != exclude_event_id
                and window_start <= v["occurred_at"] < before_ts
                and same_driver(v)
                and gate_passed(v)
            )

        async def update_coaching(event_id, coaching_state):
            for v in self.violations:
                if v["event_id"] == event_id:
                    v["coaching_state"] = coaching_state

        async def record_gate(event_id, coaching_state, skip_reason):
            self.notifications[event_id].update(coaching_state=coaching_state,
                                                skip_reason=skip_reason)

        async def get_group(company_slug, vehicle):
            return self.groups.get(vehicle)

        async def deactivate(group_id):
            self.deactivated.append(group_id)

        async def migrate(old_id, new_id):
            self.migrated.append((old_id, new_id))

        monkeypatch.setattr(da, "ensure_driver_schema", ensure)
        monkeypatch.setattr(da, "_claim", claim)
        monkeypatch.setattr(da, "_record_driver", record_driver)
        monkeypatch.setattr(da, "_record_sent", record_sent)
        monkeypatch.setattr(da, "update_violation_driver", update_driver)
        monkeypatch.setattr(da, "count_driver_cell_phone_events", count)
        monkeypatch.setattr(da, "update_violation_coaching_state", update_coaching)
        monkeypatch.setattr(da, "_record_gate", record_gate)
        monkeypatch.setattr(da, "get_driver_group", get_group)
        monkeypatch.setattr(da, "deactivate_driver_group", deactivate)
        monkeypatch.setattr(da, "migrate_driver_group", migrate)
        monkeypatch.setattr(da, "_RETRY_DELAY", 0)


def _fake_email_module(monkeypatch, configured=True, fail=False):
    mod = types.ModuleType("utils.email_sender")
    mod.sent = []

    class EmailAttachment:
        def __init__(self, filename, content, content_type):
            self.filename, self.content, self.content_type = filename, content, content_type

    def is_configured():
        return configured

    def pick_attachments(media, is_video, max_total_bytes=20 * 1024 * 1024):
        return [EmailAttachment("a.mp4", m, "video/mp4") for m in media]

    async def send_email(to, subject, html, text, attachments=None, idempotency_key=None):
        if fail:
            raise RuntimeError("smtp down")
        mod.sent.append({"to": to, "subject": subject, "html": html, "text": text,
                         "attachments": attachments, "idempotency_key": idempotency_key})
        return True

    mod.EmailAttachment = EmailAttachment
    mod.is_configured = is_configured
    mod.pick_attachments = pick_attachments
    mod.send_email = send_email
    monkeypatch.setitem(sys.modules, "utils.email_sender", mod)
    monkeypatch.setattr(utils, "email_sender", mod, raising=False)
    return mod


def _with_driver(monkeypatch, email=None, name="John Smith", ref=None):
    async def resolve(company_slug, event, vehicle, occurred_at):
        return dl.DriverInfo(name=name, ref=ref, email=email, source="event")
    monkeypatch.setattr(dl, "resolve_driver", resolve)


@pytest.fixture
def env(monkeypatch):
    store = _FakeStore()
    store.install(monkeypatch)
    bot = _FakeDriverBot()
    monkeypatch.setattr(da, "_get_driver_bot", lambda: bot)
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_DRY_RUN", False, raising=False)
    email = _fake_email_module(monkeypatch)
    _with_driver(monkeypatch, email="driver@example.com")
    return types.SimpleNamespace(store=store, bot=bot, email=email)


_BASE_TEXT = "📵 <b>CELL PHONE USAGE</b>\n🚛 <b>Vehicle:</b> <code>T-120</code>"


async def _notify(store, event_id, occurred_at, vehicle="T-120", media=(b"VID",), save=True,
                  event=None):
    if save:  # _handle_event saves the violation before routing
        store.add_violation(event_id, vehicle, occurred_at)
    await da.notify_driver(
        company_slug="co", event=event or {"id": str(event_id), "type": "cell_phone"},
        event_id=event_id, vehicle=vehicle, occurred_at=occurred_at,
        base_text=_BASE_TEXT,
        media=list(media), is_video=True,
        video_urls=["http://vid/inward.mp4"], image_urls=[],
    )


_HEADLINE = "You will be charged"


def _drivers_in_turn(monkeypatch, *infos):
    """resolve_driver answers with each DriverInfo in turn, one per notified event."""
    answers = list(infos)

    async def resolve(company_slug, event, vehicle, occurred_at):
        return answers.pop(0)
    monkeypatch.setattr(dl, "resolve_driver", resolve)


# ── message rules ────────────────────────────────────────────────────────────────

async def test_first_event_of_the_day_is_exactly_the_company_text(env):
    env.store.groups["T-120"] = -100
    await _notify(env.store, 1, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.bot.calls[0][2] == _BASE_TEXT
    assert env.store.notifications[1]["ordinal"] == 1
    assert "charge applies" not in env.email.sent[0]["subject"]


async def test_second_event_of_the_day_has_warning_on_top_but_no_today_line(env):
    env.store.groups["T-120"] = -100
    await _notify(env.store, 2, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 3, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))

    caption = env.bot.calls[1][2]
    assert caption == da.CHARGE_HEADLINE.format(amount="") + "\n\n" + _BASE_TEXT
    assert caption.startswith("⚠️") and _HEADLINE in caption
    assert "cell phone use today" not in caption
    assert env.store.notifications[3]["ordinal"] == 2
    assert "charge applies" in env.email.sent[1]["subject"]


async def test_third_and_later_events_add_the_ordinal_today_line(env):
    env.store.groups["T-120"] = -100
    for i in range(1, 5):
        await _notify(env.store, 10 + i, datetime(2026, 10, 23, 12 + i, tzinfo=timezone.utc))

    captions = [c[2] for c in env.bot.calls]
    assert "cell phone use today" not in captions[0] and "cell phone use today" not in captions[1]
    assert _HEADLINE in captions[2] and captions[2].startswith("⚠️")
    assert captions[2].endswith(_BASE_TEXT + "\n\nThis is your 3rd cell phone use today (Oct 23, 2026).")
    assert "This is your 4th cell phone use today (Oct 23, 2026)." in captions[3]
    assert "charge applies" in env.email.sent[3]["subject"]


async def test_previous_days_do_not_count(env):
    env.store.groups["T-120"] = -100
    await _notify(env.store, 20, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    await _notify(env.store, 21, datetime(2026, 10, 6, 15, tzinfo=timezone.utc))
    assert env.bot.calls[1][2] == _BASE_TEXT


async def test_day_boundary_is_new_york_time(env):
    env.store.groups["T-120"] = -100
    last_minute = datetime(2026, 10, 22, 23, 59, tzinfo=ET)   # = Oct 23 03:59 UTC
    first_minute = datetime(2026, 10, 23, 0, 1, tzinfo=ET)    # = Oct 23 04:01 UTC
    await _notify(env.store, 10, last_minute.astimezone(timezone.utc))
    await _notify(env.store, 11, first_minute.astimezone(timezone.utc))

    assert env.bot.calls[0][2] == _BASE_TEXT
    # Same UTC day as the event before, but a new day in New York: first again.
    assert env.bot.calls[1][2] == _BASE_TEXT
    assert env.store.count_calls[1][0] == datetime(2026, 10, 23, tzinfo=ET)

    # 05:00 UTC Oct 23 = 01:00 ET: the same New York day as event 11.
    await _notify(env.store, 12, datetime(2026, 10, 23, 5, tzinfo=timezone.utc))
    assert _HEADLINE in env.bot.calls[2][2]
    # 02:00 UTC Oct 23 is still Oct 22 in New York, the day of event 10.
    await _notify(env.store, 13, datetime(2026, 10, 23, 2, tzinfo=timezone.utc))
    assert env.store.count_calls[3][0] == datetime(2026, 10, 22, tzinfo=ET)


def test_day_start_et_reads_naive_as_utc():
    # 02:00 UTC on Nov 2 is still Nov 1 in New York.
    assert da.day_start_et(datetime(2026, 11, 2, 2, 0)) == datetime(2026, 11, 1, tzinfo=ET)
    assert da.day_start_et(datetime(2026, 11, 2, 6, 0)) == datetime(2026, 11, 2, tzinfo=ET)
    assert da.day_label(datetime(2026, 11, 2, tzinfo=ET)) == "Nov 2, 2026"


# ── which driver an earlier event belongs to ─────────────────────────────────────

async def test_same_name_from_different_sources_counts_as_the_same_driver(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(
        monkeypatch,
        dl.DriverInfo(name="John Smith", ref="vitality:abc", source="vitality"),
        dl.DriverInfo(name="  john   SMITH ", ref="motive:77", source="motive"),
    )
    await _notify(env.store, 70, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 71, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))

    assert env.store.notifications[71]["ordinal"] == 2
    assert _HEADLINE in env.bot.calls[1][2]


async def test_same_name_when_a_source_gives_no_ref_still_matches(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(
        monkeypatch,
        dl.DriverInfo(name="Ann Lee", ref="vitality:abc", source="vitality"),
        dl.DriverInfo(name="Ann Lee", source="event"),
    )
    await _notify(env.store, 72, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 73, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.store.notifications[73]["ordinal"] == 2


async def test_without_a_name_the_ref_is_the_key(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(
        monkeypatch,
        dl.DriverInfo(ref="motive:77", source="motive"),
        dl.DriverInfo(ref="motive:99", source="motive"),
        dl.DriverInfo(ref="motive:77", source="motive"),
    )
    await _notify(env.store, 74, datetime(2026, 10, 5, 13, tzinfo=timezone.utc))
    await _notify(env.store, 75, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 76, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert [env.store.notifications[i]["ordinal"] for i in (74, 75, 76)] == [1, 1, 2]


async def test_with_nothing_known_the_truck_is_the_key(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(monkeypatch, dl.DriverInfo(), dl.DriverInfo())
    await _notify(env.store, 77, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 78, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.store.notifications[78]["ordinal"] == 2


async def test_different_names_on_the_same_truck_are_counted_separately(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(
        monkeypatch,
        dl.DriverInfo(name="John Smith", source="event"),
        dl.DriverInfo(name="Ann Lee", source="event"),
        dl.DriverInfo(name="john smith", source="event"),
    )
    await _notify(env.store, 79, datetime(2026, 10, 5, 13, tzinfo=timezone.utc))
    await _notify(env.store, 80, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 81, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert [env.store.notifications[i]["ordinal"] for i in (79, 80, 81)] == [1, 1, 2]


# ── go-live floor (DRIVER_ALERTS_COUNT_FROM) ─────────────────────────────────────

async def test_count_from_later_than_day_start_narrows_the_window(env, monkeypatch):
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_COUNT_FROM", "2026-10-20T10:00:00", raising=False)
    env.store.groups["T-120"] = -100
    await _notify(env.store, 40, datetime(2026, 10, 20, 12, tzinfo=timezone.utc))   # 08:00 ET, before the floor
    await _notify(env.store, 41, datetime(2026, 10, 20, 15, tzinfo=timezone.utc))   # 11:00 ET
    await _notify(env.store, 42, datetime(2026, 10, 20, 16, tzinfo=timezone.utc))

    assert env.store.count_calls[1][0] == datetime(2026, 10, 20, 10, tzinfo=ET)
    captions = [c[2] for c in env.bot.calls]
    assert captions[1] == _BASE_TEXT                       # event 40 is not counted
    assert _HEADLINE in captions[2] and "3rd" not in captions[2]


async def test_count_from_earlier_than_day_start_has_no_effect(env, monkeypatch):
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_COUNT_FROM", "2026-09-01", raising=False)
    env.store.groups["T-120"] = -100
    await _notify(env.store, 43, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.store.count_calls[0][0] == datetime(2026, 10, 5, tzinfo=ET)


def test_parse_count_from_reads_bare_date_as_et_midnight_and_keeps_offsets():
    assert da.parse_count_from("2026-11-01") == datetime(2026, 11, 1, tzinfo=ET)
    assert da.parse_count_from("2026-11-01T08:30:00") == datetime(2026, 11, 1, 8, 30, tzinfo=ET)
    aware = da.parse_count_from("2026-11-01T12:00:00+00:00")
    assert aware == datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    assert da.parse_count_from("") is None and da.parse_count_from(None) is None


async def test_invalid_count_from_is_ignored_and_warned_once(env, monkeypatch, caplog):
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_COUNT_FROM", "not-a-date", raising=False)
    monkeypatch.setattr(da, "_warned_count_from", set())
    env.store.groups["T-120"] = -100
    with caplog.at_level("WARNING", logger=da.logger.name):
        await _notify(env.store, 44, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
        await _notify(env.store, 45, datetime(2026, 10, 5, 16, tzinfo=timezone.utc))
    assert [c[0] for c in env.store.count_calls] == [datetime(2026, 10, 5, tzinfo=ET)] * 2
    assert sum("DRIVER_ALERTS_COUNT_FROM" in r.getMessage() for r in caplog.records) == 1
    assert _HEADLINE in env.bot.calls[1][2]


async def test_email_gets_an_idempotency_key_per_event(env):
    await _notify(env.store, 46, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.email.sent[0]["idempotency_key"] == "cell-phone-46"


# ── robustness ───────────────────────────────────────────────────────────────────

async def test_redelivered_event_is_sent_once(env):
    env.store.groups["T-120"] = -100
    ts = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    await _notify(env.store, 7, ts)
    await _notify(env.store, 7, ts, save=False)
    assert len(env.bot.calls) == 1
    assert len(env.email.sent) == 1


async def test_no_mapped_group_still_emails(env):
    await _notify(env.store, 20, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.bot.calls == []
    assert len(env.email.sent) == 1
    assert env.email.sent[0]["to"] == "driver@example.com"
    assert env.store.notifications[20]["sent_group"] is False
    assert env.store.notifications[20]["sent_email"] is True


async def test_email_failure_does_not_stop_telegram(env, monkeypatch):
    _fake_email_module(monkeypatch, fail=True)
    env.store.groups["T-120"] = -100
    await _notify(env.store, 21, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert len(env.bot.calls) == 1
    assert env.store.notifications[21] == {**env.store.notifications[21],
                                           "sent_group": True, "sent_email": False}


async def test_email_falls_back_to_links_without_media(env):
    await _notify(env.store, 22, datetime(2026, 10, 5, 15, tzinfo=timezone.utc), media=())
    sent = env.email.sent[0]
    assert "http://vid/inward.mp4" in sent["text"] and "http://vid/inward.mp4" in sent["html"]
    assert sent["attachments"] is None


async def test_driver_bot_not_configured_is_noop(env, monkeypatch):
    monkeypatch.setattr(da, "_get_driver_bot", lambda: None)
    env.store.groups["T-120"] = -100
    await _notify(env.store, 30, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.bot.calls == [] and env.email.sent == [] and env.store.notifications == {}


async def test_dry_run_sends_nothing(env, monkeypatch):
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_DRY_RUN", True, raising=False)
    env.store.groups["T-120"] = -100
    await _notify(env.store, 31, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.bot.calls == [] and env.email.sent == []


async def test_lookup_and_db_failures_never_escape(env, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("db down")
    async def bad_lookup(*_a, **_k):
        raise RuntimeError("lookup down")
    monkeypatch.setattr(dl, "resolve_driver", bad_lookup)
    monkeypatch.setattr(da, "count_driver_cell_phone_events", boom)
    monkeypatch.setattr(da, "get_driver_group", boom)
    await _notify(env.store, 32, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))  # no raise
    monkeypatch.setattr(da, "_claim", boom)
    await _notify(env.store, 33, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))  # no raise


async def test_motive_clip_pending_waits_for_the_delivery_with_media(env):
    env.store.groups["T-120"] = -100
    ts = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    pending = {"id": "40", "type": "cell_phone", "camera_media": {"available": True}}
    env.store.add_violation(40, "T-120", ts)
    await da.notify_driver(company_slug="co", event=pending, event_id=40, vehicle="T-120",
                           occurred_at=ts, base_text="x", media=[], is_video=False,
                           video_urls=[], image_urls=[])
    assert env.bot.calls == [] and 40 not in env.store.notifications
    await _notify(env.store, 40, ts, save=False)
    assert len(env.bot.calls) == 1


async def test_kicked_group_is_deactivated_and_migration_followed(env, monkeypatch):
    env.store.groups["T-120"] = -100
    env.bot.fail_with = [BotKicked("Forbidden: bot was kicked from the group chat")]
    await _notify(env.store, 50, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.store.deactivated == [-100] and env.bot.calls == []

    migrate = MigrateToChat("Group migrated to supergroup")
    migrate.migrate_to_chat_id = -1009
    env.bot.fail_with = [migrate]
    await _notify(env.store, 51, datetime(2026, 10, 6, 15, tzinfo=timezone.utc))
    assert env.store.migrated == [(-100, -1009)]
    assert env.bot.calls[-1][:2] == ("video", -1009)


async def test_long_text_goes_after_the_clip(env):
    env.store.groups["T-120"] = -100
    env.store.add_violation(60, "T-120", datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    await da.notify_driver(company_slug="co", event={"id": "60"}, event_id=60, vehicle="T-120",
                           occurred_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc),
                           base_text="x" * 1200, media=[b"V"], is_video=True,
                           video_urls=["u"], image_urls=[])
    assert [c[0] for c in env.bot.calls] == ["video", "message"]
    assert env.bot.calls[0][2] is None


# ── driver lookup stub ───────────────────────────────────────────────────────────

async def test_stub_lookup_reads_event_driver_and_never_raises():
    info = await dl.resolve_driver("co", {"driver": {"first_name": "Ann", "last_name": "Lee"}},
                                   "T1", None)
    assert info == dl.DriverInfo(name="Ann Lee", source="event")
    assert await dl.resolve_driver("co", {}, "T1", None) == dl.DriverInfo()
    assert await dl.resolve_driver("co", {"driver": "weird"}, "T1", None) == dl.DriverInfo()
    assert await dl.resolve_driver("co", None, "T1", None) == dl.DriverInfo()


# ── SQL helpers (query shape only) ───────────────────────────────────────────────

def test_normalize_driver_name():
    assert violations.normalize_driver_name("  John \t SMITH\n") == "john smith"
    assert violations.normalize_driver_name("  ") == "" and violations.normalize_driver_name(None) == ""


async def test_count_uses_name_then_ref_then_normalized_unit(monkeypatch):
    seen = []

    async def fetchval(query, *args):
        seen.append((query, args))
        return 3

    monkeypatch.setattr(violations.db, "fetchval", fetchval)
    ms, before = datetime(2026, 10, 1, tzinfo=ET), datetime(2026, 10, 9, tzinfo=timezone.utc)
    count = violations.count_driver_cell_phone_events
    # A name wins over a ref, and is matched on the same normalization in SQL and Python.
    assert await count("co", "  John   SMITH ", "D-9", "T-120", ms, before, 5) == 3
    assert r"lower(btrim(regexp_replace(driver_name, '\s+', ' ', 'g'))) = $5" in seen[0][0]
    assert "driver_ref" not in seen[0][0] and seen[0][1][-1] == "john smith"
    assert seen[0][1][:4] == ("co", ms, before, 5)
    # Rows the Samsara gate rejected are left out; NULL (Motive / older rows) still counts.
    assert ("(coaching_state IS NULL OR coaching_state IN "
            "('needsCoaching', 'needsRecognition', 'needsReview'))") in seen[0][0]
    assert await count("co", None, "D-9", "T-120", ms, before, 5) == 3
    assert "driver_ref = $5" in seen[1][0] and seen[1][1][-1] == "D-9"
    assert await count("co", " ", None, " t-120 ", ms, before, 5) == 3
    assert "regexp_replace(upper(vehicle_number)" in seen[2][0] and seen[2][1][-1] == "T120"
    assert await count("co", None, None, "—", ms, before, 5) == 0


async def test_get_driver_group_matches_on_unit_key(monkeypatch):
    seen = []

    async def fetchval(query, *args):
        seen.append(args)
        return -100

    monkeypatch.setattr(driver_groups.db, "fetchval", fetchval)
    assert await driver_groups.get_driver_group("co", "t 120") == -100
    assert seen == [("co", "T120")]
    assert await driver_groups.get_driver_group("co", "") is None


async def test_get_driver_group_falls_back_to_first_token(monkeypatch):
    seen = []
    rows = {"4063": -200}

    async def fetchval(query, *args):
        seen.append(args)
        return rows.get(args[1])

    monkeypatch.setattr(driver_groups.db, "fetchval", fetchval)
    assert await driver_groups.get_driver_group("co", "4063   O/O") == -200
    assert seen == [("co", "4063OO"), ("co", "4063")]
    seen.clear()
    rows["4063OO"] = -300  # an exact match wins and stops the lookup
    assert await driver_groups.get_driver_group("co", "4063 O/O") == -300
    assert seen == [("co", "4063OO")]
    assert await driver_groups.get_driver_group("co", "9999 SUB") is None


# ── _handle_event hook ───────────────────────────────────────────────────────────

def _async_const(value):
    async def _f(*_a, **_k):
        return value
    return _f


class _FakeMainBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self.sent.append(chat_id)

    async def send_video(self, chat_id, media, caption=None, parse_mode=None):
        self.sent.append(chat_id)


def _patch_handle_event(monkeypatch, group_ids, dm_ids):
    calls = []

    async def fake_notify(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(wh, "get_company_name", _async_const("Co"))
    monkeypatch.setattr(wh, "save_violation", _async_const(None))
    monkeypatch.setattr(wh, "get_groups_for_event", _async_const(group_ids))
    monkeypatch.setattr(wh, "get_subscribed_admins", _async_const(dm_ids))
    monkeypatch.setattr(wh, "notify_driver", fake_notify)
    monkeypatch.setattr(wh, "driver_alerts_enabled", lambda: True)
    return calls


async def test_handle_event_other_types_do_not_notify_driver(monkeypatch):
    calls = _patch_handle_event(monkeypatch, [1], [])
    bot = _FakeMainBot()
    await wh._handle_event(bot, {"id": "900", "type": "hard_brake", "vehicle": {"number": "T1"},
                                 "start_time": "2026-10-05T15:00:00Z"}, "co")
    assert bot.sent == [1] and calls == []


async def test_handle_event_cell_phone_without_company_targets_still_notifies(monkeypatch):
    calls = _patch_handle_event(monkeypatch, [], [])
    bot = _FakeMainBot()
    await wh._handle_event(bot, {"id": "901", "type": "cell_phone", "vehicle": {"number": "T1"},
                                 "start_time": "2026-10-05T15:00:00Z"}, "co")
    assert bot.sent == []
    assert len(calls) == 1
    c = calls[0]
    assert c["event_id"] == 901 and c["vehicle"] == "T1" and c["company_slug"] == "co"
    assert "CELL PHONE USAGE" in c["base_text"]
    assert c["occurred_at"] == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


async def test_handle_event_cell_phone_with_targets_sends_both(monkeypatch):
    calls = _patch_handle_event(monkeypatch, [1], [2])
    bot = _FakeMainBot()
    await wh._handle_event(bot, {"id": "902", "type": "cell_phone", "vehicle": {"number": "T1"},
                                 "start_time": "2026-10-05T15:00:00Z"}, "co")
    assert bot.sent == [1, 2] and len(calls) == 1


async def test_handle_event_cell_phone_driver_disabled_no_targets_skips(monkeypatch):
    calls = _patch_handle_event(monkeypatch, [], [])
    monkeypatch.setattr(wh, "driver_alerts_enabled", lambda: False)
    await wh._handle_event(_FakeMainBot(), {"id": "903", "type": "cell_phone",
                                            "vehicle": {"number": "T1"}}, "co")
    assert calls == []


# ── Samsara coaching-state gate ──────────────────────────────────────────────────

def _lookup_result(state=None, status="found"):
    event = {"id": "se-1", "coachingState": state} if status == "found" else None
    return safety.SafetyLookup(status, event=event, attempts=1)


def _fake_wait(monkeypatch, *results, store=None, gate=None):
    """Replace the Samsara wait. Answers with each result in turn (the last repeats).
    Records each call and, when given a store, the violations' coaching_state seen
    while waiting. `gate` (an asyncio.Event) holds the wait until it is set."""
    calls = []
    answers = list(results)

    async def wait(api_key, vehicle_id, at, **_kw):
        calls.append({"api_key": api_key, "vehicle_id": vehicle_id, "at": at,
                      "during": [v["coaching_state"] for v in store.violations] if store else None})
        if gate is not None:
            await gate.wait()
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(da.safety, "wait_for_safety_event", wait)
    return calls


async def _notify_samsara(store, event_id, occurred_at, vehicle="T-120", api_key="KEY",
                          vehicle_id="281474977", save=True, extra=None):
    if save:
        store.add_violation(event_id, vehicle, occurred_at)
    event = {"id": f"uuid-{event_id}", "type": "cell_phone", "_source": "samsara",
             "_samsara_vehicle_id": vehicle_id, **(extra or {})}
    await da.notify_driver(
        company_slug="co", event=event, event_id=event_id, vehicle=vehicle,
        occurred_at=occurred_at, base_text=_BASE_TEXT, media=[b"VID"], is_video=True,
        video_urls=["http://vid/inward.mp4"], image_urls=[], samsara_api_key=api_key,
    )


_TS = datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize("state", ["needsReview", "needsCoaching", "needsRecognition"])
async def test_samsara_allowed_states_notify_the_driver(env, monkeypatch, state):
    env.store.groups["T-120"] = -100
    calls = _fake_wait(monkeypatch, _lookup_result(state), store=env.store)
    await _notify_samsara(env.store, 100, _TS)

    assert len(env.bot.calls) == 1 and len(env.email.sent) == 1
    assert calls[0]["api_key"] == "KEY" and calls[0]["vehicle_id"] == "281474977"
    assert calls[0]["during"] == [safety.STATE_PENDING]   # marked before the wait
    assert env.store.violation(100)["coaching_state"] == state
    n = env.store.notifications[100]
    assert n["coaching_state"] == state and n["skip_reason"] is None
    assert n["ordinal"] == 1 and n["sent_group"] is True


@pytest.mark.parametrize("state", ["coached", "dismissed", "autoDismissed", "reviewed",
                                   "archived", "manualReview", "recognized", "invalid",
                                   "someStateSamsaraAddsLater", None])
async def test_samsara_other_states_skip_the_driver(env, monkeypatch, state):
    env.store.groups["T-120"] = -100
    _fake_wait(monkeypatch, _lookup_result(state))
    await _notify_samsara(env.store, 101, _TS)

    assert env.bot.calls == [] and env.email.sent == []
    expected = state or "unknown"
    assert env.store.violation(101)["coaching_state"] == expected
    n = env.store.notifications[101]
    assert n["coaching_state"] == expected and n["skip_reason"] == da.SKIP_NOT_ALLOWED
    assert "ordinal" not in n and "sent_group" not in n


@pytest.mark.parametrize("status,state,reason", [
    ("not_found", safety.STATE_NOT_FOUND, da.SKIP_NOT_FOUND),
    ("error", safety.STATE_LOOKUP_ERROR, da.SKIP_LOOKUP_ERROR),
])
async def test_samsara_no_safety_event_fails_closed(env, monkeypatch, caplog, status, state, reason):
    env.store.groups["T-120"] = -100
    _fake_wait(monkeypatch, _lookup_result(status=status))
    with caplog.at_level("INFO", logger=da.logger.name):
        await _notify_samsara(env.store, 102, _TS)
    assert env.bot.calls == [] and env.email.sent == []
    assert env.store.violation(102)["coaching_state"] == state
    assert env.store.notifications[102]["skip_reason"] == reason
    assert any(reason in r.getMessage() for r in caplog.records)


async def test_samsara_without_api_key_or_vehicle_id_skips_without_waiting(env, monkeypatch):
    env.store.groups["T-120"] = -100
    calls = _fake_wait(monkeypatch, _lookup_result("needsReview"))
    await _notify_samsara(env.store, 103, _TS, api_key=None)
    await _notify_samsara(env.store, 104, _TS, vehicle_id="")
    assert calls == [] and env.bot.calls == [] and env.email.sent == []
    assert env.store.notifications[103]["skip_reason"] == da.SKIP_NO_API_KEY
    assert env.store.notifications[104]["skip_reason"] == da.SKIP_NO_VEHICLE_ID
    assert env.store.violation(103)["coaching_state"] == safety.STATE_LOOKUP_ERROR


async def test_samsara_lookup_uses_the_harsh_event_timestamp(env, monkeypatch):
    calls = _fake_wait(monkeypatch, _lookup_result("needsReview"))
    await _notify_samsara(env.store, 105, _TS, extra={"_samsara_timestamp_ms": 1759676405000})
    assert calls[0]["at"] == datetime.fromtimestamp(1759676405, tz=timezone.utc)
    await _notify_samsara(env.store, 106, _TS)
    assert calls[1]["at"] == _TS


async def test_motive_events_are_not_gated(env, monkeypatch):
    env.store.groups["T-120"] = -100
    calls = _fake_wait(monkeypatch, _lookup_result("dismissed"))
    await _notify(env.store, 107, _TS)
    assert calls == [] and len(env.bot.calls) == 1
    assert env.store.violation(107)["coaching_state"] is None
    assert "skip_reason" not in env.store.notifications[107]


async def test_samsara_redelivery_does_not_start_a_second_waiter(env, monkeypatch):
    env.store.groups["T-120"] = -100
    release = asyncio.Event()
    calls = _fake_wait(monkeypatch, _lookup_result("needsReview"), gate=release)
    first = asyncio.create_task(_notify_samsara(env.store, 108, _TS))
    while not calls:                       # until the first delivery is waiting
        await asyncio.sleep(0)
    await _notify_samsara(env.store, 108, _TS, save=False)   # redelivery: returns at once
    assert len(calls) == 1 and env.bot.calls == []
    release.set()
    await first
    assert len(calls) == 1 and len(env.bot.calls) == 1 and len(env.email.sent) == 1


async def test_rejected_samsara_events_do_not_count_toward_the_ordinal(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _fake_wait(monkeypatch,
               _lookup_result("dismissed"),            # 110: rejected
               _lookup_result(status="not_found"),     # 111: rejected (fail closed)
               _lookup_result("needsReview"),          # 112: the 1st that counts
               _lookup_result("needsCoaching"))        # 113: the 2nd
    for i in range(4):
        await _notify_samsara(env.store, 110 + i, datetime(2026, 10, 5, 13 + i, tzinfo=timezone.utc))
    assert env.store.notifications[112]["ordinal"] == 1
    assert env.bot.calls[0][2] == _BASE_TEXT
    assert env.store.notifications[113]["ordinal"] == 2
    assert _HEADLINE in env.bot.calls[1][2] and len(env.bot.calls) == 2


async def test_motive_rows_without_a_coaching_state_still_count(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _fake_wait(monkeypatch, _lookup_result("needsReview"))
    await _notify(env.store, 120, datetime(2026, 10, 5, 13, tzinfo=timezone.utc))
    await _notify_samsara(env.store, 121, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    assert env.store.notifications[121]["ordinal"] == 2


@pytest.mark.parametrize("state,word", [("needsReview", "would send"), ("coached", "skipped")])
async def test_samsara_dry_run_looks_up_and_logs_the_decision(env, monkeypatch, caplog, state, word):
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_DRY_RUN", True, raising=False)
    env.store.groups["T-120"] = -100
    calls = _fake_wait(monkeypatch, _lookup_result(state))
    with caplog.at_level("INFO", logger=da.logger.name):
        await _notify_samsara(env.store, 130, _TS)
    assert len(calls) == 1 and env.bot.calls == [] and env.email.sent == []
    assert env.store.violation(130)["coaching_state"] == state
    logs = [r.getMessage() for r in caplog.records if "DRY RUN" in r.getMessage()]
    assert len(logs) == 1 and word in logs[0] and f"coachingState={state}" in logs[0]


# ── _handle_event: Samsara in the background, Motive inline ─────────────────────

async def test_handle_event_samsara_driver_notification_runs_in_background(monkeypatch):
    _patch_handle_event(monkeypatch, [1], [])
    release, finished, seen = asyncio.Event(), [], []

    async def slow_notify(**kwargs):
        seen.append(kwargs)
        await release.wait()          # stands in for the coaching-state wait
        finished.append(kwargs["event_id"])

    async def fake_poll(vehicle_id, timestamp_ms, api_key, on_first=None):
        return {"harshEventType": "Mobile Usage", "downloadInwardVideoUrl": "http://i.mp4",
                "location": {"address": "I-95"}}

    monkeypatch.setattr(wh, "notify_driver", slow_notify)
    monkeypatch.setattr(wh, "_fetch_samsara_harsh_event", fake_poll)
    monkeypatch.setattr(wh, "_download", _async_const(b"VID"))
    bot = _FakeMainBot()
    event = {"id": "uuid-abc", "type": "harsh_event", "_source": "samsara",
             "_samsara_vehicle_id": "v9", "_samsara_timestamp_ms": 1759676400000,
             "vehicle": {"number": "T1"}, "start_time": "2026-10-05T15:00:00Z"}

    await asyncio.wait_for(wh._handle_event(bot, event, "co", "SAMSARA_KEY"), 1)
    assert finished == []                         # returned while the wait is still open
    assert bot.sent == [1]                        # the company alert is not held back
    tasks = list(wh._background_tasks)
    assert len(tasks) == 1
    await asyncio.sleep(0)
    assert seen and seen[0]["samsara_api_key"] == "SAMSARA_KEY"
    assert seen[0]["event"]["_samsara_vehicle_id"] == "v9"
    release.set()
    await tasks[0]
    await asyncio.sleep(0)
    assert finished == [wh._event_id_to_bigint("uuid-abc")]
    assert wh._background_tasks == set()          # reference dropped once done


async def test_handle_event_motive_driver_notification_stays_inline(monkeypatch):
    _patch_handle_event(monkeypatch, [1], [])
    finished = []

    async def notify(**kwargs):
        await asyncio.sleep(0)
        finished.append(kwargs)

    monkeypatch.setattr(wh, "notify_driver", notify)
    await wh._handle_event(_FakeMainBot(), {"id": "905", "type": "cell_phone",
                                            "vehicle": {"number": "T1"},
                                            "start_time": "2026-10-05T15:00:00Z"}, "co", "KEY")
    assert len(finished) == 1 and finished[0]["samsara_api_key"] is None
    assert wh._background_tasks == set()


async def test_background_task_failure_is_logged(caplog):
    async def boom():
        raise RuntimeError("kaput")

    with caplog.at_level("ERROR", logger=wh.logger.name):
        task = wh._spawn_background(boom(), "test job")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
    assert wh._background_tasks == set()
    assert any("Background test job failed" in r.getMessage() for r in caplog.records)
