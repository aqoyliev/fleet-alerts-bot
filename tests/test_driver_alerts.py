"""Tests for the driver notification of cell_phone events (utils/driver_alerts.py),
as run by the driver-alerts service. No Postgres: the DB-facing functions are replaced with an
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


_GROUP_TITLE = "T-120 SMITH, JOHN / LEE, ANN"


class _FakeDriverBot:
    def __init__(self, fail_with=None):
        self.calls = []          # (kind, chat_id, caption_or_text)
        self.fail_with = list(fail_with or [])
        self.titles = {}         # chat_id -> title; others get _GROUP_TITLE
        self.chat_error = None   # raised by get_chat when set
        self.get_chat_calls = []

    async def get_chat(self, chat_id):
        self.get_chat_calls.append(chat_id)
        if self.chat_error is not None:
            raise self.chat_error
        return types.SimpleNamespace(title=self.titles.get(chat_id, _GROUP_TITLE))

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

        async def record_sent(event_id, sent_group, sent_email, name_match=None):
            self.notifications[event_id].update(sent_group=sent_group, sent_email=sent_email,
                                                name_match=name_match)

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


def _with_driver(monkeypatch, email=None, name="John Smith", ref=None, source="vitality"):
    async def resolve(company_slug, event, vehicle, occurred_at):
        return dl.DriverInfo(name=name, ref=ref, email=email, source=source)
    monkeypatch.setattr(dl, "resolve_driver", resolve)


@pytest.fixture
def env(monkeypatch):
    store = _FakeStore()
    store.install(monkeypatch)
    bot = _FakeDriverBot()
    monkeypatch.setattr(da, "_get_driver_bot", lambda: bot)
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_DRY_RUN", False, raising=False)
    monkeypatch.setattr(da, "_title_cache", {})
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
        dl.DriverInfo(name="  john   SMITH ", ref="vitality:def", source="vitality"),
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
        dl.DriverInfo(name="Ann Lee", source="vitality"),
    )
    await _notify(env.store, 72, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    await _notify(env.store, 73, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.store.notifications[73]["ordinal"] == 2


async def test_different_names_on_the_same_truck_are_counted_separately(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(
        monkeypatch,
        dl.DriverInfo(name="John Smith", source="vitality"),
        dl.DriverInfo(name="Ann Lee", source="vitality"),
        dl.DriverInfo(name="john smith", source="vitality"),
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


# ── driver-name verification ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name,title", [
    ("John Smith", "T-120 SMITH, JOHN / LEE, ANN"),
    ("John A Smith", "4063 Smith, John"),                       # middle initial dropped
    ("John Allen Smith", "4063 SMITH, JOHN ALLEN"),
    ("Ann Lee", "T-120 SMITH, JOHN / LEE, ANN"),                # second of a team
    ("SMITH, JOHN", "120 john smith."),                         # any order, case, trailing dot
    ("John Smith", "T-120 SMITH, JOHN (Owner Operator)"),       # "(...)" suffix
    ("José Núñez", "77 NÚÑEZ, JOSÉ"),
    ("Madonna", "12 MADONNA, LOUISE"),                          # one-token name needs one hit
])
def test_driver_name_in_title_matches(name, title):
    assert da.driver_name_in_title(name, title) is True


@pytest.mark.parametrize("name,title", [
    ("John Smith", "T-120 JONES, MARK / LEE, ANN"),
    ("John Smith", "T-120 SMITH, ROBERT"),                      # only the surname: 1 of 2
    ("John Smith", "T-120 JOHN DEERE"),                         # only the first name
    ("John Smith", ""),
    ("John Smith", None),
    ("", "T-120 SMITH, JOHN"),
    (None, "T-120 SMITH, JOHN"),
    ("J S", "T-120 SMITH, JOHN"),                               # no token of 2+ letters
    ("Madonna", "12 SMITH, JOHN"),
])
def test_driver_name_in_title_mismatches(name, title):
    assert da.driver_name_in_title(name, title) is False


async def test_unidentified_driver_is_not_notified(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _drivers_in_turn(monkeypatch, dl.DriverInfo(), dl.DriverInfo(name="Ann Lee", source="vitality"))
    await _notify(env.store, 200, _TS)
    n = env.store.notifications[200]
    assert env.bot.calls == [] and env.email.sent == [] and env.bot.get_chat_calls == []
    assert n["skip_reason"] == da.SKIP_DRIVER_UNKNOWN == "driver_unknown"
    assert "ordinal" not in n and "sent_group" not in n
    # A Vitality driver with a blank name is as good as unknown.
    _drivers_in_turn(monkeypatch, dl.DriverInfo(name="  ", ref="vitality:1", source="vitality"))
    await _notify(env.store, 201, _TS)
    assert env.store.notifications[201]["skip_reason"] == "driver_unknown"
    assert env.bot.calls == [] and env.email.sent == []


async def test_event_sourced_driver_is_not_notified_but_is_kept_on_the_violation(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _with_driver(monkeypatch, email="driver@example.com", name="John Smith", source="event")
    await _notify(env.store, 202, _TS)
    assert env.bot.calls == [] and env.email.sent == []
    assert env.store.notifications[202]["skip_reason"] == da.SKIP_DRIVER_UNKNOWN
    assert env.store.violation(202)["driver_name"] == "John Smith"


async def test_vitality_match_sends_and_records_name_match_true(env):
    env.store.groups["T-120"] = -100
    await _notify(env.store, 203, _TS)
    assert len(env.bot.calls) == 1 and env.bot.get_chat_calls == [-100]
    n = env.store.notifications[203]
    assert n["name_match"] is True and n["sent_group"] is True
    assert n.get("skip_reason") is None


async def test_name_mismatch_still_sends_and_records_name_match_false(env, caplog):
    env.store.groups["T-120"] = -100
    env.bot.titles[-100] = "T-120 JONES, MARK"
    with caplog.at_level("INFO", logger=da.logger.name):
        await _notify(env.store, 204, _TS)
    assert len(env.bot.calls) == 1 and env.bot.calls[0][1] == -100
    n = env.store.notifications[204]
    assert n["name_match"] is False and n["sent_group"] is True
    info = [r.getMessage() for r in caplog.records
            if r.levelname == "INFO" and "-100" in r.getMessage() and "T-120" in r.getMessage()
            and "not in the title" in r.getMessage()]
    assert len(info) == 1 and "Smith" not in info[0] and "JONES" not in info[0]


async def test_unreadable_title_sends_and_records_name_match_none(env, caplog):
    env.store.groups["T-120"] = -100
    env.bot.chat_error = RuntimeError("Bad Request: chat not found")
    with caplog.at_level("WARNING", logger=da.logger.name):
        await _notify(env.store, 205, _TS)
    assert len(env.bot.calls) == 1
    n = env.store.notifications[205]
    assert n["name_match"] is None and n["sent_group"] is True
    assert any(r.levelname == "WARNING" and "-100" in r.getMessage() for r in caplog.records)

    env.bot.chat_error = None
    env.bot.titles[-100] = ""        # a chat without a title
    await _notify(env.store, 206, _TS)
    assert env.store.notifications[206]["name_match"] is None and len(env.bot.calls) == 2


async def test_no_group_means_no_title_lookup_and_no_name_match(env):
    await _notify(env.store, 207, _TS)
    assert env.bot.get_chat_calls == []
    assert env.store.notifications[207]["name_match"] is None


async def test_group_title_is_cached_for_an_hour(env, monkeypatch):
    env.store.groups["T-120"] = -100
    now = [1000.0]
    monkeypatch.setattr(da.time, "monotonic", lambda: now[0])
    await _notify(env.store, 208, datetime(2026, 10, 5, 13, tzinfo=timezone.utc))
    now[0] += 3599
    await _notify(env.store, 209, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    assert env.bot.get_chat_calls == [-100]
    assert env.store.notifications[209]["name_match"] is True
    now[0] += 2                       # past the hour: read again
    env.bot.titles[-100] = "T-120 JONES, MARK"
    await _notify(env.store, 210, datetime(2026, 10, 5, 15, tzinfo=timezone.utc))
    assert env.bot.get_chat_calls == [-100, -100]
    assert env.store.notifications[210]["name_match"] is False


async def test_failed_title_read_is_not_cached(env):
    env.store.groups["T-120"] = -100
    env.bot.chat_error = RuntimeError("timeout")
    await _notify(env.store, 211, datetime(2026, 10, 5, 13, tzinfo=timezone.utc))
    env.bot.chat_error = None
    await _notify(env.store, 212, datetime(2026, 10, 5, 14, tzinfo=timezone.utc))
    assert len(env.bot.get_chat_calls) == 2
    assert env.store.notifications[212]["name_match"] is True


async def test_migrated_group_title_is_treated_as_unknown(env):
    env.store.groups["T-120"] = -100
    migrate = MigrateToChat("Group migrated to supergroup")
    migrate.migrate_to_chat_id = -1009
    env.bot.chat_error = migrate
    await _notify(env.store, 213, _TS)
    assert len(env.bot.calls) == 1
    assert env.store.notifications[213]["name_match"] is None


async def test_dry_run_logs_the_driver_decision_and_sends_nothing(env, monkeypatch, caplog):
    monkeypatch.setattr(da.config, "DRIVER_ALERTS_DRY_RUN", True, raising=False)
    env.store.groups["T-120"] = -100
    _drivers_in_turn(monkeypatch, dl.DriverInfo(name="Bob Roe", source="event"),
                     dl.DriverInfo(name="John Smith", source="vitality"))
    with caplog.at_level("INFO", logger=da.logger.name):
        await _notify(env.store, 214, _TS)
        await _notify(env.store, 215, _TS)
    logs = [r.getMessage() for r in caplog.records if "DRY RUN" in r.getMessage()]
    assert len(logs) == 2
    assert "skipped" in logs[0] and "driver_unknown" in logs[0]
    assert "would send" in logs[1] and "name_match=True" in logs[1]
    assert env.bot.calls == [] and env.email.sent == []
    assert env.store.notifications[214]["skip_reason"] == "driver_unknown"


async def test_samsara_rejection_wins_over_driver_unknown(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _with_driver(monkeypatch, name="John Smith", source="event")
    _fake_wait(monkeypatch, _lookup_result("dismissed"))
    await _notify_samsara(env.store, 216, _TS)
    n = env.store.notifications[216]
    assert n["skip_reason"] == da.SKIP_NOT_ALLOWED and n["coaching_state"] == "dismissed"
    assert env.bot.calls == [] and env.email.sent == []


async def test_samsara_allowed_with_unknown_driver_is_driver_unknown(env, monkeypatch):
    env.store.groups["T-120"] = -100
    _with_driver(monkeypatch, name="", source="unknown")
    _fake_wait(monkeypatch, _lookup_result("needsReview"))
    await _notify_samsara(env.store, 217, _TS)
    n = env.store.notifications[217]
    assert n["skip_reason"] == da.SKIP_DRIVER_UNKNOWN and n["coaching_state"] == "needsReview"
    assert env.bot.calls == [] and env.email.sent == []


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


# ── _handle_event ────────────────────────────────────────────────────────────────
# _handle_event no longer calls notify_driver: it forwards cell_phone events to the
# driver-alerts service. See tests/test_driver_forward.py and tests/test_driver_service.py.


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
