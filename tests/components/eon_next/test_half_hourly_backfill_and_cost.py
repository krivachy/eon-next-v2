"""Half-hourly historical backfill and the GBP cost statistic.

* Backfill fetches half-hourly data (paging through the endpoint) and splices
  it per hour, so a day the live import only partly covered is filled
  completely, without double counting.
* ``eon_next:<fuel>_<serial>_cost`` prices each half-hour at its own unit rate
  plus the daily standing charge in the first hour of each local day, for
  flat and time-of-use tariffs, in both the live and the backfill path.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.eon_next.backfill import EonNextBackfillManager
from custom_components.eon_next.const import (
    CONF_BACKFILL_CHUNK_DAYS,
    CONF_BACKFILL_DELAY_SECONDS,
    CONF_BACKFILL_ENABLED,
    CONF_BACKFILL_LOOKBACK_DAYS,
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
)
from custom_components.eon_next.statistics import (
    _group_cost_by_hour,
    _merge_and_recompute_series,
    async_import_consumption_statistics,
    async_import_cost_statistics,
    async_import_historical_statistics,
    cost_statistic_id_for_meter,
    statistic_id_for_meter,
)

_LONDON = ZoneInfo("Europe/London")

# Pure pricing tests need a known BST (summer) and GMT (winter) day; take them
# from last year so they are always in the past without hardcoding a year.
_LAST_YEAR = datetime.now(tz=timezone.utc).year - 1
# 1 June is BST, so the local day starts at 23:00 UTC the day before.
_BST_MIDNIGHT = datetime(_LAST_YEAR, 5, 31, 23, 0, tzinfo=timezone.utc)
_GMT_MIDNIGHT = datetime(_LAST_YEAR, 1, 15, 0, 0, tzinfo=timezone.utc)
# A cursor date guaranteed to be in the future (a "finished" backfill).
_FUTURE_ISO = (datetime.now(tz=timezone.utc) + timedelta(days=3650)).date().isoformat()

# A comfortably-past day so imported hours never collide with "now".
_DAY_OFFSET = 4

# Flat tariff: 25p/kWh, 50p/day standing charge (GBP, as on meter data).
_FLAT_TARIFF: dict[str, Any] = {
    "tariff_unit_rate": 0.25,
    "tariff_is_tou": False,
    "standing_charge": 0.50,
}


@pytest.fixture(autouse=True)
def _quiet_sqlalchemy_engine_logs() -> Generator[None, None, None]:
    logger = logging.getLogger("sqlalchemy.engine")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


@pytest.fixture
def london_tz() -> Generator[None, None, None]:
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_LONDON)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


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


def _slot(start: datetime, kwh: float) -> dict[str, Any]:
    return {
        "interval_start": start.isoformat(),
        "interval_end": (start + timedelta(minutes=30)).isoformat(),
        "consumption": kwh,
    }


def _local_day(day_offset: int = _DAY_OFFSET) -> tuple[datetime, datetime]:
    """UTC bounds of the local day *day_offset* days ago."""
    day = dt_util.now().date() - timedelta(days=day_offset)
    start = dt_util.as_utc(dt_util.start_of_local_day(day))
    end = dt_util.as_utc(dt_util.start_of_local_day(day + timedelta(days=1)))
    return start, end


def _day_slots(start: datetime, end: datetime, kwh: float) -> list[dict[str, Any]]:
    slots = []
    when = start
    while when < end:
        slots.append(_slot(when, kwh))
        when += timedelta(minutes=30)
    return slots


async def _read_rows(
    hass: HomeAssistant,
    statistic_id: str,
    start: datetime,
    end: datetime,
    *,
    expect: int,
) -> list[tuple[datetime, float]]:
    """Read ``(hour, per-hour change)`` rows once the recorder has settled."""
    from homeassistant.helpers.recorder import get_instance

    rows: list[Any] = []
    for _ in range(50):
        await hass.async_block_till_done()
        await get_instance(hass).async_block_till_done()
        data = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start - timedelta(days=2),
            end + timedelta(days=2),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = sorted(data.get(statistic_id, []), key=lambda r: r["start"])
        if len(rows) >= expect:
            break
    out: list[tuple[datetime, float]] = []
    prev = 0.0
    for row in rows:
        cumulative = float(row["sum"])
        out.append((dt_util.utc_from_timestamp(row["start"]), cumulative - prev))
        prev = cumulative
    return out


class _PagingEndpoint:
    """Fake consumption API that honours the period window and page size.

    Returns at most ``page_size`` entries (``max_page`` caps it further, like a
    server-side limit) newest first, with ``next`` set when more remain.
    """

    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        max_page: int | None = None,
        newest_first: bool = True,
    ) -> None:
        self.entries = entries
        self.max_page = max_page
        self.newest_first = newest_first
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        meter_type: str,
        supply_point_id: str,
        serial: str,
        group_by: str = "day",
        page_size: int = 10,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        del meter_type, supply_point_id, serial
        self.calls.append(
            {
                "group_by": group_by,
                "page_size": page_size,
                "period_from": period_from,
                "period_to": period_to,
            }
        )
        if group_by != "half_hour":
            return {"results": []}
        lower = dt_util.parse_datetime(period_from) if period_from else None
        upper = dt_util.parse_datetime(period_to) if period_to else None
        matching = []
        for entry in self.entries:
            start = dt_util.parse_datetime(entry["interval_start"])
            assert start is not None
            if lower is not None and start < lower:
                continue
            if upper is not None and start >= upper:
                continue
            matching.append((start, entry))
        matching.sort(key=lambda item: item[0], reverse=self.newest_first)
        limit = min(page_size, self.max_page or page_size)
        page = [entry for _start, entry in matching[:limit]]
        return {
            "count": len(matching),
            "next": "https://example.invalid/next" if len(matching) > limit else None,
            "results": page,
        }


def _manager(
    hass: HomeAssistant,
    meter: SimpleNamespace,
    endpoint: _PagingEndpoint,
    start: datetime,
    *,
    coordinator_data: dict[str, Any] | None = None,
) -> EonNextBackfillManager:
    entry = SimpleNamespace(
        entry_id=f"hh-{meter.serial}",
        options={
            CONF_BACKFILL_ENABLED: True,
            CONF_BACKFILL_REBUILD_STATISTICS: False,
            CONF_BACKFILL_CHUNK_DAYS: 1,
            CONF_BACKFILL_REQUESTS_PER_RUN: 1,
            CONF_BACKFILL_DELAY_SECONDS: 0,
        },
        async_create_background_task=Mock(),
    )
    api = SimpleNamespace(
        accounts=[SimpleNamespace(meters=[meter])],
        async_get_consumption=endpoint,
    )
    coordinator = SimpleNamespace(data=coordinator_data or {})
    manager = EonNextBackfillManager(hass, entry, api, coordinator)
    manager._save_state = AsyncMock()  # type: ignore[method-assign]
    local_day = dt_util.as_local(start).date()
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {meter.serial: {"next_start": local_day.isoformat(), "done": False}},
        "half_hourly": True,
    }
    return manager


def _meter(serial: str) -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial, type="electricity", supply_point_id=f"sp-{serial}"
    )


# --- Fix 3: half-hourly backfill -------------------------------------------


def test_merge_half_hourly_fills_partial_day_without_double_counting() -> None:
    """Two existing hourly rows are overwritten, the other 22 hours added."""
    day = _GMT_MIDNIGHT
    existing = [(day, 1.0), (day + timedelta(hours=1), 2.0)]  # 1.0 kWh each
    full_day = {day + timedelta(hours=h): 0.5 for h in range(24)}

    series = _merge_and_recompute_series(0.0, existing, full_day)

    assert len(series) == 24
    assert series[-1][1] == pytest.approx(12.0)  # not 13.0 / 14.0


@pytest.mark.asyncio
async def test_backfill_fills_day_partly_covered_by_live_import(
    hass: HomeAssistant,
) -> None:
    """A day with 2 live hourly rows is fully filled by the half-hourly backfill."""
    await _ensure_recorder(hass)
    start, end = _local_day()
    meter = _meter("HHPARTIAL")
    stat_id = statistic_id_for_meter(meter.serial, meter.type)
    assert stat_id is not None

    # Live import covered only the first two hours (4 half-hours, 1 kWh each).
    await async_import_consumption_statistics(
        hass,
        meter.serial,
        meter.type,
        [_slot(start + timedelta(minutes=30 * i), 1.0) for i in range(4)],
    )
    rows = await _read_rows(hass, stat_id, start, end, expect=2)
    assert [round(v, 3) for _h, v in rows] == [2.0, 2.0]

    # The API caps pages at 20 entries, so the day needs several pages.
    slots = _day_slots(start, end, 0.25)
    endpoint = _PagingEndpoint(slots, max_page=20)
    manager = _manager(hass, meter, endpoint, start)

    await manager._run_backfill_cycle()

    assert len(endpoint.calls) > 1  # paged
    assert all(call["group_by"] == "half_hour" for call in endpoint.calls)
    assert endpoint.calls[0]["period_from"] == start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert endpoint.calls[0]["period_to"] == end.strftime("%Y-%m-%dT%H:%M:%SZ")

    hours = int((end - start).total_seconds() // 3600)
    rows = await _read_rows(hass, stat_id, start, end, expect=hours)
    assert len(rows) == hours
    # Every hour is 2 x 0.25 kWh - the live rows were replaced, not added to.
    assert all(v == pytest.approx(0.5) for _h, v in rows)
    assert sum(v for _h, v in rows) == pytest.approx(len(slots) * 0.25)
    assert manager._state is not None
    assert manager._state["meters"][meter.serial]["next_start"] == (
        dt_util.as_local(end).date().isoformat()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("newest_first", [True, False])
async def test_fetch_half_hourly_pages_in_either_order(
    hass: HomeAssistant, newest_first: bool
) -> None:
    """Paging collects every slot once whichever order the API sorts in."""
    start, end = _local_day()
    slots = _day_slots(start, end, 0.1)
    endpoint = _PagingEndpoint(slots, max_page=7, newest_first=newest_first)
    meter = _meter("HHPAGES")
    manager = _manager(hass, meter, endpoint, start)

    entries = await manager._fetch_half_hourly(meter, start, end, 1)

    assert [e["interval_start"] for e in entries] == [
        s["interval_start"] for s in slots
    ]
    assert len(endpoint.calls) == -(-len(slots) // 7)


@pytest.mark.asyncio
async def test_backfill_cycle_imports_cost_with_coordinator_tariff(
    hass: HomeAssistant,
) -> None:
    """The backfill passes the meter's tariff so cost history is spliced too."""
    start, end = _local_day()
    meter = _meter("HHCOST")
    endpoint = _PagingEndpoint(_day_slots(start, end, 0.1))
    meter_data = {"type": "electricity", **_FLAT_TARIFF}
    manager = _manager(
        hass, meter, endpoint, start, coordinator_data={meter.serial: meter_data}
    )

    with patch(
        "custom_components.eon_next.backfill.async_import_historical_statistics",
        AsyncMock(),
    ) as import_mock:
        await manager._run_backfill_cycle()

    import_mock.assert_awaited_once()
    assert import_mock.await_args.kwargs["meter_data"] is meter_data
    assert import_mock.await_args.kwargs["daily_granularity"] is False


