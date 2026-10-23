"""Fakes for the userbot tests (tests/test_bot_join.py, tests/test_driver_alerts.py):
a Telethon client over an in-memory "world", the driver_bot_join_requests table, and a
shared fake clock. Nothing here touches the network."""
import asyncio
import types

from telethon import types as tl

from utils import bot_join, bot_membership, userbot

SESSION = "SECRET-SESSION-STRING-never-logged"
ADMIN_ID = 4242
BOT_USERNAME = "driver_alerts_bot"
SUPERGROUP = -1001234567890
BASIC_GROUP = -4567


def channel(marked_id=SUPERGROUP, *, members_may_invite=False):
    return tl.Channel(id=-marked_id - 10**12, title="T-120 SMITH, JOHN", photo=tl.ChatPhotoEmpty(),
                      date=None, megagroup=True, access_hash=1,
                      default_banned_rights=tl.ChatBannedRights(
                          until_date=None, invite_users=not members_may_invite))


def basic_chat(marked_id=BASIC_GROUP, *, migrated=False, deactivated=False):
    return tl.Chat(id=-marked_id, title="T-120 SMITH, JOHN", photo=tl.ChatPhotoEmpty(),
                   participants_count=3, date=None, version=1, deactivated=deactivated or None,
                   migrated_to=tl.InputChannel(1, 2) if migrated else None,
                   default_banned_rights=tl.ChatBannedRights(until_date=None, invite_users=True))


def perms(*, creator=False, admin=False, invite_users=False):
    return types.SimpleNamespace(is_creator=creator, is_admin=admin or creator,
                                 invite_users=invite_users)


class World:
    """What the userbot account sees. Clients made by new_client share it."""

    def __init__(self):
        self.dialogs = {}        # marked id -> entity
        self.perms = {}          # marked id -> perms(...) or Exception
        self.requests = []       # Telethon requests sent through client(...)
        self.sent = []           # (peer, text)
        self.fail = {}           # "call" / "send" / "get_me" / "connect" -> [Exception, ...]
        self.on_add = None       # called with the request after a successful add
        self.authorized = True
        self.clients = []
        self.connects = 0
        self.disconnects = 0
        self.dialog_reads = 0
        self.permission_reads = 0
        self.get_me_calls = 0
        self.active = 0
        self.max_active = 0

    def new_client(self):
        c = FakeClient(self)
        self.clients.append(c)
        return c

    def _maybe_fail(self, what):
        queue = self.fail.get(what)
        if queue:
            raise queue.pop(0)


class FakeClient:
    def __init__(self, world: World):
        self.w = world
        self.connected = False

    async def connect(self):
        self.w._maybe_fail("connect")
        self.w.connects += 1
        self.connected = True

    async def is_user_authorized(self):
        return self.w.authorized

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.w.disconnects += 1
        self.connected = False

    async def _busy(self):
        self.w.active += 1
        self.w.max_active = max(self.w.max_active, self.w.active)
        await asyncio.sleep(0)
        self.w.active -= 1

    async def get_dialogs(self, limit=None):
        await self._busy()
        self.w.dialog_reads += 1
        return [types.SimpleNamespace(id=gid, entity=ent, is_group=True)
                for gid, ent in self.w.dialogs.items()]

    async def get_permissions(self, entity, user):
        await self._busy()
        self.w.permission_reads += 1
        for gid, ent in self.w.dialogs.items():
            if ent is entity:
                answer = self.w.perms.get(gid, perms())
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError("permissions of an unknown entity")

    async def get_input_entity(self, peer):
        return f"input:{peer}"

    async def __call__(self, request):
        await self._busy()
        self.w._maybe_fail("call")
        self.w.requests.append(request)
        if self.w.on_add is not None:
            self.w.on_add(request)

    async def send_message(self, peer, text):
        await self._busy()
        self.w._maybe_fail("send")
        self.w.sent.append((peer, text))

    async def get_me(self):
        self.w.get_me_calls += 1
        self.w._maybe_fail("get_me")
        return types.SimpleNamespace(id=ADMIN_ID)


class JoinStore:
    """driver_bot_join_requests in memory, aged by the fake clock."""

    def __init__(self, clock):
        self.clock = clock
        self.rows = []
        self.fail_reads = None

    def install(self, monkeypatch):
        async def recent(company_slug, group_id, hours=24):
            if self.fail_reads is not None:
                raise self.fail_reads
            return {r["action"] for r in self.rows
                    if r["company_slug"] == company_slug and r["group_id"] == group_id
                    and self.clock[0] - r["at"] < hours * 3600}

        async def record(company_slug, group_id, unit_number, action, outcome, detail=None):
            self.rows.append({"id": len(self.rows) + 1, "company_slug": company_slug,
                              "group_id": group_id, "unit_number": unit_number, "action": action,
                              "outcome": outcome, "detail": detail, "at": self.clock[0]})
            return len(self.rows)

        async def set_outcome(request_id, outcome, detail=None):
            row = self.rows[request_id - 1]
            row["outcome"] = outcome
            if detail is not None:
                row["detail"] = detail

        monkeypatch.setattr(bot_join, "recent_join_actions", recent)
        monkeypatch.setattr(bot_join, "record_join_request", record)
        monkeypatch.setattr(bot_join, "set_join_request_outcome", set_outcome)

    def actions(self):
        return [(r["action"], r["outcome"]) for r in self.rows]


def install(monkeypatch, *, company_name="JRD"):
    """Userbot on, configured, every Telegram/DB/time dependency faked. Returns a
    namespace with world, store, clock, sleeps."""
    userbot._reset_state()
    bot_join._reset_state()
    clock = [1000.0]
    sleeps = []

    def now():
        return clock[0]

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        await asyncio.sleep(0)

    for mod in (userbot, bot_join):
        monkeypatch.setattr(mod, "_now", now)
        monkeypatch.setattr(mod, "_sleep", sleep)
    monkeypatch.setattr(userbot, "_start_idle_watch", lambda: None)
    world = World()
    monkeypatch.setattr(userbot, "_make_client", world.new_client)
    cfg = userbot.config
    monkeypatch.setattr(cfg, "USERBOT_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "USERBOT_SESSION", SESSION, raising=False)
    monkeypatch.setattr(cfg, "USERBOT_API_ID", "123", raising=False)
    monkeypatch.setattr(cfg, "USERBOT_API_HASH", "hash", raising=False)
    monkeypatch.setattr(cfg, "USERBOT_USER_ID", str(ADMIN_ID), raising=False)
    monkeypatch.delenv("HOS_USERNAMES_JRD", raising=False)
    monkeypatch.delenv("HOS_USERNAMES_CO", raising=False)
    monkeypatch.setattr(bot_membership, "_cache", {})

    async def name(slug):
        return company_name
    monkeypatch.setattr(bot_join, "get_company_name", name)
    store = JoinStore(clock)
    store.install(monkeypatch)
    return types.SimpleNamespace(world=world, store=store, clock=clock, sleeps=sleeps)
