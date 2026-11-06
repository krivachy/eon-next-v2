"""End-to-end tests for the REST consumption endpoint contract and gas units.

These drive the real ``EonNext`` client against a fake consumption endpoint
that mirrors the live API's behaviour:

* half-hourly data is the default when ``group_by`` is omitted;
* an explicit ``group_by=half_hour`` is rejected with HTTP 400
  (``{"group_by": ["half_hour is not a valid value for group_by"]}``);
* SMETS2 gas meters report consumption in m3, electricity in kWh.

The coordinator and historical backfill are then exercised on top of that
client so the whole path - fetch, unit conversion, aggregation and the
recorder-backed external-statistics import - is covered.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component

from custom_components.eon_next.backfill import EonNextBackfillManager
from custom_components.eon_next.const import (
    CONF_BACKFILL_CHUNK_DAYS,
    CONF_BACKFILL_DELAY_SECONDS,
    CONF_BACKFILL_ENABLED,
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
    GAS_VOLUME_CORRECTION,
)
from custom_components.eon_next.coordinator import EonNextCoordinator
from custom_components.eon_next.eonnext import (
    METER_TYPE_ELECTRIC,
    METER_TYPE_GAS,
    ElectricityMeter,
    EnergyAccount,
    EonNext,
    EonNextApiError,
    GasMeter,
    gas_m3_to_kwh,
)
from custom_components.eon_next.statistics import statistic_id_for_meter

# Default conversion: 1 m3 = 1.02264 x 38 / 3.6 kWh (~10.7945 kWh).
_KWH_PER_M3 = GAS_VOLUME_CORRECTION * 38.0 / 3.6

# A comfortably-past UTC midnight so imported hours never collide with "now".
_DAY = (datetime.now(tz=timezone.utc) - timedelta(days=4)).replace(
    hour=0, minute=0, second=0, microsecond=0
)

_ALLOWED_GROUP_BY = {"day", "week", "month", "quarter"}


@pytest.fixture(autouse=True)
def _quiet_sqlalchemy_engine_logs() -> Generator[None, None, None]:
    logger = logging.getLogger("sqlalchemy.engine")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


# --- Fake consumption endpoint -----------------------------------------------


class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def json(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._payload


class _ConsumptionEndpoint:
    """Session stand-in that emulates the Kraken REST consumption endpoint.

    ``half_hourly`` / ``daily`` map the fuel ("electricity" / "gas") to the
    raw entries the endpoint returns, in the meter's native unit.
    """

    def __init__(
        self,
        half_hourly: dict[str, list[dict[str, Any]]],
        daily: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.half_hourly = half_hourly
        self.daily = daily or {}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params=None, headers=None) -> _Response:
        del headers
        params = dict(params or {})
        self.requests.append((url, params))
        fuel = "gas" if "/gas-meter-points/" in url else "electricity"
        group_by = params.get("group_by")
        if group_by is None:
            return _Response(200, {"results": self.half_hourly.get(fuel, [])})
        if group_by not in _ALLOWED_GROUP_BY:
            return _Response(
                400, {"group_by": [f"{group_by} is not a valid value for group_by"]}
            )
        return _Response(200, {"results": self.daily.get(fuel, [])})


def _slot(start: datetime, consumption: float) -> dict[str, Any]:
    return {
        "interval_start": start.isoformat(),
        "interval_end": (start + timedelta(minutes=30)).isoformat(),
        "consumption": consumption,
    }


def _half_hours(start: datetime, values: list[float]) -> list[dict[str, Any]]:
    return [_slot(start + timedelta(minutes=30 * i), v) for i, v in enumerate(values)]


def _api(endpoint: _ConsumptionEndpoint, **gas_options: Any) -> EonNext:
    api = EonNext()
    now = api._EonNext__current_timestamp()  # type: ignore[attr-defined]
    api.auth = {
        "issued": now,
        "token": {"token": "jwt", "expires": now + 3600},
        "refresh": {"token": "refresh", "expires": now + 7200},
    }
    api._get_session = AsyncMock(return_value=endpoint)  # type: ignore[method-assign]
    for key, value in gas_options.items():
        setattr(api, key, value)
    return api


def _account(api: EonNext) -> tuple[EnergyAccount, ElectricityMeter, GasMeter]:
    account = EnergyAccount(api, "A-TEST0001")
    electric = ElectricityMeter(account, "e-id", "E1SERIAL", "mpan-1")
    gas = GasMeter(account, "g-id", "G4SERIAL", "mprn-1")
    for meter in (electric, gas):
        meter._update = AsyncMock()  # type: ignore[method-assign]
    gas.latest_reading = 100.0
    account.meters = [electric, gas]
    api.accounts = [account]
    return account, electric, gas


async def _ensure_recorder(hass: HomeAssistant) -> None:
    if "recorder" in hass.config.components:
        return
    recorder_helper.async_initialize_recorder(hass)
    with patch("homeassistant.components.recorder.ALLOW_IN_MEMORY_DB", True):
        assert await async_setup_component(
            hass,
            "recorder",
            {"recorder": {"db_url": "sqlite://", "commit_interval": 0}},
        )
    await hass.async_block_till_done()
    await hass.data[recorder_helper.DATA_RECORDER].db_connected


async def _read_hourly_changes(
    hass: HomeAssistant, statistic_id: str, *, expect: int
) -> list[float]:
    """Read per-hour deltas of an external statistic once the recorder settles."""
    from homeassistant.helpers.recorder import get_instance

    rows: list = []
    for _ in range(50):
        await hass.async_block_till_done()
        await get_instance(hass).async_block_till_done()
        data = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            _DAY - timedelta(days=2),
            _DAY + timedelta(days=3),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = sorted(data.get(statistic_id, []), key=lambda r: r["start"])
        if len(rows) >= expect:
            break
    sums = [float(r["sum"]) for r in rows]
    return [round(b - a, 3) for a, b in zip([0.0, *sums], sums)]


# --- Client: request parameters ----------------------------------------------


@pytest.mark.asyncio
async def test_half_hourly_request_succeeds_against_strict_endpoint() -> None:
    """The half-hourly request omits ``group_by`` so the endpoint accepts it."""
    endpoint = _ConsumptionEndpoint({"electricity": _half_hours(_DAY, [0.25])})
    api = _api(endpoint)

    result = await api.async_get_consumption(
        METER_TYPE_ELECTRIC, "mpan-1", "E1SERIAL", group_by="half_hour", page_size=100
    )

    assert result is not None
    assert [e["consumption"] for e in result["results"]] == [0.25]
    (_url, params), = endpoint.requests
    assert "group_by" not in params


@pytest.mark.asyncio
async def test_explicit_half_hour_group_by_is_what_the_endpoint_rejects() -> None:
    """Guard the emulator itself: the rejected value really is a 400."""
    endpoint = _ConsumptionEndpoint({})
    response = endpoint.get("…/consumption/", params={"group_by": "half_hour"})
    assert response.status == 400


@pytest.mark.asyncio
async def test_bad_request_is_an_api_error_not_no_data() -> None:
    """A 400 (e.g. a future parameter change) surfaces as EonNextApiError."""
    endpoint = _ConsumptionEndpoint({})
    api = _api(endpoint)
    with pytest.raises(EonNextApiError, match="status 400"):
        await api.async_get_consumption(
            METER_TYPE_ELECTRIC, "mpan-1", "E1SERIAL", group_by="fortnight"
        )


# --- Client: gas unit conversion ---------------------------------------------


def test_gas_m3_to_kwh_formula() -> None:
    """m3 x 1.02264 x calorific value / 3.6."""
    assert gas_m3_to_kwh(1.0) == pytest.approx(10.7945, abs=1e-4)
    assert gas_m3_to_kwh(2.5, 39.5) == pytest.approx(2.5 * 1.02264 * 39.5 / 3.6)
    assert gas_m3_to_kwh(0.0) == 0.0


@pytest.mark.asyncio
async def test_gas_consumption_converted_from_m3_by_default() -> None:
    """Default (SMETS2): gas m3 is converted to kWh; other fields are kept."""
    raw = _half_hours(_DAY, [0.5, 0.25])
    endpoint = _ConsumptionEndpoint({"gas": raw})
    api = _api(endpoint)

    result = await api.async_get_consumption(
        METER_TYPE_GAS, "mprn-1", "G4SERIAL", group_by="half_hour"
    )

    assert result is not None
    entries = result["results"]
    assert [e["consumption"] for e in entries] == pytest.approx(
        [0.5 * _KWH_PER_M3, 0.25 * _KWH_PER_M3], abs=1e-6
    )
    assert [e["interval_start"] for e in entries] == [e["interval_start"] for e in raw]
    assert [e["interval_end"] for e in entries] == [e["interval_end"] for e in raw]
    # The endpoint's payload is not mutated in place.
    assert raw[0]["consumption"] == 0.5


@pytest.mark.asyncio
async def test_gas_consumption_uses_configured_calorific_value() -> None:
    endpoint = _ConsumptionEndpoint({}, {"gas": [_slot(_DAY, 10.0)]})
    api = _api(endpoint, gas_calorific_value=39.2)

    result = await api.async_get_consumption(
        METER_TYPE_GAS, "mprn-1", "G4SERIAL", group_by="day"
    )

    assert result is not None
    assert result["results"][0]["consumption"] == pytest.approx(
        10.0 * 1.02264 * 39.2 / 3.6, abs=1e-6
    )


@pytest.mark.asyncio
async def test_gas_consumption_passed_through_when_reported_in_kwh() -> None:
    """SMETS1: the setting is off, so gas kWh values are left unchanged."""
    endpoint = _ConsumptionEndpoint({"gas": _half_hours(_DAY, [1.5, 2.0])})
    api = _api(endpoint, gas_consumption_in_m3=False, gas_calorific_value=40.0)

    result = await api.async_get_consumption(
        METER_TYPE_GAS, "mprn-1", "G4SERIAL", group_by="half_hour"
    )

    assert result is not None
    assert [e["consumption"] for e in result["results"]] == [1.5, 2.0]


@pytest.mark.asyncio
async def test_electricity_consumption_never_converted() -> None:
    endpoint = _ConsumptionEndpoint({"electricity": _half_hours(_DAY, [0.4])})
    api = _api(endpoint)  # gas_consumption_in_m3 defaults to True

    result = await api.async_get_consumption(
        METER_TYPE_ELECTRIC, "mpan-1", "E1SERIAL", group_by="half_hour"
    )

    assert result is not None
    assert result["results"][0]["consumption"] == 0.4


@pytest.mark.asyncio
async def test_gas_entries_without_numeric_consumption_left_for_validation() -> None:
    """Null / malformed values are not converted (downstream skips them)."""
    entries = [
        {"interval_start": _DAY.isoformat(), "consumption": None},
        {"interval_start": _DAY.isoformat(), "consumption": "n/a"},
        {"interval_start": _DAY.isoformat()},
        {"interval_start": _DAY.isoformat(), "consumption": "2"},
    ]
    endpoint = _ConsumptionEndpoint({"gas": entries})
    api = _api(endpoint)

    result = await api.async_get_consumption(
        METER_TYPE_GAS, "mprn-1", "G4SERIAL", group_by="half_hour"
    )

    assert result is not None
    out = result["results"]
    assert out[0]["consumption"] is None
    assert out[1]["consumption"] == "n/a"
    assert "consumption" not in out[2]
    assert out[3]["consumption"] == pytest.approx(2 * _KWH_PER_M3, abs=1e-6)


def test_latest_gas_reading_kwh_uses_configured_calorific_value() -> None:
    api = EonNext()
    _account_, _electric, gas = _account(api)
    assert gas.get_latest_reading_kwh(100.0) == round(100.0 * _KWH_PER_M3, 3)

    api.gas_calorific_value = 39.0
    assert gas.get_latest_reading_kwh(100.0) == round(
        100.0 * 1.02264 * 39.0 / 3.6, 3
    )


# --- Coordinator ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_uses_half_hourly_data_from_strict_endpoint() -> None:
    """With group_by omitted the coordinator gets ``half_hour`` granularity."""
    endpoint = _ConsumptionEndpoint(
        {"electricity": _half_hours(_DAY, [0.1, 0.2])},
        {"electricity": [_slot(_DAY, 9.9)]},
    )
    api = _api(endpoint)
    _account_, electric, _gas = _account(api)
    coordinator = EonNextCoordinator.__new__(EonNextCoordinator)
    coordinator.api = api

    entries, granularity = await coordinator._fetch_consumption(electric)

    assert granularity == "half_hour"
    assert entries is not None
    assert [e["consumption"] for e in entries] == [0.1, 0.2]
    # No daily fallback request was needed.
    assert len(endpoint.requests) == 1


@pytest.mark.asyncio
async def test_coordinator_update_imports_kwh_statistics_for_both_fuels(
    hass: HomeAssistant,
) -> None:
    """Full update: half-hourly data reaches the external statistics in kWh."""
    await _ensure_recorder(hass)
    endpoint = _ConsumptionEndpoint(
        {
            # Two hours of half-hourly data per fuel.
            "electricity": _half_hours(_DAY, [0.25, 0.25, 0.5, 0.5]),
            "gas": _half_hours(_DAY, [0.1, 0.2, 0.3, 0.4]),  # m3
        }
    )
    api = _api(endpoint)
    _account_, electric, gas = _account(api)
    api.async_get_account_balances = AsyncMock(return_value={})  # type: ignore[method-assign]
    api.async_get_tariff_data = AsyncMock(return_value={})  # type: ignore[method-assign]
    coordinator = EonNextCoordinator(hass, api)

    data = await coordinator._async_update_data()

    assert all("group_by" not in params for _url, params in endpoint.requests)
    gas_data = data[gas.serial]
    assert [e["consumption"] for e in gas_data["consumption"]] == pytest.approx(
        [v * _KWH_PER_M3 for v in (0.1, 0.2, 0.3, 0.4)], abs=1e-6
    )
    assert gas_data["latest_reading_kwh"] == round(100.0 * _KWH_PER_M3, 3)

    electric_id = statistic_id_for_meter(electric.serial, METER_TYPE_ELECTRIC)
    gas_id = statistic_id_for_meter(gas.serial, METER_TYPE_GAS)
    assert electric_id == "eon_next:electricity_e1serial_consumption"
    assert gas_id == "eon_next:gas_g4serial_consumption"

    assert await _read_hourly_changes(hass, electric_id, expect=2) == [0.5, 1.0]
    assert await _read_hourly_changes(hass, gas_id, expect=2) == pytest.approx(
        [round(0.3 * _KWH_PER_M3, 3), round(0.7 * _KWH_PER_M3, 3)], abs=2e-3
    )


@pytest.mark.asyncio
async def test_coordinator_gas_totals_and_cost_are_in_kwh() -> None:
    """Daily / previous-day totals and the previous-day cost use kWh."""
    from homeassistant.util import dt as dt_util

    now = dt_util.now()
    yesterday = dt_util.start_of_local_day(now - timedelta(days=1))
    # 48 half-hours yesterday at 0.1 m3 each = 4.8 m3.
    endpoint = _ConsumptionEndpoint({"gas": _half_hours(yesterday, [0.1] * 48)})
    api = _api(endpoint)
    _account_, _electric, gas = _account(api)
    api.accounts[0].meters = [gas]
    api.async_get_account_balances = AsyncMock(return_value={})  # type: ignore[method-assign]
    api.async_get_tariff_data = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "mprn-1": {
                "tariff_name": "Flat gas",
                "unit_rate": 6.0,  # pence/kWh
                "standing_charge": 30.0,  # pence/day
            }
        }
    )
    coordinator = EonNextCoordinator.__new__(EonNextCoordinator)
    coordinator.api = api
    coordinator.data = {}
    coordinator._cost_warning_logged = set()
    coordinator.hass = SimpleNamespace()

    with patch(
        "custom_components.eon_next.coordinator.async_import_consumption_statistics",
        AsyncMock(),
    ) as import_mock:
        data = await coordinator._async_update_data()

    expected_kwh = 4.8 * _KWH_PER_M3
    meter_data = data[gas.serial]
    assert meter_data["previous_day_consumption"] == pytest.approx(
        expected_kwh, abs=1e-3
    )
    assert meter_data["previous_day_consumption_data_complete"] is True
    assert meter_data["previous_day_cost"] == pytest.approx(
        expected_kwh * 0.06 + 0.30, abs=1e-3
    )
    import_mock.assert_awaited_once()
    _hass, serial, meter_type, entries = import_mock.await_args.args
    assert (serial, meter_type) == (gas.serial, METER_TYPE_GAS)
    assert sum(e["consumption"] for e in entries) == pytest.approx(
        expected_kwh, abs=1e-3
    )


# --- Historical backfill -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_m3", "expected_daily_kwh"),
    [(True, round(12.0 * _KWH_PER_M3, 3)), (False, 12.0)],
)
async def test_backfill_imports_gas_history_in_kwh(
    hass: HomeAssistant, in_m3: bool, expected_daily_kwh: float
) -> None:
    """Backfill uses the same client call, so its gas history is kWh too."""
    await _ensure_recorder(hass)
    endpoint = _ConsumptionEndpoint({}, {"gas": [_slot(_DAY, 12.0)]})
    api = _api(endpoint, gas_consumption_in_m3=in_m3)
    _account_, _electric, gas = _account(api)
    gas.serial = f"G4BACKFILL{int(in_m3)}"  # isolate recorder state per case
    api.accounts[0].meters = [gas]
    entry = SimpleNamespace(
        entry_id=f"backfill-{in_m3}",
        options={
            CONF_BACKFILL_ENABLED: True,
            CONF_BACKFILL_REBUILD_STATISTICS: False,
            CONF_BACKFILL_CHUNK_DAYS: 1,
            CONF_BACKFILL_REQUESTS_PER_RUN: 1,
            CONF_BACKFILL_DELAY_SECONDS: 0,
        },
        async_create_background_task=Mock(),
    )
    manager = EonNextBackfillManager(hass, entry, api, SimpleNamespace())
    manager._save_state = AsyncMock()  # type: ignore[method-assign]
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {gas.serial: {"next_start": _DAY.date().isoformat(), "done": False}},
    }

    await manager._run_backfill_cycle()

    (_url, params), = endpoint.requests
    assert params["group_by"] == "day"
    gas_id = statistic_id_for_meter(gas.serial, METER_TYPE_GAS)
    assert gas_id is not None
    changes = await _read_hourly_changes(hass, gas_id, expect=1)
    assert sum(changes) == pytest.approx(expected_daily_kwh, abs=1e-3)