@pytest.mark.asyncio
async def test_completed_daily_backfill_reruns_once_at_half_hourly(
    hass: HomeAssistant,
) -> None:
    """Progress from a pre-2.1.1 (daily) backfill is re-seeded, not cleared."""
    meter = _meter("HHMIGRATE")
    manager = _manager(hass, meter, _PagingEndpoint([]), dt_util.utcnow())
    manager.entry.options[CONF_BACKFILL_LOOKBACK_DAYS] = 10
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {meter.serial: {"next_start": _FUTURE_ISO, "done": True}},
        "half_hourly": False,
    }

    await manager._initialize_or_reset_progress([meter])

    expected_start = (dt_util.now().date() - timedelta(days=9)).isoformat()
    assert manager._state["half_hourly"] is True
    assert manager._state["rebuild_done"] is True  # statistics are not cleared
    assert manager._state["meters"][meter.serial] == {
        "next_start": expected_start,
        "done": False,
    }

    # Only once.
    manager._state["meters"][meter.serial] = {"next_start": _FUTURE_ISO, "done": True}
    await manager._initialize_or_reset_progress([meter])
    assert manager._state["meters"][meter.serial]["done"] is True


@pytest.mark.asyncio
async def test_rerun_with_extended_lookback_still_requests_rebuild(
    hass: HomeAssistant,
) -> None:
    """Extending the window during the re-run keeps the 2.1.0 rebuild trigger."""
    meter = _meter("HHMIGRATE2")
    manager = _manager(hass, meter, _PagingEndpoint([]), dt_util.utcnow())
    manager.entry.options[CONF_BACKFILL_LOOKBACK_DAYS] = 11
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {meter.serial: {"next_start": _FUTURE_ISO, "done": True}},
        "half_hourly": False,
    }

    await manager._initialize_or_reset_progress([meter])

    assert manager._state["half_hourly"] is True
    assert manager._state["lookback_days"] == 11
    assert manager._state["rebuild_done"] is False


