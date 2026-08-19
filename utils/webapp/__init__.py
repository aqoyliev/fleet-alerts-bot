"""The admin Mini App: an authenticated web panel served from the bot's own process.

`setup_routes(app)` is the only symbol the rest of the codebase touches — it is called
once from start_webhook_server, on the same aiohttp application that already receives the
Motive and Samsara webhooks. Sharing that app is the point: it is already bound to a
public HTTPS URL (which Telegram requires of a Mini App), already inside the event loop
that owns the asyncpg pool, and already holds the Bot instance.
"""

from .routes import setup_routes

__all__ = ["setup_routes"]
