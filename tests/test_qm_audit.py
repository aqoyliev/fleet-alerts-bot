"""Tests for scripts/qm_audit.py: the counting logic (pure functions), the collection
step and the entry point, with QuickManage, Vitality and the pti database faked. The
report must never print names, emails or titles."""
import pytest

import scripts.qm_audit as audit
import utils.pti_groups as pg
import utils.quickmanage as qm
import utils.vitality as vt
from utils.quickmanage import QMDriver, QMTruck, TruckDrivers

SECRETS = ("Zebediah", "Quillfeather", "Marigold", "zeb@example.com", "TITLE-SECRET")


def drv(name, email="x@example.com", did=None):
    return QMDriver(id=did or name, name=name, email=email, phone=None, status="active")


def rec(unit, *, title="", group_drivers=(), qm_state=audit.QM_FOUND, qm_drivers=(),
        v_state=audit.V_DRIVER, v_name=""):
    return audit.UnitRecord(unit=unit, title=title, group_drivers=tuple(group_drivers),
                            qm_state=qm_state, qm_drivers=tuple(qm_drivers),
                            vitality_state=v_state, vitality_name=v_name)


# ── evaluate ─────────────────────────────────────────────────────────────────────

def test_all_three_agree_with_reordered_names():
    ev = audit.evaluate(rec("1", title="1 SMITH, JOHN", qm_drivers=[drv("John Smith")],
                            v_name="Smith John"))
    assert (ev.vit_vs_qm, ev.qm_vs_pti, ev.vit_vs_pti, ev.agree) == ("yes", "yes", "yes", True)


def test_agreement_through_group_drivers_instead_of_title():
    ev = audit.evaluate(rec("1", title="4042 truck group", group_drivers=["John A Smith"],
                            qm_drivers=[drv("John Smith")], v_name="John Smith"))
    assert ev.agree and ev.qm_vs_pti == "yes" and ev.vit_vs_pti == "yes"


def test_vitality_differs_from_qm():
    ev = audit.evaluate(rec("1", title="1 JOHN SMITH", qm_drivers=[drv("Ann Jones")],
                            v_name="John Smith"))
    assert ev.vit_vs_qm == "no" and ev.qm_vs_pti == "no" and ev.vit_vs_pti == "yes"
    assert not ev.agree


def test_any_of_several_qm_drivers_may_match():
    ev = audit.evaluate(rec("1", title="1 JOHN SMITH",
                            qm_drivers=[drv("Ann Jones"), drv("John Smith")], v_name="John Smith"))
    assert ev.agree


def test_agree_needs_the_qm_driver_who_matches_vitality_to_be_in_pti():
    # Vitality ~ Ann Jones, and Ann Jones is not in pti, while John Smith (pti) is not Vitality's
    ev = audit.evaluate(rec("1", title="1 JOHN SMITH",
                            qm_drivers=[drv("Ann Jones"), drv("John Smith")], v_name="Ann Jones"))
    assert ev.vit_vs_qm == "yes" and ev.qm_vs_pti == "yes" and ev.vit_vs_pti == "no"
    assert not ev.agree


def test_not_applicable_and_unknown():
    ev = audit.evaluate(rec("1", title="", qm_drivers=[drv("John Smith")], v_name="John Smith"))
    assert (ev.qm_vs_pti, ev.vit_vs_pti) == ("unknown", "unknown")
    assert not ev.agree
    ev = audit.evaluate(rec("1", title="x", qm_drivers=[], v_name=""))
    assert (ev.vit_vs_qm, ev.qm_vs_pti, ev.vit_vs_pti) == ("n/a", "n/a", "n/a")
    # a Vitality name from a state that is not "driver" is ignored
    ev = audit.evaluate(rec("1", title="JOHN SMITH", qm_drivers=[drv("John Smith")],
                            v_state=audit.V_NO_DRIVER, v_name="John Smith"))
    assert ev.vit_vs_qm == "n/a"


# ── analyze ──────────────────────────────────────────────────────────────────────

