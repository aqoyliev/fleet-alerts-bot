"""Contact Support, in both directions.

The feature is a message crossing between two people who cannot see each other: a driver
writes into the bot's DM and it lands in the maintainer's, and the maintainer's reply
comes back the other way. Everything worth testing is about that crossing not losing
anything on the way.

  • the message arrives as what it is, whatever it is — a photo stays a photo,
  • it says who sent it, in a form that still identifies them when Telegram's forward
    header says "Hidden account",
  • the reply reaches that person and nobody else, and
  • a reply to an ordinary message in an admin's DM is left completely alone, because
    this handler sits in front of every other private-chat handler there is.
"""

import pytest

from data import config
import handlers.users.support as support


class _User:
    def __init__(self, user_id=555, full_name="Ann Driver", username="anndriver"):
        self.id = user_id
        self.full_name = full_name
        self.username = username


class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id


class _Sent:
    """One message the bot produced, as the test cares about it."""
    def __init__(self, message_id):
        self.message_id = message_id


class _Message:
    """A private message, with the two calls the relay makes on it."""

    def __init__(self, user=None, chat_id=None, message_id=900, text="the brakes squeal",
                 reply_to=None):
        self.from_user = user or _User()
        self.chat = _Chat(chat_id if chat_id is not None else self.from_user.id)
        self.message_id = message_id
        self.text = text
        self.reply_to_message = reply_to
        self.forwarded_to: list[int] = []
        self.answers: list[tuple[str, object]] = []
        self.replies: list[str] = []
        self._next_forward_id = 1000

    async def forward(self, chat_id):
        self.forwarded_to.append(chat_id)
        self._next_forward_id += 1
        return _Sent(self._next_forward_id)

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class _Bot:
    def __init__(self):
        self.messages: list[dict] = []
        self.copies: list[tuple[int, int, int]] = []
        self._next_id = 2000

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kwargs):
        self._next_id += 1
        self.messages.append({"chat_id": chat_id, "text": text,
                              "reply_to": reply_to_message_id, "id": self._next_id})
        return _Sent(self._next_id)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self.copies.append((chat_id, from_chat_id, message_id))
        return _Sent(1)


@pytest.fixture
def relayed(monkeypatch):
    """One maintainer, a fake bot, and the relay table kept in a dict."""
    bot = _Bot()
    remembered: dict[tuple[int, int], int] = {}

    async def _remember(chat_id, msg_id, user_id):
        remembered[(chat_id, msg_id)] = user_id

    async def _target(chat_id, msg_id):
        return remembered.get((chat_id, msg_id))

    async def _is_admin(telegram_id):
        return telegram_id == 111

    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {111})
    monkeypatch.setattr(support, "bot", bot)
    monkeypatch.setattr(support, "remember_relay", _remember)
    monkeypatch.setattr(support, "relay_target", _target)
    monkeypatch.setattr(support, "is_admin", _is_admin)
    bot.remembered = remembered
    return bot


# ── a user writes in ────────────────────────────────────────────────────────────

async def test_the_message_is_forwarded_to_the_maintainer(relayed):
    message = _Message()

    assert await support.relay_to_support(message) is True
    assert message.forwarded_to == [111]


async def test_it_is_forwarded_rather_than_retyped(relayed):
    """A forward is what carries a photo, a video or a voice note through unchanged —
    the bot never looks at the content type, so there is nothing to keep in step."""
    message = _Message(text=None)

    await support.relay_to_support(message)

    assert message.forwarded_to == [111]
    # The only thing the bot composes itself is the header, not the message.
    assert len(relayed.messages) == 1


async def test_the_header_names_the_sender_and_links_to_their_profile(relayed):
    await support.relay_to_support(_Message())

    header = relayed.messages[0]["text"]
    assert 'tg://user?id=555' in header
    assert "Ann Driver" in header
    assert "@anndriver" in header
    assert "555" in header


async def test_the_header_survives_a_name_that_looks_like_markup(relayed):
    """The bot sends with parse_mode=HTML, and a name is written by someone else. An
    unescaped one is a header Telegram refuses — and the whole message with it."""
    await support.relay_to_support(_Message(user=_User(full_name="Ann <b>& Co</b>")))

    header = relayed.messages[0]["text"]
    assert "Ann &lt;b&gt;&amp; Co&lt;/b&gt;" in header


async def test_the_header_is_attached_to_the_forwarded_message(relayed):
    """Quoted onto the forward so the two stay together when several people write at
    once, instead of interleaving into an unreadable column."""
    message = _Message()
    await support.relay_to_support(message)

    assert relayed.messages[0]["reply_to"] == 1001


async def test_a_sender_with_no_username_still_identifies(relayed):
    await support.relay_to_support(_Message(user=_User(username=None)))

    header = relayed.messages[0]["text"]
    assert "@" not in header
    assert "tg://user?id=555" in header


async def test_both_halves_of_the_relay_point_back_at_the_sender(relayed):
    """Replying to the forward and replying to the header are the same gesture to a
    human, so they have to be the same gesture to the bot."""
    await support.relay_to_support(_Message())

    forward_id, header_id = 1001, relayed.messages[0]["id"]
    assert relayed.remembered[(111, forward_id)] == 555
    assert relayed.remembered[(111, header_id)] == 555


async def test_every_maintainer_gets_it(relayed, monkeypatch):
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {111, 222})
    message = _Message()

    await support.relay_to_support(message)

    assert sorted(message.forwarded_to) == [111, 222]


