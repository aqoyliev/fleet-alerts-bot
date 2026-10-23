# Driver cell-phone alerts — deploy notes

Two Railway services, built from the same repo and branch, sharing one Postgres.

| Service         | Start command              | Does                                                                 |
|-----------------|----------------------------|----------------------------------------------------------------------|
| `web`           | `python app.py` (Procfile) | Main bot + Motive/Samsara webhooks, company alerts. Forwards each `cell_phone` event to `driver-alerts` (fire-and-forget). |
| `driver-alerts` | `python driver_service.py` | `POST /events` (secret-checked, answers 202, processes in background), `GET /health`. Runs `utils/driver_alerts.notify_driver`: Vitality, Samsara coaching-state gate, name check vs group title, daily count, driver-bot post + email, DRY RUN, idempotency via `driver_notifications`. |

`driver-alerts` never polls the main bot or sets its webhook; it only sends through the driver bot.

## Environment variables

**web** (in addition to what it already has)

- `DRIVER_ALERTS_FORWARD_URL` — `http://driver-alerts.railway.internal:8080/events`
  (`<service-name>.railway.internal`, plain `http`, the port `driver-alerts` listens on). Empty = forwarding off.
- `DRIVER_ALERTS_FORWARD_SECRET` — shared secret, same value on both services.
- `DRIVER_ALERTS_BOT_TOKEN`, `VITALITY_*` and the other `DRIVER_ALERTS_*` settings are no longer used by web and can be removed from it.

**driver-alerts**

- Required by `data/config.py` at import (share them from web): `BOT_TOKEN`, `ADMINS`, `ip`, `GROUP_CHAT_ID`, `DATABASE_URL`.
- `DRIVER_ALERTS_FORWARD_SECRET` — same value as on web. The service refuses to start if it is empty.
- `PORT` — set it to `8080` explicitly so it matches the URL on web (defaults to 8080).
- `DRIVER_ALERTS_BOT_TOKEN` — the driver bot (unset = events are accepted but nothing is sent).
- `DRIVER_ALERTS_DRY_RUN`, `DRIVER_ALERTS_COUNT_FROM`
- `VITALITY_PROVIDER_KEY`, `VITALITY_BASE_URL`
- `RESEND_API_KEY`, `EMAIL_FROM` (driver emails)

The service listens on `0.0.0.0` and `::`, so Railway's (IPv6) private network reaches it.
It needs no public domain.

## Rollout

1. Create the `driver-alerts` service from this repo/branch, start command `python driver_service.py`, variables as above with **`DRIVER_ALERTS_DRY_RUN=true`**.
2. Check its log for the `Ready` line and that `/health` answers.
3. On web set `DRIVER_ALERTS_FORWARD_URL` and `DRIVER_ALERTS_FORWARD_SECRET`, redeploy. web logs `Driver cell-phone alerts: forwarding to …` at startup and `[driver-forward] Forwarded event …` per event; driver-alerts logs `Accepted …` and the `DRY RUN` decisions.
4. When the dry-run decisions look right, set `DRIVER_ALERTS_DRY_RUN=false` on driver-alerts.

## Rollback

Unset `DRIVER_ALERTS_FORWARD_URL` on web (and redeploy). web goes back to company alerts only; the driver-alerts service can then be stopped.

## Notes

- web never waits on or fails because of the forward: it runs as a background task, times out after 5 s, retries at most twice on a network error or 5xx, and only logs on failure. A 401/400 is not retried — check that the secret matches.
- Media bytes are not forwarded; driver-alerts downloads the clip/photo URLs itself.
- A redeploy of driver-alerts cancels notifications still in flight (e.g. a Samsara event in its coaching-state wait); the event stays claimed in `driver_notifications` and is not retried.