RECORDS = [
    # all agree, with email
    rec("1001", title="1001 JOHN SMITH", qm_drivers=[drv("John Smith")], v_name="John Smith"),
    # all agree, driver without email
    rec("1002", title="1002 ANN JONES", qm_drivers=[drv("Ann Jones", email=None)], v_name="Ann Jones"),
    # QM has 2 drivers; Vitality matches the second; pti has the second
    rec("1003", title="1003 BOB LEE", qm_drivers=[drv("Tom Hill"), drv("Bob Lee")], v_name="Bob Lee"),
    # QM has the truck but no active driver
    rec("1004", title="1004 CAL DAY", qm_drivers=[], v_name="Cal Day"),
    # not in QM
    rec("1005", title="1005 DEE FOX", qm_state=audit.QM_NOT_FOUND, v_name="Dee Fox"),
    # ambiguous
    rec("1006", title="1006 EVA GAY", qm_state=audit.QM_AMBIGUOUS, v_name="Eva Gay"),
    # lookup failed
    rec("1007", title="1007 FAY HOY", qm_state=audit.QM_ERROR, v_name="Fay Hoy"),
    # Vitality has nobody
    rec("1008", title="1008 GUS IVY", qm_drivers=[drv("Gus Ivy")], v_state=audit.V_NO_DRIVER),
    # Vitality driver differs from QM and from pti
    rec("1009", title="1009 HAL JAY", qm_drivers=[drv("Hal Jay")], v_name="Ian Kay"),
    # QM driver not in pti, Vitality = QM
    rec("1010", title="1010 SOMEONE ELSE", qm_drivers=[drv("Jo Lane")], v_name="Jo Lane"),
]


def test_analyze_counts():
    a = audit.analyze(RECORDS)
    assert a.groups == 10
    assert dict(a.qm) == {"found": 7, "not_found": 1, "ambiguous": 1, "error": 1}
    assert dict(a.qm_active) == {"0": 1, "1": 5, "2+": 1}
    assert dict(a.vitality) == {"driver": 9, "no_driver": 1}
    # yes: 1001 1002 1003 1010; no: 1009; n/a: no QM drivers (1004-1007) or no Vitality name (1008)
    assert dict(a.vit_vs_qm) == {"yes": 4, "no": 1, "n/a": 5}
    assert dict(a.qm_vs_pti) == {"yes": 5, "no": 1, "n/a": 4}
    assert dict(a.vit_vs_pti) == {"yes": 7, "no": 2, "n/a": 1}
    assert a.agree == 3                      # 1001, 1002, 1003
    assert a.active_drivers == 7             # 1+1+2+0+1+1+1 over the 7 found trucks
    assert a.drivers_with_email == 6         # all but 1002's
    assert a.units_with_email == 5


def test_analyze_lists():
    a = audit.analyze(RECORDS)
    assert a.lists["qm_not_found"] == ["1005"]
    assert a.lists["qm_ambiguous"] == ["1006"]
    assert a.lists["qm_error"] == ["1007"]
    assert a.lists["qm_no_active"] == ["1004"]
    assert a.lists["qm_multi_active"] == ["1003"]
    assert a.lists["vit_none"] == ["1008"]
    assert a.lists["vit_ne_qm"] == ["1009"]
    assert a.lists["qm_ne_pti"] == ["1010"]
    assert a.lists["vit_ne_pti"] == ["1009", "1010"]
    assert a.lists["no_email"] == ["1002"]


def test_unit_lists_are_sorted_and_deduplicated():
    a = audit.analyze([rec("20", qm_state=audit.QM_NOT_FOUND), rec("3", qm_state=audit.QM_NOT_FOUND),
                       rec("3", qm_state=audit.QM_NOT_FOUND)])
    assert a.groups == 3
    assert a.lists["qm_not_found"] == ["20", "3"]


def test_unnamed_vitality_driver_counts_as_no_driver():
    a = audit.analyze([rec("1", qm_drivers=[drv("John Smith")], v_name="")])
    assert a.lists["vit_none"] == ["1"]
    assert a.vit_vs_qm["n/a"] == 1


def test_report_has_counts_and_units_but_no_names():
    records = [
        rec("1001", title="TITLE-SECRET Zebediah Quillfeather", group_drivers=["Marigold Q"],
            qm_drivers=[drv("Zebediah Quillfeather", email="zeb@example.com")],
            v_name="Zebediah Quillfeather"),
        rec("1002", title="TITLE-SECRET", qm_drivers=[drv("Marigold Quillfeather", "zeb@example.com")],
            v_name="Zebediah Quillfeather"),
        rec("1003", qm_state=audit.QM_NOT_FOUND),
    ]
    text = audit.format_report("jrd", audit.analyze(records))
    for secret in SECRETS:
        assert secret not in text
    assert "3 usable pti groups" in text
    assert "Not found in QuickManage (1):" in text and "1003" in text
    assert "All three agree:" in text and "1 of 3" in text
    assert "Vitality driver matches no QuickManage driver (1):" in text


def test_empty_report():
    text = audit.format_report("jrd", audit.analyze([]))
    assert "0 usable pti groups" in text and "(0)" not in text


