"""Contact Support — a two-way channel between anyone using the bot and its maintainers.

A driver's only relationship with this bot is that it posts alerts into their group.
When something is wrong with that — the wrong unit, alerts that stopped, a question
nobody in the group can answer — there was no way to tell anybody, and the DM said
"you don't have access" and nothing else. This is that way.

Three rules shape the code:

  • Whatever they send is what arrives. The message is FORWARDED, not retyped into a
    summary, so a photo of a dashboard light, a dashcam clip or a voice note reaches the
    maintainer as the thing it is. content_types=ANY is the whole point, not a detail.

  • The maintainer answers by replying, in Telegram, the way they would to a person. The
    reply is copied back to the user as the bot, so the maintainer's own account never
    becomes a contact a driver can write to directly — and so they never have to think
    about who a message belongs to. Which message belongs to whom is recorded when it is
    relayed (utils/db_api/support.py); it cannot be read off the reply, because a
    forwarded message names its author only when the author permits it.

  • A non-admin's private message is relayed even without pressing the button first.
    Nothing else in a DM is available to them, so a message typed into the bot can only
    have been meant for a person — and that keeps working after a restart, when the
    button's in-memory state does not.

Registered last among the private-chat handlers (see handlers/users/__init__.py) so its
catch-all sits behind every command and menu button rather than in front of them.
"""
import logging

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.exceptions import (
    BotBlocked, CantInitiateConversation, ChatNotFound, UserDeactivated,
)

from data import config
from keyboards.default.main_menu import (
    CANCEL_BUTTON, CONTACT_BUTTON, cancel_keyboard, contact_keyboard, main_menu_keyboard,
)
from loader import bot, dp
from utils.db_api.admins import is_admin
from utils.db_api.support import relay_target, remember_relay
from utils.tg_text import esc

logger = logging.getLogger(__name__)

# Telegram errors that mean this person will never receive the answer: they blocked the
# bot, deleted their account, or never opened a chat with it. Retrying changes nothing,
# so the maintainer is told instead of the failure being swallowed into the log.
_UNREACHABLE = (BotBlocked, CantInitiateConversation, ChatNotFound, UserDeactivated)

_PROMPT = (
    "✉️ <b>Contact support</b>\n\n"
    "Send your message and it goes straight to the team — text, a photo, a video, a "
    "voice note, a document, anything.\n\n"
    "You'll get their answer right here in this chat."
)
_SENT = "✅ Sent. The team will answer you in this chat."
_UNDELIVERED = "⚠️ Couldn't send that right now. Please try again in a few minutes."


class Support(StatesGroup):
    """Waiting for the one message that follows the button."""
    writing = State()


def _maintainer_ids() -> list[int]:
    """The accounts a support message reaches: this deployment's maintainers.

    The same ids config.ADMINS names and utils.db_api.admins.is_maintainer answers for —
    whoever set the deployment up, as opposed to the company admins they added through
    the panel. "Contact support" means the people who run the bot, and that is this list
    and not the customer's dispatchers.
    """
    return sorted(config.HIDDEN_ADMIN_IDS)


def _header(user: types.User) -> str:
    """Who wrote in, as a line the maintainer can act on.

    The tg://user link opens the person's profile even when they have no username, which
    is the case that matters: a forwarded message from an account with forward privacy on
    shows "Hidden account" and nothing else, and then this line is the only way to find
    out who is asking.
    """
    who = f'<a href="tg://user?id={user.id}">{esc(user.full_name)}</a>'
    handle = f" (@{esc(user.username)})" if user.username else ""
    return (
        f"✉️ <b>New message</b>\n"
        f"👤 {who}{handle}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"<i>Reply to this message to answer them.</i>"
    )


async def _remember(chat_id: int, message_id: int, user_id: int) -> None:
    """Link one delivered message to its sender, and survive not being able to.

    A database that is down costs the reply path, not the message: the maintainer still
    reads what was sent, and the worst case is having to answer by opening the profile
    the header links to.
    """
    try:
        await remember_relay(chat_id, message_id, user_id)
    except Exception as e:
        logger.error(f"Could not record support relay {chat_id}/{message_id}: {e}")


async def relay_to_support(message: types.Message) -> bool:
    """Put one user's message in front of every maintainer. True if any of them got it.

    The forward goes first and the header follows as a reply to it, so the two arrive as
    one quoted block in the maintainer's chat instead of drifting apart when several
    people write at once. Both message ids are recorded, because a reply to either half
    is a reply to the same person.

    Each maintainer is attempted on its own: one who blocked the bot must not cost the
    others the message.
    """
    user = message.from_user
    delivered = False
    for chat_id in _maintainer_ids():
        try:
            forwarded = await message.forward(chat_id)
            await _remember(chat_id, forwarded.message_id, user.id)
            # Delivered is decided by the forward alone. The header is attribution, and a
            # maintainer holding the message without it can still answer — the row above
            # is already written, so replying to the forward routes correctly.
            delivered = True
            header = await bot.send_message(
                chat_id, _header(user), reply_to_message_id=forwarded.message_id)
            await _remember(chat_id, header.message_id, user.id)
        except Exception as e:
            logger.error(f"Support message from {user.id} did not reach {chat_id}: {e}",
                         exc_info=True)
    if not delivered:
        logger.error(f"Support message from {user.id} reached nobody — "
                     f"maintainer ids configured: {_maintainer_ids() or 'none'}")
    return delivered