@pytest.mark.asyncio
async def test_rebuild_clears_cost_statistics_too(hass: HomeAssistant) -> None:
    """Clear-and-rebuild removes the cost statistic with the consumption one."""
    meter = _meter("HHCLEAR")
    manager = _manager(hass, meter, _PagingEndpoint([]), dt_util.utcnow())
    manager.entry.options[CONF_BACKFILL_REBUILD_STATISTICS] = True
    assert manager._state is not None
    manager._state["rebuild_done"] = False
    recorder = Mock()
    recorder.async_clear_statistics = Mock(
        side_effect=lambda _ids, on_done: on_done()
    )
    with patch(
        "custom_components.eon_next.backfill.get_instance", return_value=recorder
    ):
        await manager._clear_existing_statistics([meter])

    cleared = recorder.async_clear_statistics.call_args.args[0]
    assert cleared == [
        "eon_next:electricity_hhclear_consumption",
        "eon_next:electricity_hhclear_cost",
    ]


# --- Fix 4: cost statistics -------------------------------------------------


def test_cost_statistic_id() -> None:
    assert (
        cost_statistic_id_for_meter("E1-ABC", "electricity")
        == "eon_next:electricity_e1_abc_cost"
    )
    assert cost_statistic_id_for_meter("G4X", "gas") == "eon_next:gas_g4x_cost"
    assert cost_statistic_id_for_meter("X", "water") is None


