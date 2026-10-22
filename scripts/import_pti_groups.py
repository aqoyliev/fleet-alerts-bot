"""Import a pti-checker-bot `groups` export into nexus-fleet's driver_groups table.

Each pti-checker-bot deployment keeps `groups(group_id, unit_number, setup_complete,
is_active, post_blocked, title, ...)`, mapping a truck unit to its driver Telegram group.
Export it per company, e.g. in psql:

    \\copy (SELECT unit_number, group_id, is_active, setup_complete, post_blocked FROM groups) TO 'acme.csv' CSV HEADER

then preview (the default, writes nothing) and apply:

    python -m scripts.import_pti_groups --company acme --csv acme.csv --known-units acme_units.txt
    python -m scripts.import_pti_groups --company acme --csv acme.csv --apply

The core (parse_rows / plan_import / format_report) is pure; only main() and apply_plan()
touch files and the database. The report never prints group titles, which often carry
driver names.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from utils.unit_key import normalize_unit

MAX_UNIT_LEN = 50  # driver_groups.unit_key / unit_number are VARCHAR(50)

_TRUE = {"t", "true", "1", "y", "yes", "on"}
_FALSE = {"f", "false", "0", "n", "no", "off"}

# (column, value required to keep the row, skip reason, value assumed for an empty/NULL cell)
_FLAG_RULES = (
    ("is_active", True, "inactive", False),
    ("setup_complete", True, "setup_incomplete", False),
    ("post_blocked", False, "post_blocked", False),
)


def parse_bool(value: str | None) -> bool | None:
    """t/f/true/false/1/0/yes/no (any case). None for empty or unrecognised input."""
    v = (value or "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def parse_group_id(value: str | None) -> int | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        return int(v, 10)
    except ValueError:
        return None


@dataclass(frozen=True)
class Row:
    line: int            # 1-based line in the CSV (the header is line 1)
    unit_number: str     # as written in the CSV, surrounding whitespace stripped
    unit_key: str        # normalize_unit(unit_number)
    group_id: int


@dataclass
class ParseResult:
    rows: list[Row] = field(default_factory=list)
    total: int = 0
    skipped: Counter = field(default_factory=Counter)


@dataclass
class ImportPlan:
    to_import: list[Row] = field(default_factory=list)
    duplicate_units: dict[str, list[Row]] = field(default_factory=dict)
    multi_unit_groups: dict[int, list[str]] = field(default_factory=dict)
    unknown_units: list[str] | None = None          # CSV units matching no known vehicle
    units_without_group: list[str] | None = None    # known vehicles that end up with no group


def _sniff_delimiter(text: str) -> str:
    header = text.split("\n", 1)[0]
    return "\t" if header.count("\t") > header.count(",") else ","


def parse_rows(csv_text: str) -> ParseResult:
    """Parse the export and keep only importable rows; count the rest by skip reason."""
    if csv_text.startswith("﻿"):
        csv_text = csv_text[1:]
    reader = csv.reader(io.StringIO(csv_text), delimiter=_sniff_delimiter(csv_text))
    header = next(reader, None)
    if header is None:
        raise ValueError("CSV is empty")
    cols = {name.strip().lower(): i for i, name in enumerate(header)}
    missing = [c for c in ("unit_number", "group_id") if c not in cols]
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(missing)}")

    def cell(record: list[str], name: str) -> str | None:
        i = cols.get(name)
        if i is None:
            return None  # column not exported
        return record[i] if i < len(record) else ""

    result = ParseResult()
    for line, record in enumerate(reader, start=2):
        if not any(c.strip() for c in record):
            continue  # blank line, not a row
        result.total += 1

        unit_number = (cell(record, "unit_number") or "").strip()
        unit_key = normalize_unit(unit_number)
        if not unit_key:
            result.skipped["empty_unit"] += 1
            continue
        if len(unit_number) > MAX_UNIT_LEN:
            result.skipped["unit_too_long"] += 1
            continue
        group_id = parse_group_id(cell(record, "group_id"))
        if group_id is None:
            result.skipped["bad_group_id"] += 1
            continue

        reason = None
        for name, required, skip_reason, if_empty in _FLAG_RULES:
            raw = cell(record, name)
            if raw is None:
                continue
            if raw.strip():
                value = parse_bool(raw)
                if value is None:
                    reason = f"bad_{name}"
                    break
            else:
                value = if_empty
            if value != required:
                reason = skip_reason
                break
        if reason:
            result.skipped[reason] += 1
            continue

        result.rows.append(Row(line, unit_number, unit_key, group_id))
    return result


def parse_known_units(text: str) -> set[str]:
    """One vehicle name per line; blank lines and '#' comments ignored; normalised."""
    if text.startswith("﻿"):
        text = text[1:]
    keys = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key = normalize_unit(line)
        if key:
            keys.add(key)
    return keys


def plan_import(rows: Iterable[Row], known_units: Iterable[str] | None = None) -> ImportPlan:
    rows = list(rows)
    by_key: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_key[row.unit_key].append(row)

    plan = ImportPlan()
    for key, group in by_key.items():
        if len(group) > 1:
            plan.duplicate_units[key] = group
        else:
            plan.to_import.append(group[0])

    by_group: dict[int, list[str]] = defaultdict(list)
    for row in plan.to_import:
        by_group[row.group_id].append(row.unit_key)
    plan.multi_unit_groups = {g: sorted(keys) for g, keys in by_group.items() if len(keys) > 1}

    if known_units is not None:
        known = {k for k in (normalize_unit(u) for u in known_units) if k}
        imported = {r.unit_key for r in plan.to_import}
        plan.unknown_units = sorted(set(by_key) - known)
        plan.units_without_group = sorted(known - imported)
    return plan


def format_report(company: str, parsed: ParseResult, plan: ImportPlan, *, apply: bool) -> str:
    dup_rows = sum(len(v) for v in plan.duplicate_units.values())
    out = [
        f"Company: {company}",
        f"Mode: {'APPLY' if apply else 'DRY RUN (nothing written; pass --apply to write)'}",
        "",
        f"Rows read:   {parsed.total}",
        f"Rows valid:  {len(parsed.rows)}",
        f"To import:   {len(plan.to_import)}",
        f"Skipped:     {sum(parsed.skipped.values()) + dup_rows}",
    ]
    for reason, n in sorted(parsed.skipped.items()):
        out.append(f"  {reason}: {n}")
    if dup_rows:
        out.append(f"  duplicate_unit: {dup_rows} rows ({len(plan.duplicate_units)} units)")

    if plan.duplicate_units:
        out += ["", "Duplicate units (ambiguous, NOT imported):"]
        for key in sorted(plan.duplicate_units):
            parts = ", ".join(f"line {r.line}: {r.unit_number!r} -> {r.group_id}"
                              for r in plan.duplicate_units[key])
            out.append(f"  {key}: {parts}")

    if plan.multi_unit_groups:
        out += ["", "Warning: groups serving several units (imported; check this is intended):"]
        for gid in sorted(plan.multi_unit_groups):
            out.append(f"  {gid}: {', '.join(plan.multi_unit_groups[gid])}")

    if plan.unknown_units is not None:
        out += ["", f"CSV units matching no known vehicle: {len(plan.unknown_units)}"]
        out += [f"  {k}" for k in plan.unknown_units]
        out += ["", f"Known vehicles with no group: {len(plan.units_without_group)}"]
        out += [f"  {k}" for k in plan.units_without_group]
    return "\n".join(out)


async def apply_plan(company: str, plan: ImportPlan) -> int:
    """Upsert every importable row. Re-runs are safe: the upsert keys on (company_slug, unit_key)."""
    from utils.db_api import db
    from utils.db_api.driver_groups import upsert_driver_group  # lazy: module may be new

    await db.init_pool()
    try:
        for row in plan.to_import:
            await upsert_driver_group(company, row.unit_number, row.group_id)
    finally:
        await db.close_pool()
    return len(plan.to_import)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.import_pti_groups",
        description="Import a pti-checker-bot groups CSV into driver_groups (dry run by default).",
    )
    p.add_argument("--company", required=True, help="company slug in nexus-fleet")
    p.add_argument("--csv", required=True, dest="csv_path", help="exported groups CSV (comma or tab)")
    p.add_argument("--known-units", help="file with one known vehicle name per line, for the mismatch report")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", default=False,
                      help="preview only (default)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="write to the database")
    return p


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return f.read()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    company = args.company.strip()
    if not company or len(company) > 50:
        print("error: --company must be 1..50 characters", file=sys.stderr)
        return 2
    try:
        parsed = parse_rows(_read_text(args.csv_path))
        known = parse_known_units(_read_text(args.known_units)) if args.known_units else None
    except (OSError, ValueError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    plan = plan_import(parsed.rows, known)
    print(format_report(company, parsed, plan, apply=args.apply))
    if args.apply:
        written = asyncio.run(apply_plan(company, plan))
        print(f"\nUpserted {written} driver group(s) for {company}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
