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
- `USERBOT_ENABLED` — `true` turns on the userbot step (see [The userbot](#the-userbot-adding-a-missing-driver-bot)). Default `false`: the whole step is skipped with one log line.
- `USERBOT_SESSION`, `USERBOT_API_ID`, `USERBOT_API_HASH` — the userbot's Telethon StringSession and its my.telegram.org app. The session is a login: keep it secret (it is never logged) and never use it from two places at once.
- `USERBOT_USER_ID` — the userbot account's own Telegram user id, where the driver bot sends "could not add" notices. Optional: unset = read once from Telegram (`get_me`).
- `HOS_USERNAMES_<SLUG>` — e.g. `HOS_USERNAMES_JRD=@hos_anna,@hos_bek` (slug as for `PTI_DATABASE_URL_<SLUG>`, `@` optional). The HOS staff the userbot asks to add it to a group it is not in; the first one is asked. Empty/unset = the admin is told instead.

The service listens on `0.0.0.0` and `::`, so Railway's (IPv6) private network reaches it.
It needs no public domain.

## Routing (who gets the alert)

Per event, after the Vitality driver is known and the daily ordinal counted:

1. **Group.** With `PTI_DATABASE_URL_<SLUG>`: the pti `groups` row whose unit is the truck's, among the usable ones (`is_active AND setup_complete AND NOT post_blocked`, unit not empty), with its `group_drivers` names (members listed in `non_drivers` left out). Units match through `utils/unit_key` on both sides (full key, or the leading token before a suffix like `O/O`). Fetched in one read-only query and cached 60 s; if a refresh fails the last snapshot is used while younger than 10 min. No group → `pti_no_group`; more than one usable group → `pti_ambiguous`; database down and no recent snapshot → `pti_unavailable`. There is **no fallback** to `driver_groups`. Without a pti DB: the imported `driver_groups` mapping as before (no group there = email only).
2. **Name.** The Vitality name must match the pti title or any `group_drivers` name (for `driver_groups`: the Telegram title), same token rule as before (`driver_name_in_title`). With `skip`: no match → `name_mismatch`, title unreadable → `name_unverified`. `driver_notifications.name_match` = TRUE / FALSE / NULL.
3. **Bot membership.** `getChatMember` for the driver bot itself (cached 10 min per chat; errors are not cached). Left / kicked / chat not found → `bot_not_in_group`; restricted without `can_send_messages` → `bot_cannot_post`; supergroup migration → `group_migrated` (the `driver_groups` mapping follows it for the next event); any other error → `bot_check_failed`. On `bot_not_in_group` the userbot step runs (`utils/bot_membership.request_bot_join` → `utils/bot_join.py`, below): if it gets the bot in, the same event is sent right away (its decision line ends `→ sending (driver bot just added by the userbot)`); otherwise the event stays skipped.

A skipped event sends nothing (no group post, no email) and records its `skip_reason`. All three checks also run in DRY RUN (the userbot step only logs there). Each event logs one decision line, e.g.

    [driver] DRY RUN Event 123 jrd/1304: route=pti group=-100123 unit=1304 driver='John Smith' name_match=True (title) bot=member #2 today, charged=True → would send (email=no, media=1)

### Audit the bot's membership

    python -m scripts.audit_driver_bot --company jrd

Needs `DRIVER_ALERTS_BOT_TOKEN` and `PTI_DATABASE_URL_JRD` (from `.env` or the environment). For every usable pti group it asks Telegram whether the driver bot is in it and prints the counts (member / admin / restricted / not member / error) and the units and group ids where it cannot post. Read-only; titles are not printed. Exit 0 = all good, 1 = some groups need the bot, 2 = configuration/DB error.

## The userbot: adding a missing driver bot

The driver bot cannot join a group by itself. When an alert's group (found and name-checked as above) answers `bot_not_in_group`, a Telegram **user** account — the userbot, logged in through Telethon (`utils/userbot.py`) — gets it in. One group at a time per process; every step is logged with `[driver-bot]`, the unit and the group id.

1. The driver bot's membership is read again, uncached (an alert that waited behind another for the same group sees its result).
2. The userbot looks the group up in its own dialogs (Telethon dialog ids are the Bot API's marked ids):
   - **in the group and may add members** (creator, admin with *add members*, or the group lets every member invite) → it adds the driver bot (`InviteToChannelRequest` for a supergroup, `AddChatUserRequest` for a basic group), waits 2 s, re-checks with the driver bot's `getChatMember`. In → **the same alert is sent**. The event was already claimed in `driver_notifications`, and it is sent by the same `notify_driver` call, so there is no second claim and a redelivery is still a no-op.
   - **in the group but cannot add** (no right, a basic group that migrated/was deactivated — pti has a stale id —, a Telegram error, or the add did not take) → the **admin** is told: the **driver bot** DMs the userbot account (`USERBOT_USER_ID`, else its `get_me`):
     `Could not add @<bot> to the group for JRD unit 4042 (group -100…): <reason>. Please add it manually.`
   - **not in the group** → the userbot DMs **one** HOS staff member, the first of `HOS_USERNAMES_<SLUG>` (`bot_join.pick_hos`, where a rotation can go later):

         Can you please add me to this group?
         JRD
         4042 - John Smith

     (company = `companies.name`, else the slug upper-cased; then the pti unit and the Vitality driver, ` - name` left out when unknown). No HOS configured (logged `no HOS configured (HOS_USERNAMES_JRD)`) or the DM fails → the admin is told instead.
3. **Once per 24 h per group**: at most one add attempt and one message (HOS or admin), whatever their outcome, tracked in `driver_bot_join_requests` (company_slug, group_id, unit_number, action `add` / `hos_message` / `admin_message`, outcome, detail, created_at; created at runtime like the other driver tables). Each row is written before its action; if the table cannot be read nothing is done. A burst of alerts for one group therefore sends HOS one message. After HOS adds the userbot, the next alert for that group adds the bot (the add is still unused), even inside the 24 h.
4. **Rate limits.** Userbot actions run one at a time and adds are at least 30 s apart. A `FloodWait` stops all userbot use until it has passed; meanwhile (and when the session is broken or cannot connect) the admin gets **one** notice per flood period / per hour — not one per group — and nothing is recorded for the groups, so they get their normal turn afterwards. A flood wait hit by an add is recorded on that add and the admin is told about that group.
5. **First admin notice.** A bot can only DM a user who has started it. The first time the notice fails with *bot can't initiate conversation*, the userbot sends `/start` to the driver bot (once per process) and the notice is retried. To avoid even that, open the driver bot from the userbot account and press Start once.
6. **Session safety.** Telegram may revoke a session used from two places at once (`AUTH_KEY_DUPLICATED`). The service keeps one Telethon client per process, connects it on first use, disconnects it after 5 min idle and on shutdown. **Do not run `scripts/add_driver_bot.py` (or any other client with the same session) while `USERBOT_ENABLED=true` on driver-alerts.** A revoked / unauthorized session disables the userbot until the service restarts (logged as an error; generate a new `USERBOT_SESSION`).
7. **DRY RUN**: nothing outward and nothing written — no add, no DM, no `/start`; it logs `would add @<bot> (unit 4042, group …)`, `would message HOS @x: …`, `would notify admin: …`. The read-only checks (membership, the userbot's dialogs and rights) still run, so the userbot connects in DRY RUN too. The 24 h de-dup is kept in memory there.

For a one-off backfill of all of a company's groups: `python -m scripts.add_driver_bot --company jrd` (report) / `--apply` (same userbot session — stop the service's userbot first, see 6).

## Rollout

1. Create the `driver-alerts` service from this repo/branch, start command `python driver_service.py`, variables as above with **`DRIVER_ALERTS_DRY_RUN=true`**, `DRIVER_ALERTS_COMPANIES=jrd` and `PTI_DATABASE_URL_JRD`.
2. Check its log for the `Ready` line (it shows the companies, the mismatch mode and `pti DB=jrd:yes`) and that `/health` answers. Run `python -m scripts.audit_driver_bot --company jrd` and add the driver bot to the groups it lists.
3. On web set `DRIVER_ALERTS_FORWARD_URL` and `DRIVER_ALERTS_FORWARD_SECRET`, redeploy. web logs `Driver cell-phone alerts: forwarding to …` at startup and `[driver-forward] Forwarded event …` per event; driver-alerts logs `Accepted …` and the `DRY RUN` decisions.
4. When the dry-run decision lines look right (group, unit, driver, `name_match`, `bot=`), set `DRIVER_ALERTS_DRY_RUN=false` on driver-alerts.
5. Userbot: set `USERBOT_SESSION`, `USERBOT_API_ID`, `USERBOT_API_HASH` (and `USERBOT_USER_ID`, `HOS_USERNAMES_JRD`), press Start on the driver bot from the userbot account, then `USERBOT_ENABLED=true`. The `Ready` line shows `userbot=on`. Try it in DRY RUN first and check the `would add` / `would message HOS` / `would notify admin` lines. Turn it off again with `USERBOT_ENABLED=false`.

## Rollback

Unset `DRIVER_ALERTS_FORWARD_URL` on web (and redeploy). web goes back to company alerts only; the driver-alerts service can then be stopped.

## Notes

- web never waits on or fails because of the forward: it runs as a background task, times out after 5 s, retries at most twice on a network error or 5xx, and only logs on failure. A 401/400 is not retried — check that the secret matches.
- Media bytes are not forwarded; driver-alerts downloads the clip/photo URLs itself.
- A redeploy of driver-alerts cancels notifications still in flight (e.g. a Samsara event in its coaching-state wait); the event stays claimed in `driver_notifications` and is not retried.