@pytest.mark.usefixtures("london_tz")
def test_cost_flat_tariff_adds_standing_charge_at_local_midnight() -> None:
    """Flat: kWh x rate per hour, standing charge in the local day's first hour."""
    local_midnight = _BST_MIDNIGHT
    entries = [
        _slot(local_midnight, 1.0),
        _slot(local_midnight + timedelta(minutes=30), 1.0),
        _slot(local_midnight + timedelta(hours=5), 2.0),
    ]

    hourly = _group_cost_by_hour(entries, _FLAT_TARIFF)

    assert hourly == {
        local_midnight: pytest.approx(2.0 * 0.25 + 0.50),
        local_midnight + timedelta(hours=5): pytest.approx(2.0 * 0.25),
    }


@pytest.mark.usefixtures("london_tz")
def test_cost_standing_charge_creates_missing_midnight_hour() -> None:
    """A day whose midnight hour has no data still gets its standing charge once."""
    local_midnight = _GMT_MIDNIGHT
    entries = [_slot(local_midnight + timedelta(hours=h), 1.0) for h in (3, 4)]

    hourly = _group_cost_by_hour(entries, _FLAT_TARIFF)

    assert hourly[local_midnight] == pytest.approx(0.50)
    assert sum(hourly.values()) == pytest.approx(2 * 0.25 + 0.50)


@pytest.mark.usefixtures("london_tz")
def test_cost_time_of_use_prices_each_half_hour_at_its_rate() -> None:
    """ToU: each half-hour uses the schedule window covering it."""
    local_midnight = _GMT_MIDNIGHT
    off_peak_end = local_midnight + timedelta(hours=7)
    day_end = local_midnight + timedelta(days=1)
    meter_data = {
        "tariff_unit_rate": 0.20,  # schedule mean, must not be used
        "tariff_is_tou": True,
        "tariff_code": "E-2R-TEST",
        "standing_charge": 0.60,
        "tariff_rates_schedule": [
            {
                "value": 7.0,  # pence
                "validFrom": local_midnight.isoformat(),
                "validTo": off_peak_end.isoformat(),
            },
            {
                "value": 30.0,
                "validFrom": off_peak_end.isoformat(),
                "validTo": day_end.isoformat(),
            },
        ],
    }
    entries = [
        _slot(local_midnight + timedelta(hours=2), 4.0),  # off-peak
        _slot(local_midnight + timedelta(hours=6, minutes=30), 1.0),  # off-peak
        _slot(local_midnight + timedelta(hours=7), 1.0),  # peak
        _slot(local_midnight + timedelta(hours=18), 2.0),  # peak
    ]

    hourly = _group_cost_by_hour(entries, meter_data)

    assert hourly == {
        local_midnight: pytest.approx(0.60),
        local_midnight + timedelta(hours=2): pytest.approx(4.0 * 0.07),
        local_midnight + timedelta(hours=6): pytest.approx(1.0 * 0.07),
        local_midnight + timedelta(hours=7): pytest.approx(1.0 * 0.30),
        local_midnight + timedelta(hours=18): pytest.approx(2.0 * 0.30),
    }


