"""What the bot says to a driver group: the message posted when it joins, and /help.

Free of aiogram and loader imports so the wording can be tested without a bot token or a
database, the same reason utils/group_parser.py is its own module.

Two people read these strings. The driver needs to know what will start appearing in the
chat and how to silence it; whoever set the group up needs to know why nothing is arriving
yet. So each join outcome ends with the single action that resolves it, and the unhappy
ones say plainly that no alerts will come until it's done — a group that looks connected
but silently receives nothing is the failure this bot keeps running into.

None of these texts advertise /report or /top. Those summarize the whole company's
violations, and driver groups are deliberately not offered them (see
utils/set_bot_commands); they are listed only in the admin section of /help.

Everything here is HTML — the bot's default parse mode.
"""

# Crash alerts are left out of this description on purpose: they never reach a driver
# group or the main group, only admin DMs and the dedicated CRASH_GROUP_ID chat (see
# data/event_catalog and the crash branch in webhook_handler._handle_event).
_WHAT_I_POST = "speeding, hard braking, harsh turns and similar safety events"


def _unit_label(unit: str) -> str:
    """Name a unit without stuttering.

    What is stored is Samsara's own spelling of the vehicle name, and fleets differ: one
    names its trucks "571", the next "unit571". Prefixing blindly gives "unit unit571",
    which reads like a bug in the very message that is supposed to confirm the setup
    worked.
    """
    return unit if unit.strip().lower().startswith(("unit", "truck")) else f"unit {unit}"


def joined_registered(unit: str) -> str:
    """Posted when the bot joins a driver group and the unit resolved cleanly."""
    return (
        f"✅ <b>Connected — {_unit_label(unit)}</b>\n\n"
        f"I'll post this truck's alerts here as they happen: {_WHAT_I_POST}.\n\n"
        "Nothing else to set up. Worth knowing:\n"
        "• <b>/disable</b> — mute alerts in this group (<b>/enable</b> brings them back)\n"
        "• <b>/help</b> — show this again\n\n"
        f"Wrong truck? Correct it with <code>/setunit 1234</code>."
    )


def joined_roster_unavailable(unit: str) -> str:
    """Posted when the bot joins a driver group but Samsara could not be reached.

    Deliberately not a registration. The roster is the authority on how a unit is
    spelled, and alert routing matches that spelling exactly — so a group registered on
    an unverified guess looks connected and then never posts anything. Better to say
    plainly that setup is unfinished and give the one command that finishes it.
    """
    return (
        f"⚠️ <b>Not connected yet — {_unit_label(unit)}</b>\n\n"
        "I read this group's truck number, but couldn't reach Samsara to check how it's "
        "spelled there. Saving it unverified would leave this group silent, so I "
        "haven't saved it.\n\n"
        f"Please run <code>/setunit {unit}</code> in a few minutes to finish setup."
    )


def joined_main_group(company: str) -> str:
    """Posted when the bot joins the company-wide main group."""
    return (
        "✅ <b>Connected — main group</b>\n\n"
        f"This is {company}'s main group, so it receives alerts for <b>every</b> unit in "
        "the fleet rather than one truck's.\n\n"
        "• <b>/disable</b> — mute alerts here (<b>/enable</b> brings them back)\n"
        "• <b>/help</b> — show the commands"
    )


def joined_crash_group(company: str) -> str:
    """Posted when the bot joins the group nominated as CRASH_GROUP_ID.

    Says the quiet part out loud: this chat will look dead for weeks at a stretch. A
    crash-only group that nobody has told is crash-only reads as a broken bot, and
    someone eventually removes it — right before the one message it exists to deliver.
    """
    return (
        "✅ <b>Connected — crash alerts only</b>\n\n"
        f"This group receives <b>crash detections</b> for {company}'s whole fleet, and "
        "nothing else. No speeding, no hard braking — those go to the driver groups.\n\n"
        "<b>Expect silence.</b> No message here means no crash, not a broken bot.\n\n"
        "Please keep me in this group and leave notifications on."
    )