# ── collect ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def fakes(monkeypatch):
    state = {"qm": {}, "vit_units": {}, "vit_names": {}, "vit_error": None, "qm_calls": []}

    async def truck_drivers(company, unit):
        state["qm_calls"].append((company, unit))
        answer = state["qm"][unit]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def fleet_snapshot(key):
        if state["vit_error"]:
            raise state["vit_error"]
        return state["vit_units"]

    async def driver_name(key, driver_id):
        name = state["vit_names"][driver_id]
        if isinstance(name, Exception):
            raise name
        return name

    async def no_sleep(s):
        pass

    monkeypatch.setattr(audit.quickmanage, "truck_drivers", truck_drivers)
    monkeypatch.setattr(audit.vitality, "fleet_snapshot", fleet_snapshot)
    monkeypatch.setattr(audit.vitality, "driver_name", driver_name)
    monkeypatch.setattr(audit, "_sleep", no_sleep)
    return state


def _found(unit, *drivers):
    return TruckDrivers(unit=unit, truck=QMTruck("t" + unit, unit, "active"), drivers=tuple(drivers))


def _groups():
    return [pg.PtiGroup(-1, "1001 JOHN SMITH", "1001", ("John Smith",)),
            pg.PtiGroup(-2, "1002 X", "1002"),
            pg.PtiGroup(-3, "1003 X", "1003"),
            pg.PtiGroup(-4, "1004 X", "1004"),
            pg.PtiGroup(-5, "1005 X", "1005"),
            pg.PtiGroup(-6, "1006 X", "1006")]


async def test_collect_combines_both_sources(fakes):
    fakes["qm"] = {
        "1001": _found("1001", drv("John Smith")),
        "1002": TruckDrivers(unit="1002", reason=qm.REASON_NO_TRUCK),
        "1003": TruckDrivers(unit="1003", reason=qm.REASON_AMBIGUOUS,
                             matches=(QMTruck("a", "1003"), QMTruck("b", "1003"))),
        "1004": qm.QuickManageUnavailable("down"),
        "1005": qm.QuickManageError("bad"),
        "1006": _found("1006"),
    }
    fakes["vit_units"] = {
        "1001": vt.VitalityUnit("1001", "v1", None),
        "1002": vt.VitalityUnit("1002", None, None),
        "1003": vt.VitalityUnit("1003", "v3", None),
        "1006": vt.VitalityUnit("1006", "v6", None),
    }
    fakes["vit_names"] = {"v1": "John Smith", "v3": "", "v6": vt.VitalityError("x")}
    records = await audit.collect("jrd", "ck", _groups())
    got = {r.unit: (r.qm_state, len(r.qm_drivers), r.vitality_state, r.vitality_name) for r in records}
    assert got == {
        "1001": ("found", 1, "driver", "John Smith"),
        "1002": ("not_found", 0, "no_driver", ""),
        "1003": ("ambiguous", 0, "driver", ""),
        "1004": ("error", 0, "not_in_fleet", ""),
        "1005": ("error", 0, "not_in_fleet", ""),
        "1006": ("found", 0, "driver", ""),
    }
    first = records[0]
    assert first.title == "1001 JOHN SMITH" and first.group_drivers == ("John Smith",)
    assert fakes["qm_calls"][0] == ("jrd", "1001")


async def test_collect_survives_a_dead_vitality_feed(fakes, capsys):
    fakes["qm"] = {g.unit_number: _found(g.unit_number, drv("A B")) for g in _groups()}
    fakes["vit_error"] = vt.VitalityUnavailable("data null")
    records = await audit.collect("jrd", "ck", _groups())
    assert {r.vitality_state for r in records} == {"unavailable"}
    assert "unavailable" in capsys.readouterr().err


async def test_collect_stops_on_refused_qm_credentials(fakes):
    fakes["qm"] = {"1001": qm.QuickManageAuthError("refused")}
    with pytest.raises(qm.QuickManageAuthError):
        await audit.collect("jrd", "ck", _groups())
    assert len(fakes["qm_calls"]) == 1


