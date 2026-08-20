"""How an admin actually reaches the Mini App.

There are two doors and neither is the reply keyboard: the ☰ Menu button, pointed at the
panel per chat when someone passes the admin check, and an inline button on /start.

Both carry their web_app payload as a plain dict, because aiogram 2.15 predates Bot API
6.0 and has no WebAppInfo type — it survives only because TelegramObject serializes
unknown kwargs verbatim. That is an implementation detail of the library rather than a
documented contract, so it is pinned here: a future aiogram bump fails these loudly
instead of the button quietly vanishing.
"""

from data import config
from keyboards.default.main_menu import main_menu_keyboard


def _buttons(markup) -> list[dict]:
    return [b for row in markup.to_python()["keyboard"] for b in row]


# ── the reply keyboard stays out of it ──────────────────────────────────────────

def test_the_reply_keyboard_carries_no_panel_button(monkeypatch):
    """Deliberately absent. The keyboard is a persistent strip of three commands and the
    panel is not one of them — it belongs on the Menu button, where someone looks for a
    bot's main surface. Asserted with a URL configured, since that is the state in which
    a button would otherwise appear."""
    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    buttons = _buttons(main_menu_keyboard())

    assert not any("web_app" in b for b in buttons)
    assert [b["text"] for b in buttons] == [
        "📊 Violations Report", "👥 Admins", "⚙️ Settings",
    ]


def test_the_keyboard_is_the_same_without_a_url(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_URL", "")
    assert [b["text"] for b in _buttons(main_menu_keyboard())] == [
        "📊 Violations Report", "👥 Admins", "⚙️ Settings",
    ]


# ── the inline button on /start ─────────────────────────────────────────────────

def test_start_offers_an_inline_panel_button(monkeypatch):
    from handlers.users.start import _panel_button

    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    button = _panel_button().to_python()["inline_keyboard"][0][0]

    assert button["web_app"] == {"url": "https://fleet.example/panel/"}
    assert "Admin Panel" in button["text"]


def test_no_inline_button_without_a_url(monkeypatch):
    from handlers.users.start import _panel_button

    monkeypatch.setattr(config, "WEBAPP_URL", "")
    assert _panel_button() is None


# ── the ☰ Menu button ───────────────────────────────────────────────────────────

async def test_the_menu_button_is_set_per_chat_not_globally(monkeypatch):
    """A default menu button applies to every private chat the bot has, so scoping it to
    one chat is what keeps "Admin Panel" from appearing for every driver who DMs the bot.
    The chat_id in the call is the whole guarantee."""
    import json as _json
    from handlers.users import start

    sent = []

    async def _request(method, data=None):
        sent.append((method, data))
        return True

    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    monkeypatch.setattr(start.bot, "request", _request)

    await start._set_menu_button(12345, to_panel=True)

    method, data = sent[0]
    assert method == "setChatMenuButton"
    assert data["chat_id"] == 12345
    button = _json.loads(data["menu_button"])
    assert button["type"] == "web_app"
    assert button["web_app"]["url"] == "https://fleet.example/panel/"


async def test_the_menu_button_is_put_back_for_a_non_admin(monkeypatch):
    """A removed admin's Menu button must stop advertising a door they can't open."""
    import json as _json
    from handlers.users import start

    sent = []

    async def _request(method, data=None):
        sent.append((method, data))
        return True

    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    monkeypatch.setattr(start.bot, "request", _request)

    await start._set_menu_button(12345, to_panel=False)

    assert _json.loads(sent[0][1]["menu_button"]) == {"type": "commands"}


async def test_a_failed_menu_button_does_not_break_start(monkeypatch):
    """Telegram refusing the call is a smaller problem than /start raising in a
    dispatcher's face."""
    from handlers.users import start

    async def _boom(_method, _data=None):
        raise RuntimeError("Bad Request: menu button not supported")

    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    monkeypatch.setattr(start.bot, "request", _boom)

    await start._set_menu_button(12345, to_panel=True)   # must not raise