def test_cost_without_unit_rate_is_empty() -> None:
    assert _group_cost_by_hour([_slot(datetime.now(timezone.utc), 1.0)], {}) == {}


def test_cost_daily_bucket_uses_flat_rate() -> None:
    """A daily bucket cannot be split by window, so it is priced at the mean."""
    day = _GMT_MIDNIGHT
    meter_data = {**_FLAT_TARIFF, "tariff_is_tou": True, "tariff_rates_schedule": []}

    hourly = _group_cost_by_hour(
        [_slot(day, 10.0)], meter_data, daily_granularity=True
    )

    assert hourly == {day: pytest.approx(10.0 * 0.25 + 0.50)}


@pytest.mark.asyncio
async def test_live_cost_import_metadata_and_sums(hass: HomeAssistant) -> None:
    """The live path writes a GBP, has_sum statistic for the Energy dashboard."""
    from homeassistant.components.recorder.statistics import (
        async_list_statistic_ids,
    )

    await _ensure_recorder(hass)
    start, end = _local_day(5)
    meter = _meter("LIVECOST")
    cost_id = cost_statistic_id_for_meter(meter.serial, meter.type)
    assert cost_id is not None
    entries = [_slot(start + timedelta(minutes=30 * i), 1.0) for i in range(4)]

    await async_import_cost_statistics(
        hass, meter.serial, meter.type, entries, _FLAT_TARIFF
    )
    # Re-running the same cycle must not add the standing charge again.
    await _read_rows(hass, cost_id, start, end, expect=2)
    await async_import_cost_statistics(
        hass, meter.serial, meter.type, entries, _FLAT_TARIFF
    )
    rows = await _read_rows(hass, cost_id, start, end, expect=2)

    assert [round(v, 4) for _h, v in rows] == [0.5 + 0.5, 0.5]
    listed = await async_list_statistic_ids(hass, {cost_id})
    assert listed[0]["statistics_unit_of_measurement"] == "GBP"
    assert listed[0]["has_sum"] is True
    assert listed[0]["source"] == "eon_next"


@pytest.mark.asyncio
async def test_backfill_cost_replaces_live_hours_without_double_counting(
    hass: HomeAssistant,
) -> None:
    """Backfilled cost overwrites live hours; one standing charge per day."""
    await _ensure_recorder(hass)
    start, end = _local_day(6)
    meter = _meter("SPLICECOST")
    cost_id = cost_statistic_id_for_meter(meter.serial, meter.type)
    assert cost_id is not None

    # Live import covered the first two hours (incl. the standing charge).
    await async_import_cost_statistics(
        hass,
        meter.serial,
        meter.type,
        [_slot(start + timedelta(minutes=30 * i), 1.0) for i in range(4)],
        _FLAT_TARIFF,
    )
    await _read_rows(hass, cost_id, start, end, expect=2)

    slots = _day_slots(start, end, 0.5)
    await async_import_historical_statistics(
        hass, meter.serial, meter.type, slots, meter_data=_FLAT_TARIFF
    )

    hours = int((end - start).total_seconds() // 3600)
    rows = await _read_rows(hass, cost_id, start, end, expect=hours)
    assert len(rows) == hours
    assert sum(v for _h, v in rows) == pytest.approx(
        len(slots) * 0.5 * 0.25 + 0.50, abs=1e-3
    )
    assert rows[0][1] == pytest.approx(1.0 * 0.25 + 0.50)
