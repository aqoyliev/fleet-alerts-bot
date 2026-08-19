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
