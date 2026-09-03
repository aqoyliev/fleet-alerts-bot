"""The singleton alert_group row: seeded once from config.GROUP_CHAT_ID, then
self-healing across a Telegram basic-group → supergroup migration without the seed
re-inserting a stale duplicate for the old id.
"""
from data import config
from utils.db_api import groups


def _spy(monkeypatch):
    calls = []

    async def _execute(query, *args):
        calls.append((" ".join(query.split()), args))

    monkeypatch.setattr(groups.db, "execute", _execute)
    return calls


async def test_ensure_group_seeds_the_singleton_row_from_config(monkeypatch):
    calls = _spy(monkeypatch)
    monkeypatch.setattr(config, "GROUP_CHAT_ID", -100555)

    await groups.ensure_group()

    query, args = calls[0]
    assert "INSERT INTO alert_group (id, telegram_group_id) VALUES (1, $1)" in query
    assert "ON CONFLICT (id) DO NOTHING" in query
    assert args == (-100555,)


async def test_migrate_group_repoints_the_row_by_its_current_chat_id(monkeypatch):
    calls = _spy(monkeypatch)

    await groups.migrate_group(-100111, -1001000000111)

    query, args = calls[0]
    assert "UPDATE alert_group SET telegram_group_id = $1 WHERE telegram_group_id = $2" in query
    assert args == (-1001000000111, -100111)


async def test_get_alert_target_returns_none_when_muted(monkeypatch):
    async def _fetchrow(query, *args):
        assert "WHERE enabled" in query
        return None

    monkeypatch.setattr(groups.db, "fetchrow", _fetchrow)
    assert await groups.get_alert_target() is None


async def test_get_alert_target_returns_the_chat_id_when_enabled(monkeypatch):
    async def _fetchrow(query, *args):
        return {"telegram_group_id": -100777}

    monkeypatch.setattr(groups.db, "fetchrow", _fetchrow)
    assert await groups.get_alert_target() == -100777


async def test_is_the_group_compares_against_the_current_row_not_the_env(monkeypatch):
    """The whole point of the singleton row is that it can drift from GROUP_CHAT_ID after
    a migration — is_the_group has to follow the row, not the env var."""
    async def _fetchrow(query, *args):
        return {"telegram_group_id": -1009999}

    monkeypatch.setattr(groups.db, "fetchrow", _fetchrow)
    monkeypatch.setattr(config, "GROUP_CHAT_ID", -100111)

    assert await groups.is_the_group(-1009999) is True
    assert await groups.is_the_group(-100111) is False
