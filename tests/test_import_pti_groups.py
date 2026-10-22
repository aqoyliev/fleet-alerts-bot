"""Tests for scripts/import_pti_groups.py (pti-checker groups CSV -> driver_groups).

The import decides which Telegram group a truck's alerts go to, so a wrong row sends a
driver someone else's alerts. These pin what is kept, what is refused, and that a dry
run never writes. No database is touched: the upsert and pool helpers are faked.
"""
import types

import pytest

import scripts.import_pti_groups as imp
import utils.db_api.db as db

HEADER = "unit_number,group_id,is_active,setup_complete,post_blocked,title\n"


def _units(parsed):
    return [r.unit_key for r in parsed.rows]


# ── parsing ────────────────────────────────────────────────────────────────────

def test_messy_units_normalize():
    text = (HEADER
            + '"<1304 >",-1001,t,t,f,x\n'
            + "t-120,-1002,t,t,f,x\n"
            + "f9121,-1003,t,t,f,x\n"
            + '" 55 12 ",-1004,t,t,f,x\n')
    parsed = imp.parse_rows(text)
    assert _units(parsed) == ["1304", "T120", "F9121", "5512"]
    assert parsed.rows[0].unit_number == "<1304 >"
    assert [r.group_id for r in parsed.rows] == [-1001, -1002, -1003, -1004]


@pytest.mark.parametrize("raw,expected", [
    ("t", True), ("T", True), ("true", True), ("TRUE", True), ("1", True), (" yes ", True),
    ("f", False), ("False", False), ("0", False), ("no", False),
    ("", None), ("maybe", None), (None, None),
])
def test_parse_bool_variants(raw, expected):
    assert imp.parse_bool(raw) is expected


@pytest.mark.parametrize("flags,reason", [
    ("f,t,f", "inactive"),
    ("false,true,false", "inactive"),
    ("t,f,f", "setup_incomplete"),
    ("1,0,0", "setup_incomplete"),
    ("t,t,t", "post_blocked"),
    ("true,true,1", "post_blocked"),
    ("x,t,f", "bad_is_active"),
    (",t,f", "inactive"),        # NULL is_active is not active
    ("t,,f", "setup_incomplete"),
])
def test_flag_filters(flags, reason):
    parsed = imp.parse_rows(HEADER + f"1304,-100,{flags},title\n")
    assert parsed.rows == []
    assert parsed.skipped == {reason: 1}


def test_null_post_blocked_is_not_blocked():
    parsed = imp.parse_rows(HEADER + "1304,-100,t,t,,title\n")
    assert _units(parsed) == ["1304"]


def test_flags_optional_when_columns_absent():
    parsed = imp.parse_rows("unit_number,group_id\n1304,-100\n")
    assert _units(parsed) == ["1304"]
    assert not parsed.skipped


def test_empty_unit_and_bad_group_id_skipped():
    text = (HEADER
            + ",-100,t,t,f,x\n"
            + '"<  >",-101,t,t,f,x\n'
            + "1304,,t,t,f,x\n"
            + "1305,abc,t,t,f,x\n"
            + "1306,-1.5,t,t,f,x\n"
            + "1307,-1001234567890,t,t,f,x\n"
            + "\n")
    parsed = imp.parse_rows(text)
    assert parsed.total == 6
    assert parsed.skipped == {"empty_unit": 2, "bad_group_id": 3}
    assert [(r.unit_key, r.group_id) for r in parsed.rows] == [("1307", -1001234567890)]


def test_missing_required_column_raises():
    with pytest.raises(ValueError, match="group_id"):
        imp.parse_rows("unit_number,title\n1304,x\n")


def test_bom_and_tab_delimited():
    text = "﻿unit_number\tgroup_id\tis_active\n1304\t-100\tt\nT-9\t-200\tf\n"
    parsed = imp.parse_rows(text)
    assert _units(parsed) == ["1304"]
    assert parsed.skipped == {"inactive": 1}


def test_bom_comma_header_case_insensitive():
    parsed = imp.parse_rows("﻿Unit_Number , Group_ID\n1304,-100\n")
    assert _units(parsed) == ["1304"]


# ── planning ───────────────────────────────────────────────────────────────────

def _rows(*pairs):
    text = "unit_number,group_id\n" + "".join(f"{u},{g}\n" for u, g in pairs)
    return imp.parse_rows(text).rows


