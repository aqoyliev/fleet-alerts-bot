from aiogram import types

# The labels are the routing. aiogram matches a reply-keyboard press by the exact text
# it sends, so the handlers import these constants rather than repeating the strings —
# an emoji edited in one place and not the other is a button that silently stops working.
CONTACT_BUTTON = "✉️ Contact Support"
CANCEL_BUTTON = "⬅️ Cancel"


def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    # Admin management lives in the Mini App now, not here — see its Admins screen.
    #
    # No admin-panel button here either. The Mini App is reached from the ☰ Menu button,
    # which handlers/users/start.py points at it per chat, and from the inline button on
    # /start.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📊 Violations Report"))
    kb.add(types.KeyboardButton("⚙️ Settings"))
    kb.add(types.KeyboardButton(CONTACT_BUTTON))
    return kb


def contact_keyboard() -> types.ReplyKeyboardMarkup:
    """The whole keyboard for somebody who is not an admin.

    Every other screen in this bot is admin-only, so a driver who opens the DM used to be
    told they have no access and left facing a blank chat. Writing to the people who run
    the bot is the one thing they legitimately want to do from here, so it is the one
    thing they are offered.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(CONTACT_BUTTON))
    return kb


def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    """Shown while the bot is waiting for a message to relay.

    It replaces the normal keyboard for that moment on purpose: anything typed next is
    sent to a person, and a menu button sitting there would be read as a way out that
    actually relays the words "Settings" to the maintainer.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton(CANCEL_BUTTON))
    return kb