def joined_needs_unit() -> str:
    """Posted when no unit number could be parsed from the group name or description.

    The group is NOT registered at this point, which is the thing to lead with — the bot
    sitting quietly in the chat otherwise looks like it's working.
    """
    return (
        "👋 <b>Almost there — which truck is this group for?</b>\n\n"
        "I couldn't find a unit number in this group's name or description, so I don't "
        "know whose alerts belong here. <b>Nothing will be sent until that's set.</b>\n\n"
        "Send this in the group:\n"
        "<code>/setunit 1234</code>  ← the truck number\n\n"
        "Or put it in the group name (e.g. <b>UNIT: 1234 John Smith</b>) and re-add me."
    )


def joined_unknown_unit(company: str, unit: str, suggestions: list[str] | None = None) -> str:
    """Posted when a unit was parsed from the group name but Samsara has no such vehicle —
    usually a typo in the title, or a truck not yet added to the Samsara org."""
    hint = ""
    if suggestions:
        hint = "\nClosest trucks in Samsara: " + ", ".join(f"<code>{s}</code>" for s in suggestions) + "\n"
    return (
        f"⚠️ <b>Almost there — unit {unit} isn't in Samsara</b>\n\n"
        f"I read unit <code>{unit}</code> from this group's name, but {company} has no "
        "such truck in Samsara, so I can't route alerts here yet. "
        "<b>Nothing will be sent until that's fixed.</b>\n"
        f"{hint}\n"
        "Set the right number with <code>/setunit 1234</code>."
    )


def help_text(company: str, *, unit: str | None = None, is_main: bool = False,
              is_crash: bool = False, is_admin: bool = False) -> str:
    """The /help reply inside a group.

    Deliberately not the same text as the DM /help (handlers/users/help.py): that one is a
    wall of admin instructions, and this one is read by a driver in their own truck's chat.
    The admin block is appended only for bot admins, so the driver sees four commands.
    """
    lines = [f"🚛 <b>{company} — Fleet Alerts</b>\n"]

    if is_crash:
        # Checked before is_main and before the unregistered-group warning: the crash
        # group has no row in alert_groups on purpose, so both of those would otherwise
        # describe it wrongly — the second one alarmingly so.
        lines.append(
            "I post <b>crash detections</b> for the whole fleet here, and nothing else. "
            "Silence means no crash.\n"
        )
    elif is_main:
        lines.append(
            f"This is the main group: I post <b>every</b> unit's alerts here — "
            f"{_WHAT_I_POST}.\n"
        )
    elif unit:
        lines.append(f"I post <b>{_unit_label(unit)}</b>'s alerts here — {_WHAT_I_POST}.\n")
    else:
        # An unregistered group is the case where /help matters most: someone is asking
        # because nothing is arriving, and this is the answer.
        lines.append(
            "⚠️ This group isn't set up yet — I don't know which truck it belongs to, so "
            "<b>no alerts are being sent</b>.\n"
            "Fix it with <code>/setunit 1234</code>.\n"
        )

    lines.append("<b>Commands</b>")
    if is_crash:
        # /setunit, /disable and /enable all need a row in alert_groups, and the crash
        # group deliberately has none. Offering them here would hand someone a command
        # that answers "this group isn't registered" — in the group that matters most.
        lines.append("/help — this message")
    else:
        if not is_main:
            lines.append("/setunit 1234 — set or correct this group's unit")
        lines += [
            "/disable — mute alerts in this group",
            "/enable — turn them back on",
            "/help — this message",
        ]

    if is_admin and is_crash:
        # No /events or /removegroup: this group's membership comes from CRASH_GROUP_ID
        # in the deployment's .env, so there is no filter to edit and no row to remove.
        lines += [
            "\n🔑 <b>Admins</b>",
            "/report — yesterday's violations · /top — today's worst units",
            "Crash routing is set by <code>CRASH_GROUP_ID</code> in the deployment config.",
        ]
    elif is_admin:
        lines += [
            "\n🔑 <b>Admins</b>",
            "/events — choose which event types this group gets",
            "/event_list — show the current filter",
            "/report — yesterday's violations · /top — today's worst units",
            "/removegroup — unregister this group",
        ]

    return "\n".join(lines)
