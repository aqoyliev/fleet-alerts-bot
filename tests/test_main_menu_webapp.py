"""The panel's entry point is a reply-keyboard button carrying a web_app payload.

aiogram 2.15 predates Bot API 6.0 and has no WebAppInfo type, so the payload is passed as
a plain dict and survives only because TelegramObject serializes unknown kwargs verbatim.
That is an implementation detail of the library, not a documented contract — so it gets a
test. If a future aiogram bump changes it, this fails loudly instead of the button
quietly vanishing from every dispatcher's keyboard.
"""

from data import config
from keyboards.default.main_menu import main_menu_keyboard


def _buttons(markup) -> list[dict]:
    return [b for row in markup.to_python()["keyboard"] for b in row]


def test_the_panel_button_carries_a_web_app_url(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    buttons = _buttons(main_menu_keyboard())

    panel = [b for b in buttons if "web_app" in b]
    assert len(panel) == 1, "expected exactly one web_app button"
    assert panel[0]["web_app"] == {"url": "https://fleet.example/panel/"}
    assert "Admin Panel" in panel[0]["text"]


def test_the_existing_buttons_are_untouched(monkeypatch):
    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    labels = [b["text"] for b in _buttons(main_menu_keyboard())]
    for expected in ("📊 Violations Report", "👥 Admins", "⚙️ Settings"):
        assert expected in labels


def test_a_deployment_without_a_url_keeps_the_original_keyboard(monkeypatch):
    """Blank WEBAPP_URL is a supported state — a deployment that hasn't been given a
    public domain must keep exactly the three buttons it has today, not gain a button
    that opens nothing."""
    monkeypatch.setattr(config, "WEBAPP_URL", "")
    buttons = _buttons(main_menu_keyboard())

    assert [b["text"] for b in buttons] == [
        "📊 Violations Report", "👥 Admins", "⚙️ Settings",
    ]
    assert not any("web_app" in b for b in buttons)


def test_start_offers_an_inline_panel_button_too(monkeypatch):
    """Two launch contexts, because they are not equally supported: some clients pass a
    Mini App its signed credential from an inline button but not from a keyboard button.
    The dispatcher should not have to know which client they are on."""
    from handlers.users.start import _panel_button

    monkeypatch.setattr(config, "WEBAPP_URL", "https://fleet.example")
    markup = _panel_button().to_python()
    button = markup["inline_keyboard"][0][0]

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