async def _keyboard_for(telegram_id: int) -> types.ReplyKeyboardMarkup:
    """Put back the keyboard this person had before the cancel button replaced it."""
    return main_menu_keyboard() if await is_admin(telegram_id) else contact_keyboard()


async def _take_message(message: types.Message) -> None:
    """Relay one message and tell its sender which way it went."""
    keyboard = await _keyboard_for(message.from_user.id)
    if await relay_to_support(message):
        await message.answer(_SENT, reply_markup=keyboard)
    else:
        await message.answer(_UNDELIVERED, reply_markup=keyboard)


# ── the maintainer's side: a reply goes back to whoever it answers ───────────────

async def _answers_a_relay(message: types.Message):
    """Filter: is this a reply to a message the bot relayed, and if so, from whom?

    Written as a filter rather than a check inside the handler so that a reply to
    anything else falls through to the handlers below instead of being swallowed here.
    The user id it finds is handed to the handler as a keyword argument.
    """
    reply = message.reply_to_message
    if reply is None:
        return False
    try:
        user_id = await relay_target(message.chat.id, reply.message_id)
    except Exception as e:
        logger.error(f"Could not look up the sender behind a reply: {e}")
        return False
    return {"support_user_id": user_id} if user_id else False


@dp.message_handler(_answers_a_relay, chat_type=types.ChatType.PRIVATE, state="*",
                    content_types=types.ContentTypes.ANY)
async def answer_support(message: types.Message, support_user_id: int):
    """Send the maintainer's reply on to the user it answers.

    Copied, not forwarded: the answer arrives from the bot, so the maintainer's personal
    account is never exposed as a contact — and copying carries any content type, which
    means they can answer a photo with a photo.

    state="*" because this outranks the maintainer's own Contact Support prompt: a reply
    to a driver is unambiguous, whatever the maintainer was in the middle of.
    """
    try:
        await bot.copy_message(support_user_id, message.chat.id, message.message_id)
    except _UNREACHABLE:
        await message.reply(
            "⚠️ Not delivered — this user has blocked the bot or never started it.")
        return
    except Exception as e:
        logger.error(f"Reply to {support_user_id} failed: {e}", exc_info=True)
        await message.reply("⚠️ Not delivered. Try again in a moment.")
        return
    await message.reply("✅ Delivered.")


# ── the user's side ─────────────────────────────────────────────────────────────

@dp.message_handler(text=CONTACT_BUTTON, chat_type=types.ChatType.PRIVATE, state="*")
async def btn_contact(message: types.Message, state: FSMContext):
    await state.finish()
    await Support.writing.set()
    await message.answer(_PROMPT, reply_markup=cancel_keyboard())


@dp.message_handler(text=CANCEL_BUTTON, chat_type=types.ChatType.PRIVATE,
                    state=Support.writing)
async def cancel_contact(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer("Cancelled — nothing was sent.",
                         reply_markup=await _keyboard_for(message.from_user.id))


@dp.message_handler(state=Support.writing, content_types=types.ContentTypes.ANY)
async def send_to_support(message: types.Message, state: FSMContext):
    """The message that follows the button, whatever kind of message it is.

    Deliberately not given a @rate_limit of its own. An album of three photos arrives as
    three separate messages a few milliseconds apart, and a per-handler limit would relay
    the first and answer "Too many requests!" to the other two — dropping most of the
    evidence somebody just tried to send. The dispatcher-wide throttle still applies.
    """
    await state.finish()
    await _take_message(message)


@dp.message_handler(chat_type=types.ChatType.PRIVATE, content_types=types.ContentTypes.ANY)
async def relay_stray_message(message: types.Message):
    """A non-admin writing into the DM without pressing the button first.

    Which is how people actually behave, and the only thing a DM can mean for somebody
    with no other screen in it. Handling it here also means the feature does not depend
    on FSM state that a restart clears — a driver mid-conversation is not left typing
    into a bot that has forgotten it asked.

    Admins fall through silently, exactly as before: they have menu buttons, and a
    mistyped one should not be sent to anybody. Commands fall through too, so an
    unrecognised /whatever is not relayed as if it were a question.
    """
    if (message.text or "").startswith("/"):
        return
    if await is_admin(message.from_user.id):
        return
    await _take_message(message)
