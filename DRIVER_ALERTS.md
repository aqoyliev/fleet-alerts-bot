# Driver cell-phone alerts — deploy notes

Two Railway services, built from the same repo and branch, sharing one Postgres.

| Service         | Start command              | Does                                                                 |
|-----------------|----------------------------|----------------------------------------------------------------------|
| `web`           | `python app.py` (Procfile) | Main bot + Motive/Samsara webhooks, company alerts. Forwards each `cell_phone` event to `driver-alerts` (fire-and-forget). |
| `driver-alerts` | `python driver_service.py` | `POST /events` (secret-checked, answers 202, processes in background), `GET /health`. Runs `utils/driver_alerts.notify_driver`: company allowlist, Vitality, Samsara coaching-state gate, daily count, routing (live pti-checker-bot DB, or the imported `driver_groups`), name check, driver-bot membership check, driver-bot post + email, DRY RUN, idempotency via `driver_notifications`. |

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
- `DRIVER_ALERTS_COMPANIES` — comma list of company slugs to handle, any case (e.g. `jrd`). Empty = all. Events of any other company are answered 202 and dropped before any download, DB write or claim (logged at DEBUG, plus one INFO line per company per hour).
- `DRIVER_ALERTS_NAME_MISMATCH` — `skip` (default) or `send`: what to do when the Vitality driver's name is not in the group (see Routing). Any other value acts as `skip`.
- `PTI_DATABASE_URL_<SLUG>` — the company's pti-checker-bot Postgres, e.g. `PTI_DATABASE_URL_JRD` (slug upper-cased, anything not a letter/digit as `_`). Read-only. When set, that company is routed **only** from it.
- `VITALITY_PROVIDER_KEY`, `VITALITY_BASE_URL`
- `RESEND_API_KEY`, `EMAIL_FROM` (driver emails)

The service listens on `0.0.0.0` and `::`, so Railway's (IPv6) private network reaches it.
It needs no public domain.

## Routing (who gets the alert)

Per event, after the Vitality driver is known and the daily ordinal counted:

1. **Group.** With `PTI_DATABASE_URL_<SLUG>`: the pti `groups` row whose unit is the truck's, among the usable ones (`is_active AND setup_complete AND NOT post_blocked`, unit not empty), with its `group_drivers` names (members listed in `non_drivers` left out). Units match through `utils/unit_key` on both sides (full key, or the leading token before a suffix like `O/O`). Fetched in one read-only query and cached 60 s; if a refresh fails the last snapshot is used while younger than 10 min. No group → `pti_no_group`; more than one usable group → `pti_ambiguous`; database down and no recent snapshot → `pti_unavailable`. There is **no fallback** to `driver_groups`. Without a pti DB: the imported `driver_groups` mapping as before (no group there = email only).
2. **Name.** The Vitality name must match the pti title or any `group_drivers` name (for `driver_groups`: the Telegram title), same token rule as before (`driver_name_in_title`). With `skip`: no match → `name_mismatch`, title unreadable → `name_unverified`. `driver_notifications.name_match` = TRUE / FALSE / NULL.
3. **Bot membership.** `getChatMember` for the driver bot itself (cached 10 min per chat; errors are not cached). Left / kicked / chat not found → `bot_not_in_group`; restricted without `can_send_messages` → `bot_cannot_post`; supergroup migration → `group_migrated` (the `driver_groups` mapping follows it for the next event); any other error → `bot_check_failed`. `utils/bot_membership.request_bot_join` is the hook for a later userbot that adds the bot; it currently only logs.

A skipped event sends nothing (no group post, no email) and records its `skip_reason`. All three checks also run in DRY RUN. Each event logs one decision line, e.g.

    [driver] DRY RUN Event 123 jrd/1304: route=pti group=-100123 unit=1304 driver='John Smith' name_match=True (title) bot=member #2 today, charged=True → would send (email=no, media=1)

### Audit the bot's membership

    python -m scripts.audit_driver_bot --company jrd

Needs `DRIVER_ALERTS_BOT_TOKEN` and `PTI_DATABASE_URL_JRD` (from `.env` or the environment). For every usable pti group it asks Telegram whether the driver bot is in it and prints the counts (member / admin / restricted / not member / error) and the units and group ids where it cannot post. Read-only; titles are not printed. Exit 0 = all good, 1 = some groups need the bot, 2 = configuration/DB error.

## Rollout

1. Create the `driver-alerts` service from this repo/branch, start command `python driver_service.py`, variables as above with **`DRIVER_ALERTS_DRY_RUN=true`**, `DRIVER_ALERTS_COMPANIES=jrd` and `PTI_DATABASE_URL_JRD`.
2. Check its log for the `Ready` line (it shows the companies, the mismatch mode and `pti DB=jrd:yes`) and that `/health` answers. Run `python -m scripts.audit_driver_bot --company jrd` and add the driver bot to the groups it lists.
3. On web set `DRIVER_ALERTS_FORWARD_URL` and `DRIVER_ALERTS_FORWARD_SECRET`, redeploy. web logs `Driver cell-phone alerts: forwarding to …` at startup and `[driver-forward] Forwarded event …` per event; driver-alerts logs `Accepted …` and the `DRY RUN` decisions.
4. When the dry-run decision lines look right (group, unit, driver, `name_match`, `bot=`), set `DRIVER_ALERTS_DRY_RUN=false` on driver-alerts.

## Rollback

Unset `DRIVER_ALERTS_FORWARD_URL` on web (and redeploy). web goes back to company alerts only; the driver-alerts service can then be stopped.

## Notes

- web never waits on or fails because of the forward: it runs as a background task, times out after 5 s, retries at most twice on a network error or 5xx, and only logs on failure. A 401/400 is not retried — check that the secret matches.
- Media bytes are not forwarded; driver-alerts downloads the clip/photo URLs itself.
- A redeploy of driver-alerts cancels notifications still in flight (e.g. a Samsara event in its coaching-state wait); the event stays claimed in `driver_notifications` and is not retried.