async def test_one_unreachable_maintainer_does_not_cost_the_others(relayed, monkeypatch):
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {111, 222})
    message = _Message()
    original = message.forward

    async def _forward(chat_id):
        if chat_id == 111:
            raise RuntimeError("bot was blocked by the user")
        return await original(chat_id)

    message.forward = _forward

    assert await support.relay_to_support(message) is True
    assert message.forwarded_to == [222]


async def test_a_message_that_reached_nobody_says_so(relayed, monkeypatch):
    """The sender is told their message went through only when it did — a cheerful
    confirmation over a delivery that failed is the worst outcome of the three."""
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", set())
    message = _Message()

    assert await support.relay_to_support(message) is False

    await support._take_message(message)
    assert "Couldn't send" in message.answers[0][0]


async def test_a_delivered_message_is_confirmed(relayed):
    message = _Message()
    await support._take_message(message)

    assert "Sent" in message.answers[0][0]


async def test_a_header_that_fails_does_not_make_it_an_undelivered_message(relayed, monkeypatch):
    """The forward is the message; the header is who sent it. Losing the second one
    leaves the maintainer holding something they can still read and still reply to, so
    telling the sender it never arrived would be wrong."""
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("Bad Request: reply message not found")

    monkeypatch.setattr(relayed, "send_message", _boom)
    message = _Message()

    assert await support.relay_to_support(message) is True
    assert relayed.remembered[(111, 1001)] == 555


async def test_a_database_that_cannot_record_the_relay_still_delivers(relayed, monkeypatch):
    """Losing the reply path is bad; losing the message is worse."""
    async def _boom(*_args):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(support, "remember_relay", _boom)
    message = _Message()

    assert await support.relay_to_support(message) is True
    assert message.forwarded_to == [111]


# ── the maintainer answers ──────────────────────────────────────────────────────

async def test_a_reply_reaches_the_user_it_answers(relayed):
    await support.relay_to_support(_Message())
    reply = _Message(user=_User(111, "Boss"), chat_id=111, message_id=77,
                     reply_to=_Sent(1001), text="on it")

    target = await support._answers_a_relay(reply)
    assert target == {"support_user_id": 555}

    await support.answer_support(reply, support_user_id=555)
    assert relayed.copies == [(555, 111, 77)]


async def test_replying_to_the_header_works_too(relayed):
    await support.relay_to_support(_Message())
    header_id = relayed.messages[0]["id"]
    reply = _Message(user=_User(111, "Boss"), chat_id=111, reply_to=_Sent(header_id))

    assert await support._answers_a_relay(reply) == {"support_user_id": 555}


async def test_the_answer_is_copied_so_the_maintainer_stays_anonymous(relayed):
    """copy_message, not forward: the driver sees a message from the bot, and never gets
    the maintainer's personal account as a contact they can write to directly."""
    await support.relay_to_support(_Message())
    reply = _Message(user=_User(111, "Boss"), chat_id=111, message_id=77,
                     reply_to=_Sent(1001))

    await support.answer_support(reply, support_user_id=555)

    assert relayed.copies == [(555, 111, 77)]
    assert reply.forwarded_to == []


async def test_an_ordinary_reply_in_an_admin_dm_is_left_alone(relayed):
    """This handler is checked before every other private-chat handler, so a reply to
    anything the bot did not relay has to fall straight through it."""
    reply = _Message(user=_User(111, "Boss"), chat_id=111, reply_to=_Sent(4242))

    assert await support._answers_a_relay(reply) is False


async def test_a_message_that_is_not_a_reply_is_left_alone(relayed):
    assert await support._answers_a_relay(_Message()) is False


async def test_a_lookup_failure_does_not_swallow_the_message(relayed, monkeypatch):
    """A database blip must not turn every reply in an admin's DM into a black hole."""
    async def _boom(*_args):
        raise RuntimeError("pool is gone")

    monkeypatch.setattr(support, "relay_target", _boom)
    reply = _Message(user=_User(111, "Boss"), chat_id=111, reply_to=_Sent(1001))

    assert await support._answers_a_relay(reply) is False


async def test_a_blocked_user_is_reported_to_the_maintainer(relayed, monkeypatch):
    """Otherwise the answer disappears and the maintainer believes it was delivered."""
    from aiogram.utils.exceptions import BotBlocked

    async def _blocked(*_args, **_kwargs):
        raise BotBlocked("Forbidden: bot was blocked by the user")

    monkeypatch.setattr(relayed, "copy_message", _blocked)
    reply = _Message(user=_User(111, "Boss"), chat_id=111, reply_to=_Sent(1001))

    await support.answer_support(reply, support_user_id=555)

    assert "Not delivered" in reply.replies[0]


# ── who gets relayed at all ─────────────────────────────────────────────────────

async def test_a_non_admin_is_relayed_without_pressing_the_button(relayed):
    """People type into a bot's DM. There is nothing else in this one for a driver, so a
    message can only have been meant for a person — and this path keeps working after a
    restart drops the button's in-memory state."""
    message = _Message(user=_User(555))

    await support.relay_stray_message(message)

    assert message.forwarded_to == [111]


async def test_an_admin_typing_into_the_menu_is_not_relayed(relayed):
    """They have buttons; a mistyped one is a mistake, not a support request."""
    message = _Message(user=_User(111, "Boss"))

    await support.relay_stray_message(message)

    assert message.forwarded_to == []
    assert message.answers == []


async def test_an_unknown_command_is_not_relayed(relayed):
    """An unrecognised /command is a typo, and forwarding it as a question would put a
    stream of them in front of the maintainer."""
    message = _Message(user=_User(555), text="/statistics")

    await support.relay_stray_message(message)

    assert message.forwarded_to == []
