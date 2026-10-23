"""Tests for scripts/audit_driver_bot.py: per usable pti group, is the driver bot able
to post. No Telegram, no Postgres: the bot and the pti fetch are fakes. The report must
never print group titles (they carry driver names)."""
import types

import pytest
from aiogram.utils.exceptions import ChatNotFound, RetryAfter

import scripts.audit_driver_bot as audit
import utils.pti_groups as pg


class _Bot:
    id = 777

    def __init__(self, answers):
        self.answers, self.calls, self.closed = answers, [], False

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        answer = self.answers.get(chat_id, "member")
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return types.SimpleNamespace(status=answer)
        return answer

    async def get_session(self):
        bot = self

        class _S:
            async def close(self):
                bot.closed = True
        return _S()


_GROUPS = (
    pg.PtiGroup(-1, "1301 SECRET, NAME", "1301"),
    pg.PtiGroup(-2, "1302 SECRET, NAME", "1302"),
    pg.PtiGroup(-3, "1303 SECRET, NAME", "1303"),
    pg.PtiGroup(-4, "1304 SECRET, NAME", "1304"),
    pg.PtiGroup(-5, "1305 SECRET, NAME", "1305"),
    pg.PtiGroup(-6, "1306 SECRET, NAME", "1306"),
    pg.PtiGroup(-7, "1307 SECRET, NAME", "1307"),
)
_ANSWERS = {
    -2: "administrator",
    -3: types.SimpleNamespace(status="restricted", is_member=True, can_send_messages=False),
    -4: "left",
    -5: ChatNotFound("Bad Request: chat not found"),
    -6: RuntimeError("network down"),
    -7: [RetryAfter(0), "member"],
}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(audit, "PAUSE", 0)
    monkeypatch.setenv("DRIVER_ALERTS_BOT_TOKEN", "777:test")
    monkeypatch.setenv("PTI_DATABASE_URL_JRD", "postgresql://pti/jrd")
    monkeypatch.setattr(pg, "_pools", {})
    bot = _Bot(dict(_ANSWERS))
    monkeypatch.setattr(audit, "_make_bot", lambda token: bot)
    fetched = []

    async def fetch(company):
        fetched.append(company)
        return _GROUPS

    monkeypatch.setattr(audit.pti_groups, "fetch_usable_groups", fetch)
    monkeypatch.setattr(audit, "_sleep", _no_sleep)
    return types.SimpleNamespace(bot=bot, fetched=fetched)


async def _no_sleep(_s):
    return None


async def test_report_counts_and_lists_problem_units(setup, capsys):
    assert await audit.run("jrd") == 1
    out = capsys.readouterr().out
    assert setup.fetched == ["jrd"] and setup.bot.closed
    assert all(user == 777 for _chat, user in setup.bot.calls)
    assert "7 usable groups" in out
    counts = {line.rsplit(" ", 1)[0].strip(): int(line.rsplit(" ", 1)[1])
              for line in out.splitlines()[1:6]}
    assert counts == {"member": 2, "admin": 1, "restricted": 1, "not member": 2, "error": 1}
    problems = out.split("Cannot post (4):")[1]
    for unit in ("1303", "1304", "1305", "1306"):
        assert f"unit {unit}" in problems
    for unit in ("1301", "1302", "1307"):
        assert f"unit {unit}" not in problems
    assert "bot_cannot_post" in problems and "bot_not_in_group" in problems
    assert "SECRET" not in out                       # titles are never printed


async def test_all_good_is_exit_0(setup, capsys):
    setup.bot.answers = {}
    assert await audit.run("jrd") == 0
    assert "can post in every usable group" in capsys.readouterr().out


async def test_missing_configuration_is_exit_2(setup, monkeypatch, capsys):
    monkeypatch.delenv("DRIVER_ALERTS_BOT_TOKEN")
    assert await audit.run("jrd") == 2
    monkeypatch.setenv("DRIVER_ALERTS_BOT_TOKEN", "777:test")
    assert await audit.run("acme") == 2
    assert setup.fetched == [] and setup.bot.calls == []


async def test_database_error_is_exit_2(setup, monkeypatch):
    async def broken(company):
        raise OSError("refused")
    monkeypatch.setattr(audit.pti_groups, "fetch_usable_groups", broken)
    assert await audit.run("jrd") == 2
    assert setup.bot.calls == []
