"""Carriers that moved to Vitality Plus keep their company key but need the Plus
provider key: the first key answers 403 or an empty fleet for them."""
import pytest

import utils.vitality as vit

OLD, PLUS = "old-provider", "plus-provider"
OK = {"description": "success", "data": [{"number": "4042", "driver_id": "d1"}], "total_pages": 1}


@pytest.fixture
def keys(monkeypatch):
    vit._reset_caches()
    monkeypatch.setattr(vit.config, "VITALITY_PROVIDER_KEY", OLD)
    monkeypatch.setattr(vit.config, "VITALITY_PLUS_PROVIDER_KEY", PLUS)
    yield
    vit._reset_caches()


def _fake(monkeypatch, answers):
    calls = []

    async def fake_get(path, company_key, params=None, provider_key=None):
        calls.append(provider_key)
        answer = answers[provider_key]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(vit, "_get", fake_get)
    return calls


async def test_plus_key_used_when_the_old_key_is_forbidden(keys, monkeypatch):
    calls = _fake(monkeypatch, {OLD: vit.VitalityUnavailable("HTTP 403"), PLUS: OK})
    rows = await vit._fetch_pages("/v2/latest-vehicle-status", "co", 200)
    assert rows == OK["data"] and calls == [OLD, PLUS]
    calls.clear()
    await vit._fetch_pages("/v2/latest-vehicle-status", "co", 200)
    assert calls == [PLUS]                      # remembered: no wasted request


async def test_plus_key_used_when_the_old_key_sees_an_empty_fleet(keys, monkeypatch):
    calls = _fake(monkeypatch, {OLD: {"description": "success", "data": None}, PLUS: OK})
    assert await vit._fetch_pages("/v2/latest-vehicle-status", "co", 200) == OK["data"]
    assert calls == [OLD, PLUS]


async def test_primary_key_still_first_for_carriers_that_did_not_move(keys, monkeypatch):
    calls = _fake(monkeypatch, {OLD: OK, PLUS: vit.VitalityUnavailable("HTTP 403")})
    await vit._fetch_pages("/v2/latest-vehicle-status", "co", 200)
    assert calls == [OLD]


async def test_both_keys_refused_is_unavailable(keys, monkeypatch):
    _fake(monkeypatch, {OLD: vit.VitalityUnavailable("HTTP 403"),
                        PLUS: vit.VitalityUnavailable("HTTP 403")})
    with pytest.raises(vit.VitalityUnavailable):
        await vit._fetch_pages("/v2/latest-vehicle-status", "co", 200)


async def test_without_a_plus_key_only_the_primary_is_used(keys, monkeypatch):
    monkeypatch.setattr(vit.config, "VITALITY_PLUS_PROVIDER_KEY", "")
    calls = _fake(monkeypatch, {OLD: vit.VitalityUnavailable("HTTP 403")})
    with pytest.raises(vit.VitalityUnavailable):
        await vit._fetch_pages("/v2/latest-vehicle-status", "co", 200)
    assert calls == [OLD]
