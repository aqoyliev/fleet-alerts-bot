"""The per-provider filter on reports.

A company with both fleets has one Telegram group per provider
(company_groups.alert_source), and alerts already route that way. Reports did not: they
counted every violation of the company, so a /report run in the Samsara group answered
with Motive's numbers — contradicting the very feed it was posted in.

These pin the filter that fixed it, including the NULL rule, which is the part that
would quietly rewrite history if it were flipped.
"""
import utils.db_api.violations as v


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class _FetchSpy:
    """Stands in for db.fetch and keeps the SQL and bound parameters it was handed."""

    def __init__(self):
        self.sql = ""
        self.args = ()

    async def __call__(self, sql, *args):
        self.sql = _norm(sql)
        self.args = args
        return []


# ── which rows each provider counts ─────────────────────────────────────────────

async def test_samsara_counts_only_samsara_rows(monkeypatch):
    spy = _FetchSpy()
    monkeypatch.setattr(v.db, "fetch", spy)
    await v.get_violations_by_type("jrd", since=1, until=2, source="samsara")
    assert "AND source = 'samsara'" in spy.sql
    # Strictly: a row with no source is not evidence of a Samsara event, and counting
    # one here would be a wrong number rather than a missing one.
    assert "IS NULL" not in spy.sql


async def test_motive_also_counts_rows_written_before_the_column_existed(monkeypatch):
    """Every violation recorded before `source` was added carries NULL. Motive predates
    Samsara on every company here, so reading those as Motive keeps the report that has
    always been right, right — dropping them would blank out months of history."""
    spy = _FetchSpy()
    monkeypatch.setattr(v.db, "fetch", spy)
    await v.get_violations_by_type("jrd", since=1, until=2, source="motive")
    assert "AND (source = 'motive' OR source IS NULL)" in spy.sql


async def test_no_source_counts_everything(monkeypatch):
    """A group with alert_source NULL receives both providers, so it reports both."""
    spy = _FetchSpy()
    monkeypatch.setattr(v.db, "fetch", spy)
    await v.get_violations_by_type("jrd", since=1, until=2)
    assert "source" not in spy.sql


async def test_top_violators_takes_the_same_filter(monkeypatch):
    """/top and /report are read side by side in the same group; they must agree."""
    spy = _FetchSpy()
    monkeypatch.setattr(v.db, "fetch", spy)
    await v.get_top_violators("jrd", since=1, until=2, source="samsara")
    assert "AND source = 'samsara'" in spy.sql


# ── the clause never carries anything from outside ──────────────────────────────

async def test_an_unrecognized_source_restricts_nothing_and_reaches_no_sql(monkeypatch):
    """The clause is interpolated into an f-string, so it comes from a closed lookup.
    Anything else must fall through to "no filter" rather than into the query."""
    spy = _FetchSpy()
    monkeypatch.setattr(v.db, "fetch", spy)
    await v.get_violations_by_type("jrd", since=1, until=2, source="' OR 1=1 --")
    assert "OR 1=1" not in spy.sql
    assert "source" not in spy.sql


# ── writing the source down ─────────────────────────────────────────────────────

async def test_save_violation_binds_the_source_as_a_parameter(monkeypatch):
    recorded = {}

    async def _execute(sql, *args):
        recorded["sql"] = _norm(sql)
        recorded["args"] = args

    monkeypatch.setattr(v.db, "execute", _execute)
    await v.save_violation("jrd", "4063", "speeding", 7, "2026-08-31", "high", "samsara")
    assert "source" in recorded["sql"]
    assert recorded["args"][-1] == "samsara"


async def test_save_violation_defaults_to_no_source(monkeypatch):
    """Callers that don't know the provider still write a row; it reads as legacy."""
    recorded = {}

    async def _execute(sql, *args):
        recorded["args"] = args

    monkeypatch.setattr(v.db, "execute", _execute)
    await v.save_violation("jrd", "4063", "speeding", 7, "2026-08-31")
    assert recorded["args"][-1] is None