def test_duplicate_units_excluded_and_reported():
    plan = imp.plan_import(_rows(("1304", -1), ("<1304 >", -2), ("t-120", -3)))
    assert [r.unit_key for r in plan.to_import] == ["T120"]
    assert set(plan.duplicate_units) == {"1304"}
    assert [r.group_id for r in plan.duplicate_units["1304"]] == [-1, -2]


def test_same_group_multi_unit_is_imported_with_warning():
    plan = imp.plan_import(_rows(("1304", -9), ("1305", -9), ("1306", -8)))
    assert [r.unit_key for r in plan.to_import] == ["1304", "1305", "1306"]
    assert plan.multi_unit_groups == {-9: ["1304", "1305"]}


def test_known_units_mismatch_both_directions():
    plan = imp.plan_import(
        _rows(("1304", -1), ("T-120", -2), ("999", -3), ("777", -4), ("777", -5)),
        known_units=["1304 ", "t120", "F9121", "777", ""],
    )
    assert plan.unknown_units == ["999"]
    # 777 is in the CSV but ambiguous, so it ends up with no group
    assert plan.units_without_group == ["777", "F9121"]


def test_no_known_units_means_no_mismatch_report():
    plan = imp.plan_import(_rows(("1304", -1)))
    assert plan.unknown_units is None and plan.units_without_group is None


def test_parse_known_units_file():
    assert imp.parse_known_units("﻿# fleet\n1304\n\n t-120 \n<>\n") == {"1304", "T120"}


def test_report_omits_titles():
    text = HEADER + "1304,-1,t,t,f,Driver Secretname\n1304,-2,t,t,f,Other Person\n"
    parsed = imp.parse_rows(text)
    report = imp.format_report("acme", parsed, imp.plan_import(parsed.rows), apply=False)
    assert "Secretname" not in report and "Other Person" not in report
    assert "1304" in report and "-1" in report and "DRY RUN" in report


# ── dry run vs apply ───────────────────────────────────────────────────────────

@pytest.fixture
def fake_db(monkeypatch):
    calls = {"upserts": [], "init": 0, "close": 0}

    async def upsert_driver_group(company_slug, unit_number, telegram_group_id):
        calls["upserts"].append((company_slug, unit_number, telegram_group_id))

    async def init_pool():
        calls["init"] += 1

    async def close_pool():
        calls["close"] += 1

    fake_module = types.ModuleType("utils.db_api.driver_groups")
    fake_module.upsert_driver_group = upsert_driver_group
    monkeypatch.setitem(__import__("sys").modules, "utils.db_api.driver_groups", fake_module)
    monkeypatch.setattr(db, "init_pool", init_pool)
    monkeypatch.setattr(db, "close_pool", close_pool)
    return calls


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "groups.csv"
    p.write_text(HEADER + "1304,-1,t,t,f,x\nT-120,-2,t,t,f,x\n55,-3,f,t,f,x\n", encoding="utf-8-sig")
    return str(p)


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_dry_run_never_upserts(fake_db, csv_file, capsys, extra):
    assert imp.main(["--company", "acme", "--csv", csv_file, *extra]) == 0
    assert fake_db == {"upserts": [], "init": 0, "close": 0}
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_upserts_kept_rows(fake_db, csv_file, capsys):
    assert imp.main(["--company", "acme", "--csv", csv_file, "--apply"]) == 0
    assert fake_db["upserts"] == [("acme", "1304", -1), ("acme", "T-120", -2)]
    assert fake_db["init"] == 1 and fake_db["close"] == 1
    assert "Upserted 2" in capsys.readouterr().out


def test_apply_rerun_is_same_calls(fake_db, csv_file):
    imp.main(["--company", "acme", "--csv", csv_file, "--apply"])
    imp.main(["--company", "acme", "--csv", csv_file, "--apply"])
    first, second = fake_db["upserts"][:2], fake_db["upserts"][2:]
    assert first == second


def test_dry_run_and_apply_are_exclusive(csv_file):
    with pytest.raises(SystemExit):
        imp.main(["--company", "acme", "--csv", csv_file, "--dry-run", "--apply"])


def test_known_units_cli(fake_db, csv_file, tmp_path, capsys):
    known = tmp_path / "known.txt"
    known.write_text("1304\nF9121\n", encoding="utf-8")
    imp.main(["--company", "acme", "--csv", csv_file, "--known-units", str(known)])
    out = capsys.readouterr().out
    assert "CSV units matching no known vehicle: 1\n  T120" in out
    assert "Known vehicles with no group: 1\n  F9121" in out