# ── run ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def configured(monkeypatch, fakes):
    monkeypatch.setenv("PTI_DATABASE_URL_JRD", "postgresql://u:p@localhost/pti")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_ID_JRD", "id")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_SECRET_JRD", "secret")
    monkeypatch.setenv("VITALITY_COMPANY_KEY_JRD", "ck-from-env")
    monkeypatch.setattr(audit.vitality.config, "VITALITY_PROVIDER_KEY", "prov", raising=False)
    closed = []

    async def fetch(company):
        return tuple(_groups()[:1])

    async def pg_close():
        closed.append("pti")

    async def qm_close():
        closed.append("qm")

    async def vt_close():
        closed.append("vitality")

    monkeypatch.setattr(audit.pti_groups, "fetch_usable_groups", fetch)
    monkeypatch.setattr(audit.pti_groups, "close", pg_close)
    monkeypatch.setattr(audit.quickmanage, "close", qm_close)
    monkeypatch.setattr(audit.vitality, "close", vt_close)
    fakes["qm"] = {"1001": _found("1001", drv("Zebediah Quillfeather", "zeb@example.com"))}
    fakes["vit_units"] = {"1001": vt.VitalityUnit("1001", "v1", None)}
    fakes["vit_names"] = {"v1": "Zebediah Quillfeather"}
    return {"closed": closed, "fakes": fakes}


async def test_run_prints_counts_only_and_exits_1_on_a_mismatch(configured, capsys):
    # pti title does not carry the driver -> not all three agree
    code = await audit.run("jrd")
    out = capsys.readouterr().out
    assert code == 1
    assert "1 usable pti groups" in out
    for secret in SECRETS:
        assert secret not in out
    assert set(configured["closed"]) == {"pti", "qm", "vitality"}


async def test_run_exits_0_when_everything_agrees(configured, monkeypatch, capsys):
    async def fetch(company):
        return (pg.PtiGroup(-1, "1001 Zebediah Quillfeather", "1001"),)
    monkeypatch.setattr(audit.pti_groups, "fetch_usable_groups", fetch)
    assert await audit.run("jrd") == 0
    assert "1 of 1" in capsys.readouterr().out


async def test_run_vitality_key_env_option(configured, monkeypatch):
    monkeypatch.delenv("VITALITY_COMPANY_KEY_JRD")
    monkeypatch.setenv("MY_VKEY", "other-key")
    seen = []
    real = audit.vitality.fleet_snapshot

    async def snap(key):
        seen.append(key)
        return await real(key)
    monkeypatch.setattr(audit.vitality, "fleet_snapshot", snap)
    await audit.run("jrd", "MY_VKEY")
    assert seen == ["other-key"]


@pytest.mark.parametrize("missing", ["PTI_DATABASE_URL_JRD", "QUICKMANAGE_CLIENT_SECRET_JRD",
                                     "QUICKMANAGE_CLIENT_ID_JRD", "VITALITY_COMPANY_KEY_JRD"])
async def test_run_config_errors_exit_2(configured, monkeypatch, missing, capsys):
    monkeypatch.delenv(missing)
    monkeypatch.setattr(audit, "_REAL_DATABASE_URL", False)
    assert await audit.run("jrd") == 2
    err = capsys.readouterr().err
    assert "secret" not in err and "ck-from-env" not in err


async def test_run_without_provider_key_exits_2(configured, monkeypatch):
    monkeypatch.setattr(audit.vitality.config, "VITALITY_PROVIDER_KEY", "", raising=False)
    assert await audit.run("jrd") == 2


async def test_run_pti_failure_exits_2(configured, monkeypatch, capsys):
    async def boom(company):
        raise OSError("db down")
    monkeypatch.setattr(audit.pti_groups, "fetch_usable_groups", boom)
    assert await audit.run("jrd") == 2
    assert "pti groups" in capsys.readouterr().err


async def test_run_refused_qm_credentials_exit_2(configured, capsys):
    configured["fakes"]["qm"]["1001"] = qm.QuickManageAuthError("refused")
    assert await audit.run("jrd") == 2
    assert "credentials" in capsys.readouterr().err
    assert "qm" in configured["closed"]


async def test_vitality_key_falls_back_to_the_db_only_with_a_real_database_url(monkeypatch):
    monkeypatch.delenv("VITALITY_COMPANY_KEY_JRD", raising=False)
    calls = []

    async def from_db(company):
        calls.append(company)
        return " db-key "
    monkeypatch.setattr(audit, "_key_from_db", from_db)
    monkeypatch.setattr(audit, "_REAL_DATABASE_URL", False)
    assert await audit.resolve_vitality_key("jrd", None) == ""
    assert calls == []
    monkeypatch.setattr(audit, "_REAL_DATABASE_URL", True)
    assert await audit.resolve_vitality_key("jrd", None) == "db-key"
    monkeypatch.setenv("VITALITY_COMPANY_KEY_JRD", "env-key")
    assert await audit.resolve_vitality_key("jrd", None) == "env-key"
    assert calls == ["jrd"]
