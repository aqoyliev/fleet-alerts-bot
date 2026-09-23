"""Where a relayed support message came from, so an answer can find its way back.

A message somebody sends the bot is put into the maintainer's DM, and the maintainer
answers it by replying to it in Telegram. Telegram hands that reply back to us naming
only the message it replies to — never its author: a forward carries no usable sender id
once the person who wrote it has forward privacy on, which is the default for a lot of
accounts. So the link is written down at relay time instead of being read off the reply.

Both halves of a relay get a row — the forwarded message and the header above it — so
replying to either one reaches the same person. Rows outlive the process on purpose: a
maintainer answering tomorrow morning is the ordinary case, not an edge one, and an
in-memory map would lose exactly those.
"""
from utils.db_api import db


async def remember_relay(admin_chat_id: int, admin_msg_id: int, user_telegram_id: int) -> None:
    """Record that this message in a maintainer's chat belongs to this user.

    ON CONFLICT DO UPDATE rather than DO NOTHING: message ids are unique per chat, so a
    conflict means Telegram reused an id we had already stored for somebody else, and the
    newer relay is the one a reply would be answering.
    """
    await db.execute(
        """
        INSERT INTO support_relays (admin_chat_id, admin_msg_id, user_telegram_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (admin_chat_id, admin_msg_id)
            DO UPDATE SET user_telegram_id = EXCLUDED.user_telegram_id,
                          created_at       = NOW()
        """,
        admin_chat_id, admin_msg_id, user_telegram_id,
    )


async def relay_target(admin_chat_id: int, admin_msg_id: int) -> int | None:
    """Who wrote the message being replied to, or None if it wasn't a relayed one.

    None is the answer for every ordinary reply in an admin's DM, so it has to be cheap
    and it has to be certain: it decides whether the reply is forwarded to a driver or
    left alone.
    """
    return await db.fetchval(
        "SELECT user_telegram_id FROM support_relays "
        "WHERE admin_chat_id = $1 AND admin_msg_id = $2",
        admin_chat_id, admin_msg_id,
    )
