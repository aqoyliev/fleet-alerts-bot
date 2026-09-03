"""Route table for the admin Mini App.

Kept apart from api.py so the URL surface is readable in one screen — which matters more
than usual here, because every path under /panel/api is authenticated and any route added
without a decorator is a hole. Reading the table is how you check that.
"""

import logging
from pathlib import Path

from aiohttp import web

from utils.webapp import api

logger = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

# Resolved from __file__, not the working directory, following utils/db_api/db.py's
# handling of schemas.sql. Procfile happens to run `python app.py` from the repo root,
# but nothing should depend on that staying true.


def _file(name: str, content_type: str):
    async def handler(_request: web.Request) -> web.Response:
        return web.FileResponse(_STATIC / name, headers={
            "Content-Type": content_type,
            # no-store, not a cache header with a max-age: the whole payload is a few tens
            # of KB, and Telegram's in-app WebView caches aggressively enough that a stale
            # app.js after a deploy is genuinely painful to diagnose from a phone.
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        })
    return handler


def setup_routes(app: web.Application) -> None:
    """Mount the panel on the aiohttp app that already serves the provider webhooks.

    Everything lives under /panel, which cannot collide with /webhook/* or /health —
    and if a future route ever does collide, aiohttp raises at startup rather than
    shadowing one silently.
    """
    app.router.add_get("/panel", _file("index.html", "text/html; charset=utf-8"))
    app.router.add_get("/panel/", _file("index.html", "text/html; charset=utf-8"))
    app.router.add_get("/panel/app.css", _file("app.css", "text/css; charset=utf-8"))
    app.router.add_get("/panel/app.js", _file("app.js", "application/javascript; charset=utf-8"))

    # Three explicit file routes rather than add_static: the payload is three known files,
    # and add_static brings a path-traversal and symlink-following surface for no gain.

    app.router.add_get("/panel/api/bootstrap", api.bootstrap)
    app.router.add_get("/panel/api/stats", api.stats)
    app.router.add_get("/panel/api/group", api.group)
    app.router.add_get("/panel/api/alerts", api.alerts)
    app.router.add_get("/panel/api/admins", api.admins)

    # Mutations are POST even where PATCH/DELETE would read better. Telegram's WebView
    # and the proxies in front of it are reliably fine with GET and POST; the others are
    # occasionally not, and a panel that works everywhere beats one that is REST-shaped.
    app.router.add_post("/panel/api/group/enabled", api.set_group_enabled_route)

    app.router.add_post("/panel/api/admins", api.create_admin)
    app.router.add_post("/panel/api/admins/{admin_id}/update", api.update_admin)
    app.router.add_post("/panel/api/admins/{admin_id}/remove", api.delete_admin_route)

    logger.info("[webapp] admin panel mounted at /panel")
