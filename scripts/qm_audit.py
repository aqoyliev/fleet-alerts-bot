"""Do QuickManage, Vitality and pti-checker-bot agree on who drives each truck?

    python -m scripts.qm_audit --company jrd [--vitality-key-env NAME]

For every usable pti group of the company (utils.pti_groups.fetch_usable_groups) it takes
the group's unit and asks
  * QuickManage (utils.quickmanage.truck_drivers): the truck and its active drivers,
  * Vitality (utils.vitality): the driver assigned to that unit right now,
and compares both with the pti group's title / group_drivers names, with the same token
rule the alerts use (utils.driver_alerts.driver_name_in_title / name_check).

Needs: PTI_DATABASE_URL_<SLUG>, QUICKMANAGE_CLIENT_ID_<SLUG> / _SECRET_<SLUG>,
VITALITY_PROVIDER_KEY, and the carrier's Vitality company key: from the env var named by
--vitality-key-env (default VITALITY_COMPANY_KEY_<SLUG>), else, when DATABASE_URL is a real
one, companies.vitality_company_key (a single SELECT). Read-only: nothing is written or
sent anywhere. Prints counts and unit numbers only -- never names, emails or titles.

Exit status: 0 = every unit agrees on all three, 1 = some units need a look,
2 = configuration or database error.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass, field

from environs import Env

# Load .env first, then fill in what data.config demands at import but this script never
# uses (the main bot is not contacted), as scripts/audit_driver_bot.py does.
Env().read_env()
_REAL_DATABASE_URL = bool(os.environ.get("DATABASE_URL"))
for _name, _placeholder in (("BOT_TOKEN", "0:unused"), ("ADMINS", "0"), ("ip", "127.0.0.1"),
                            ("GROUP_CHAT_ID", "0"), ("DATABASE_URL", "postgresql://unused")):
    os.environ.setdefault(_name, _placeholder)

from data import config  # noqa: E402
from utils import pti_groups, quickmanage, vitality  # noqa: E402
from utils.driver_alerts import driver_name_in_title, name_check  # noqa: E402

PAUSE = 0.05       # between units, to go easy on the (rate-limited) APIs
_sleep = asyncio.sleep   # tests patch this

# QuickManage states
QM_FOUND, QM_NOT_FOUND, QM_AMBIGUOUS, QM_ERROR = "found", "not_found", "ambiguous", "error"
# Vitality states
V_DRIVER, V_NO_DRIVER, V_NOT_IN_FLEET, V_UNAVAILABLE = ("driver", "no_driver", "not_in_fleet",
                                                       "unavailable")
YES, NO, UNKNOWN, NA = "yes", "no", "unknown", "n/a"


@dataclass(frozen=True)
class UnitRecord:
    """Everything known about one pti group's truck. `qm_drivers` are the ACTIVE
    QuickManage drivers (utils.quickmanage.QMDriver); `vitality_name` is only meaningful
    when vitality_state is V_DRIVER ("" = Vitality has a driver id but no name)."""
    unit: str
    title: str = ""
    group_drivers: tuple[str, ...] = ()
    qm_state: str = QM_NOT_FOUND
    qm_drivers: tuple = ()
    vitality_state: str = V_NO_DRIVER
    vitality_name: str = ""


@dataclass(frozen=True)
class UnitEval:
    vit_vs_qm: str      # a QM driver matches the Vitality name
    qm_vs_pti: str      # a QM driver's name is in the pti title / group_drivers
    vit_vs_pti: str     # the Vitality name is in the pti title / group_drivers
    agree: bool         # all three: Vitality ~ a QM driver, who is in pti, and Vitality in pti


def _tri(results: list[bool | None]) -> str:
    """yes if any True; unknown if there was nothing to compare with; else no."""
    if not results:
        return NA
    if any(r is True for r in results):
        return YES
    if all(r is None for r in results):
        return UNKNOWN
    return NO


def evaluate(rec: UnitRecord) -> UnitEval:
    vit = rec.vitality_name if rec.vitality_state == V_DRIVER else ""
    qm_names = [d.name for d in rec.qm_drivers if d.name]

    vit_vs_qm = NA
    if vit and qm_names:
        vit_vs_qm = YES if any(driver_name_in_title(vit, n) for n in qm_names) else NO

    qm_pti = [name_check(n, rec.title, rec.group_drivers)[0] for n in qm_names]
    vit_pti = [name_check(vit, rec.title, rec.group_drivers)[0]] if vit else []

    agree = False
    if vit and _tri(vit_pti) == YES:
        agree = any(driver_name_in_title(vit, n)
                    and name_check(n, rec.title, rec.group_drivers)[0] is True
                    for n in qm_names)
    return UnitEval(vit_vs_qm, _tri(qm_pti), _tri(vit_pti), agree)


# Mismatch categories, in report order: key -> heading
CATEGORIES = {
    "qm_not_found": "Not found in QuickManage",
    "qm_ambiguous": "Several QuickManage trucks with this unit",
    "qm_error": "QuickManage lookup failed",
    "qm_no_active": "QuickManage truck with 0 active drivers",
    "qm_multi_active": "QuickManage truck with 2+ active drivers",
    "vit_none": "No Vitality driver (none assigned / not in fleet / unavailable / unnamed)",
    "vit_ne_qm": "Vitality driver matches no QuickManage driver",
    "qm_ne_pti": "No QuickManage driver in the pti title / group_drivers",
    "vit_ne_pti": "Vitality driver not in the pti title / group_drivers",
    "no_email": "QuickManage has active drivers but none with an email",
}


@dataclass
class Analysis:
    groups: int = 0
    qm: Counter = field(default_factory=Counter)
    qm_active: Counter = field(default_factory=Counter)
    vitality: Counter = field(default_factory=Counter)
    vit_vs_qm: Counter = field(default_factory=Counter)
    qm_vs_pti: Counter = field(default_factory=Counter)
    vit_vs_pti: Counter = field(default_factory=Counter)
    agree: int = 0
    active_drivers: int = 0
    drivers_with_email: int = 0
    units_with_email: int = 0
    lists: dict = field(default_factory=lambda: {k: [] for k in CATEGORIES})


def analyze(records) -> Analysis:
    """Counts and per-category unit lists for the records. Pure; no names in the result."""
    a = Analysis()
    for rec in records:
        a.groups += 1
        a.qm[rec.qm_state] += 1
        a.vitality[rec.vitality_state] += 1
        ev = evaluate(rec)
        lists = a.lists

        if rec.qm_state == QM_NOT_FOUND:
            lists["qm_not_found"].append(rec.unit)
        elif rec.qm_state == QM_AMBIGUOUS:
            lists["qm_ambiguous"].append(rec.unit)
        elif rec.qm_state == QM_ERROR:
            lists["qm_error"].append(rec.unit)
        else:
            n = len(rec.qm_drivers)
            a.qm_active["0" if n == 0 else "1" if n == 1 else "2+"] += 1
            a.active_drivers += n
            with_email = sum(1 for d in rec.qm_drivers if d.email)
            a.drivers_with_email += with_email
            if with_email:
                a.units_with_email += 1
            if n == 0:
                lists["qm_no_active"].append(rec.unit)
            elif n > 1:
                lists["qm_multi_active"].append(rec.unit)
            if n and not with_email:
                lists["no_email"].append(rec.unit)

        if rec.vitality_state != V_DRIVER or not rec.vitality_name:
            lists["vit_none"].append(rec.unit)
        a.vit_vs_qm[ev.vit_vs_qm] += 1
        a.qm_vs_pti[ev.qm_vs_pti] += 1
        a.vit_vs_pti[ev.vit_vs_pti] += 1
        if ev.vit_vs_qm == NO:
            lists["vit_ne_qm"].append(rec.unit)
        if ev.qm_vs_pti == NO:
            lists["qm_ne_pti"].append(rec.unit)
        if ev.vit_vs_pti == NO:
            lists["vit_ne_pti"].append(rec.unit)
        if ev.agree:
            a.agree += 1
    for key in a.lists:
        a.lists[key] = sorted(set(a.lists[key]))
    return a


def format_report(company: str, a: Analysis) -> str:
    def row(label: str, counter: Counter, keys) -> list[str]:
        return [f"  {label}"] + [f"    {k:<14}{counter.get(k, 0)}" for k in keys]

    lines = [f"QuickManage / Vitality / pti agreement for {company}: {a.groups} usable pti groups",
             ""]
    lines += row("Unit in QuickManage:", a.qm,
                 (QM_FOUND, QM_NOT_FOUND, QM_AMBIGUOUS, QM_ERROR))
    lines += row("Active drivers per QuickManage truck:", a.qm_active, ("0", "1", "2+"))
    lines += row("Vitality driver for the unit:", a.vitality,
                 (V_DRIVER, V_NO_DRIVER, V_NOT_IN_FLEET, V_UNAVAILABLE))
    lines += row("Vitality name matches a QuickManage driver:", a.vit_vs_qm, (YES, NO, NA))
    lines += row("QuickManage driver in pti title / group_drivers:", a.qm_vs_pti,
                 (YES, NO, UNKNOWN, NA))
    lines += row("Vitality driver in pti title / group_drivers:", a.vit_vs_pti,
                 (YES, NO, UNKNOWN, NA))
    lines += ["  All three agree:", f"    {'units':<14}{a.agree} of {a.groups}",
              "  Emails (QuickManage, active drivers):",
              f"    {'with email':<14}{a.drivers_with_email} of {a.active_drivers} drivers",
              f"    {'units':<14}{a.units_with_email} with at least one"]
    for key, heading in CATEGORIES.items():
        units = a.lists[key]
        if units:
            lines += ["", f"{heading} ({len(units)}):", "  " + ", ".join(units)]
    return "\n".join(lines)


# ── Collection ───────────────────────────────────────────────────────────────────

async def _qm_part(company: str, unit: str) -> tuple[str, tuple]:
    """(state, active drivers). A QuickManageAuthError propagates (stop the audit)."""
    try:
        result = await quickmanage.truck_drivers(company, unit)
    except quickmanage.QuickManageAuthError:
        raise
    except quickmanage.QuickManageError:
        return QM_ERROR, ()
    if result.found:
        return QM_FOUND, result.drivers
    if result.reason == quickmanage.REASON_AMBIGUOUS:
        return QM_AMBIGUOUS, ()
    return QM_NOT_FOUND, ()


async def _vitality_part(company_key: str, snapshot, unit: str) -> tuple[str, str]:
    """(state, driver name). `snapshot` is the fleet snapshot, or None when it failed."""
    if snapshot is None:
        return V_UNAVAILABLE, ""
    found = vitality.lookup(snapshot, unit)
    if found is None:
        return V_NOT_IN_FLEET, ""
    if not found.driver_id:
        return V_NO_DRIVER, ""
    try:
        return V_DRIVER, await vitality.driver_name(company_key, found.driver_id)
    except vitality.VitalityError:
        return V_DRIVER, ""


async def collect(company: str, company_key: str, groups) -> list[UnitRecord]:
    try:
        snapshot = await vitality.fleet_snapshot(company_key)
    except vitality.VitalityError as e:
        print(f"Vitality is unavailable ({type(e).__name__}: {e}); "
              f"every unit counts as '{V_UNAVAILABLE}'", file=sys.stderr)
        snapshot = None
    records = []
    for g in groups:
        qm_state, qm_drivers = await _qm_part(company, g.unit_number)
        v_state, v_name = await _vitality_part(company_key, snapshot, g.unit_number)
        records.append(UnitRecord(
            unit=g.unit_number, title=g.title, group_drivers=tuple(g.driver_names),
            qm_state=qm_state, qm_drivers=qm_drivers,
            vitality_state=v_state, vitality_name=v_name))
        await _sleep(PAUSE)
    return records


# ── Entry point ──────────────────────────────────────────────────────────────────

async def _key_from_db(company: str) -> str | None:
    from utils.db_api import companies, db
    await db.init_pool()
    try:
        return await companies.get_vitality_company_key(company)
    finally:
        await db.close_pool()


async def resolve_vitality_key(company: str, env_name: str | None) -> str:
    """The carrier's Vitality company key: the env var (``env_name``, default
    VITALITY_COMPANY_KEY_<SLUG>), else companies.vitality_company_key when DATABASE_URL is
    a real one. "" when neither has it."""
    name = env_name or "VITALITY_COMPANY_KEY_" + config.slug_env_suffix(company)
    key = os.environ.get(name, "").strip()
    if key or not _REAL_DATABASE_URL:
        return key
    return ((await _key_from_db(company)) or "").strip()


async def run(company: str, vitality_key_env: str | None = None) -> int:
    if not pti_groups.is_configured(company):
        print(f"{config.PTI_DATABASE_URL_PREFIX}<SLUG> is not set for {company!r}", file=sys.stderr)
        return 2
    if not quickmanage.is_configured(company):
        print(f"{config.QUICKMANAGE_CLIENT_ID_PREFIX}<SLUG> / "
              f"{config.QUICKMANAGE_CLIENT_SECRET_PREFIX}<SLUG> are not set for {company!r}",
              file=sys.stderr)
        return 2
    if not vitality.is_configured():
        print("VITALITY_PROVIDER_KEY is not set", file=sys.stderr)
        return 2
    try:
        company_key = await resolve_vitality_key(company, vitality_key_env)
    except Exception as e:
        print(f"Could not read the Vitality company key: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if not company_key:
        name = vitality_key_env or "VITALITY_COMPANY_KEY_" + config.slug_env_suffix(company)
        print(f"No Vitality company key for {company!r}: set {name} (or a real DATABASE_URL "
              f"with companies.vitality_company_key)", file=sys.stderr)
        return 2

    try:
        groups = await pti_groups.fetch_usable_groups(company)
    except Exception as e:
        print(f"Could not read the {company} pti groups: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        await pti_groups.close()

    try:
        records = await collect(company, company_key, groups)
    except quickmanage.QuickManageError as e:
        print(f"QuickManage refused the {company} credentials: {e}", file=sys.stderr)
        return 2
    finally:
        await quickmanage.close()
        await vitality.close()

    analysis = analyze(records)
    print(format_report(company, analysis))
    return 0 if analysis.agree == analysis.groups else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--company", required=True, help="company slug, e.g. jrd")
    parser.add_argument("--vitality-key-env", metavar="NAME",
                        help="env var holding the carrier's Vitality company key "
                             "(default VITALITY_COMPANY_KEY_<SLUG>; else read from the DB)")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.company.strip().lower(), args.vitality_key_env))


if __name__ == "__main__":
    sys.exit(main())
