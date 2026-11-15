"""Late-data reconcile window: dated fetch, reconcile instead of append, lock.

If E.ON publishes a day's half-hourly data late, the append-only live import
used to leave that day partial for good.  These tests cover the fix:

* the coordinator's dated, paged fetch (``reconcile_days``);
* ``async_reconcile_recent_statistics`` filling, revising and never erasing
  hours, for both the consumption and the cost statistic;
* the per-entry lock shared with the historical backfill;
* ``reconcile_days = 0`` keeping the v2.1.1 behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.components.recorder import statistics as recorder_statistics
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
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
    GAS_VOLUME_CORRECTION,
)
from custom_components.eon_next.consumption_fetch import utc_boundary
from custom_components.eon_next.coordinator import (
    EonNextCoordinator,
    slots_in_local_day,
)
from custom_components.eon_next.eonnext import EonNext, EonNextApiError
from custom_components.eon_next.statistics import (
    async_import_historical_statistics,
    async_reconcile_recent_statistics,
    cost_statistic_id_for_meter,
    statistic_id_for_meter,
)

_LONDON = ZoneInfo("Europe/London")
_RECONCILE_DAYS = 7
_KWH_PER_M3 = GAS_VOLUME_CORRECTION * 38.0 / 3.6


def _last_sunday(year: int, month: int) -> date:
    """UK clocks change on the last Sunday of March and of October."""
    day = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


# Last year's clock changes: always in the past, never hard-coded.
_LAST_YEAR = date.today().year - 1
_CLOCKS_BACK = _last_sunday(_LAST_YEAR, 10)  # 25-hour day, 50 slots
_CLOCKS_FORWARD = _last_sunday(_LAST_YEAR, 3)  # 23-hour day, 46 slots
_SUMMER_DAY = _CLOCKS_FORWARD + timedelta(days=30)  # an ordinary BST day

# Flat tariff: 25p/kWh, 50p/day standing charge (GBP, as on meter data).
_FLAT_TARIFF: dict[str, Any] = {
    "tariff_unit_rate": 0.25,
    "tariff_is_tou": False,
    "standing_charge": 0.50,
}
_SLOT_KWH = 0.25  # per half-hour => 0.5 kWh, GBP 0.125 per hour
_HOUR_KWH = 2 * _SLOT_KWH
_HOUR_GBP = _HOUR_KWH * 0.25
_STANDING = 0.5


@pytest.fixture(autouse=True)
def _quiet_sqlalchemy_engine_logs() -> Generator[None, None, None]:
    logger = logging.getLogger("sqlalchemy.engine")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


@pytest.fixture(autouse=True)
def london_tz() -> Generator[None, None, None]:
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(_LONDON)
    try:
        yield
    finally:
        dt_util.set_default_time_zone(previous)


async def _ensure_recorder(hass: HomeAssistant) -> None:
    # The ``hass`` fixture resets the default zone (US/Pacific); local-day
    # logic here is UK-specific.
    await hass.config.async_set_time_zone("Europe/London")
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


def _meter(serial: str) -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial,
        type="electricity",
        supply_point_id=f"sp-{serial}",
        is_export=False,
    )


def _slot(start: datetime, kwh: float) -> dict[str, Any]:
    return {
        "interval_start": start.isoformat(),
        "interval_end": (start + timedelta(minutes=30)).isoformat(),
        "consumption": kwh,
    }


def _day_bounds(days_ago: int) -> tuple[datetime, datetime]:
    """UTC bounds of the local day *days_ago* days ago."""
    day = dt_util.now().date() - timedelta(days=days_ago)
    return utc_boundary(day), utc_boundary(day + timedelta(days=1))


def _hours_in(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 3600)


def _slots(
    start: datetime, end: datetime, kwh: float = _SLOT_KWH, *, hours: set[int] | None = None
) -> list[dict[str, Any]]:
    """Half-hour slots in ``[start, end)``, optionally only some hour indexes."""
    out = []
    when = start
    while when < end:
        index = int((when - start).total_seconds() // 3600)
        if hours is None or index in hours:
            out.append(_slot(when, kwh))
        when += timedelta(minutes=30)
    return out


def _window_start() -> datetime:
    return utc_boundary(dt_util.now().date() - timedelta(days=_RECONCILE_DAYS))


async def _read_sums(hass: HomeAssistant, statistic_id: str) -> dict[datetime, float]:
    """Cumulative sum per stored hour, once the recorder has settled."""
    from homeassistant.helpers.recorder import get_instance

    await hass.async_block_till_done()
    await get_instance(hass).async_block_till_done()
    data = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - timedelta(days=3650),
        dt_util.utcnow() + timedelta(days=2),
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return {
        dt_util.utc_from_timestamp(row["start"]): float(row["sum"])
        for row in sorted(data.get(statistic_id, []), key=lambda r: r["start"])
    }


def _changes(sums: dict[datetime, float]) -> dict[datetime, float]:
    """Per-hour change from cumulative sums."""
    out: dict[datetime, float] = {}
    previous = 0.0
    for hour in sorted(sums):
        out[hour] = sums[hour] - previous
        previous = sums[hour]
    return out


def _ids(meter: SimpleNamespace) -> tuple[str, str]:
    stat_id = statistic_id_for_meter(meter.serial, meter.type)
    cost_id = cost_statistic_id_for_meter(meter.serial, meter.type)
    assert stat_id is not None and cost_id is not None
    return stat_id, cost_id


async def _seed(
    hass: HomeAssistant, meter: SimpleNamespace, entries: list[dict[str, Any]]
) -> None:
    """Store *entries* (consumption and cost) the way an earlier poll did."""
    await async_import_historical_statistics(
        hass, meter.serial, meter.type, entries, meter_data=_FLAT_TARIFF
    )


async def _reconcile(
    hass: HomeAssistant,
    meter: SimpleNamespace,
    entries: list[dict[str, Any]],
    *,
    window_start: datetime | None = None,
):
    return await async_reconcile_recent_statistics(
        hass,
        meter.serial,
        meter.type,
        entries,
        _FLAT_TARIFF,
        window_start=window_start or _window_start(),
    )


# --- Late day filled ---------------------------------------------------------


@pytest.mark.asyncio
async def test_late_day_is_filled_and_later_sums_shift_exactly(
    hass: HomeAssistant,
) -> None:
    """Day D has 3 of its hours; D+1 and D+2 are complete; E.ON now has all of D."""
    await _ensure_recorder(hass)
    meter = _meter("LATEDAY")
    stat_id, cost_id = _ids(meter)
    d_start, d_end = _day_bounds(4)
    d1_start, d1_end = _day_bounds(3)
    d2_start, d2_end = _day_bounds(2)
    hours = _hours_in(d_start, d_end)

    partial = {0, hours - 2, hours - 1}  # standing-charge hour and the last two
    stored_entries = (
        _slots(d_start, d_end, hours=partial)
        + _slots(d1_start, d1_end)
        + _slots(d2_start, d2_end)
    )
    await _seed(hass, meter, stored_entries)
    before = {sid: await _read_sums(hass, sid) for sid in (stat_id, cost_id)}
    assert sum(1 for h in before[stat_id] if d_start <= h < d_end) == 3

    full = _slots(d_start, d_end) + _slots(d1_start, d1_end) + _slots(d2_start, d2_end)
    result = await _reconcile(hass, meter, full)

    missing = hours - 3
    assert result.consumption_filled == missing
    assert result.cost_filled == missing
    assert result.filled == missing

    added = {stat_id: missing * _HOUR_KWH, cost_id: missing * _HOUR_GBP}
    for sid in (stat_id, cost_id):
        after = await _read_sums(hass, sid)
        d_rows = [h for h in after if d_start <= h < d_end]
        assert len(d_rows) == hours
        # Each hour's change is its own value; later hours are unchanged.
        old_changes, new_changes = _changes(before[sid]), _changes(after)
        for hour in old_changes:
            assert new_changes[hour] == pytest.approx(old_changes[hour], abs=1e-3)
        # Sums from D+1 onwards moved by exactly D's added total.
        for hour, old_sum in before[sid].items():
            if hour >= d1_start:
                assert after[hour] - old_sum == pytest.approx(added[sid], abs=1e-3)
        # And the whole series is monotonic.
        ordered = [after[h] for h in sorted(after)]
        assert ordered == sorted(ordered)

    # Correct kWh and GBP per filled hour (the standing-charge hour was stored).
    kwh_changes = _changes(await _read_sums(hass, stat_id))
    gbp_changes = _changes(await _read_sums(hass, cost_id))
    for hour in kwh_changes:
        if d_start <= hour < d_end:
            assert kwh_changes[hour] == pytest.approx(_HOUR_KWH, abs=1e-3)
            expected = _HOUR_GBP + (_STANDING if hour == d_start else 0.0)
            assert gbp_changes[hour] == pytest.approx(expected, abs=1e-3)


@pytest.mark.asyncio
async def test_out_of_order_hours_fill_the_day_on_next_poll(
    hass: HomeAssistant,
) -> None:
    """A day's last two hours arrive first; the rest on a later poll."""
    await _ensure_recorder(hass)
    meter = _meter("OUTORDER")
    stat_id, cost_id = _ids(meter)
    d_start, d_end = _day_bounds(3)
    hours = _hours_in(d_start, d_end)

    first_poll = _slots(d_start, d_end, hours={hours - 2, hours - 1})
    result = await _reconcile(hass, meter, first_poll)
    # Nothing stored yet: the two hours are new, not "late".
    assert result.filled == 0
    assert result.consumption_appended == 2

    second_poll = _slots(d_start, d_end)
    result = await _reconcile(hass, meter, second_poll)

    assert result.consumption_filled == hours - 2
    for sid, unit in ((stat_id, _HOUR_KWH), (cost_id, _HOUR_GBP)):
        sums = await _read_sums(hass, sid)
        assert len([h for h in sums if d_start <= h < d_end]) == hours
        standing = _STANDING if sid == cost_id else 0.0
        assert max(sums.values()) == pytest.approx(hours * unit + standing, abs=1e-3)


# --- Steady state, absent hours, revisions -----------------------------------


@pytest.mark.asyncio
async def test_steady_state_writes_only_genuinely_new_hours(
    hass: HomeAssistant,
) -> None:
    """When the response matches storage nothing in the window is rewritten."""
    await _ensure_recorder(hass)
    meter = _meter("STEADY")
    d_start, d_end = _day_bounds(3)
    d1_start, d1_end = _day_bounds(2)
    entries = _slots(d_start, d_end) + _slots(d1_start, d1_end)
    await _seed(hass, meter, entries)

    real = recorder_statistics.async_add_external_statistics
    with patch.object(
        recorder_statistics, "async_add_external_statistics", side_effect=real
    ) as add:
        result = await _reconcile(hass, meter, entries)
        assert result.filled == 0
        assert result.consumption_appended == result.cost_appended == 0
        add.assert_not_called()

        # One new hour arrives after the newest stored one.
        d2_start, _d2_end = _day_bounds(1)
        new_entries = entries + _slots(d2_start, d2_start + timedelta(hours=1))
        result = await _reconcile(hass, meter, new_entries)

    assert result.filled == 0
    assert result.consumption_appended == 1
    # Consumption and cost each wrote exactly that one new hour.
    assert add.call_count == 2
    for call in add.call_args_list:
        written = call.args[2]
        assert [row["start"] for row in written] == [d2_start]


@pytest.mark.asyncio
async def test_stored_hour_missing_from_response_is_left_untouched(
    hass: HomeAssistant,
) -> None:
    """A short page must never erase or zero a stored hour."""
    await _ensure_recorder(hass)
    meter = _meter("ABSENT")
    stat_id, cost_id = _ids(meter)
    d_start, d_end = _day_bounds(3)
    entries = _slots(d_start, d_end)
    await _seed(hass, meter, entries)
    before = {sid: await _read_sums(hass, sid) for sid in (stat_id, cost_id)}

    # Response lacks hour 5 entirely (and a later hour is revised, forcing a
    # splice that passes over the absent hour).
    short = [
        e
        for e in entries
        if not (d_start + timedelta(hours=5)
                <= dt_util.parse_datetime(e["interval_start"])  # type: ignore[operator]
                < d_start + timedelta(hours=6))
    ]
    revised_hour = d_start + timedelta(hours=10)
    for entry in short:
        if dt_util.parse_datetime(entry["interval_start"]) == revised_hour:
            entry["consumption"] = 1.0
    await _reconcile(hass, meter, short)

    for sid in (stat_id, cost_id):
        after = await _read_sums(hass, sid)
        old_changes, new_changes = _changes(before[sid]), _changes(after)
        assert set(after) == set(before[sid])
        absent = d_start + timedelta(hours=5)
        assert new_changes[absent] == pytest.approx(old_changes[absent], abs=1e-3)
        assert new_changes[absent] > 0


@pytest.mark.asyncio
async def test_revised_hour_is_rewritten_and_later_sums_move_by_the_difference(
    hass: HomeAssistant,
) -> None:
    """E.ON revises one hour's value: only that hour and later sums change."""
    await _ensure_recorder(hass)
    meter = _meter("REVISED")
    stat_id, cost_id = _ids(meter)
    d_start, d_end = _day_bounds(3)
    entries = _slots(d_start, d_end)
    await _seed(hass, meter, entries)
    before = {sid: await _read_sums(hass, sid) for sid in (stat_id, cost_id)}

    revised = d_start + timedelta(hours=8)
    for entry in entries:
        if dt_util.parse_datetime(entry["interval_start"]) == revised:
            entry["consumption"] = _SLOT_KWH + 1.0  # hour is now +1.0 kWh
    result = await _reconcile(hass, meter, entries)

    assert result.consumption_filled == result.cost_filled == 1
    deltas = {stat_id: 1.0, cost_id: 1.0 * 0.25}
    for sid in (stat_id, cost_id):
        after = await _read_sums(hass, sid)
        for hour, old_sum in before[sid].items():
            expected = deltas[sid] if hour >= revised else 0.0
            assert after[hour] - old_sum == pytest.approx(expected, abs=1e-3)


# --- Clocks change days ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("day", "slots", "hours"),
    [
        (_CLOCKS_BACK, 50, 25),
        (_CLOCKS_FORWARD, 46, 23),
    ],
)
async def test_clock_change_day_statistics(
    hass: HomeAssistant, day: date, slots: int, hours: int
) -> None:
    """25- and 23-hour local days: edges are local midnights, hours group right."""
    await _ensure_recorder(hass)
    meter = _meter(f"DST{day.month}")
    stat_id, cost_id = _ids(meter)
    start, end = utc_boundary(day), utc_boundary(day + timedelta(days=1))
    assert slots_in_local_day(day) == slots
    entries = _slots(start, end)
    assert len(entries) == slots

    result = await _reconcile(hass, meter, entries, window_start=start)
    assert result.filled == 0
    assert result.consumption_appended == result.cost_appended == hours

    for sid, unit in ((stat_id, _HOUR_KWH), (cost_id, _HOUR_GBP)):
        sums = await _read_sums(hass, sid)
        assert len(sums) == hours
        standing = _STANDING if sid == cost_id else 0.0
        assert max(sums.values()) == pytest.approx(hours * unit + standing, abs=1e-3)
    # The standing charge sits in the local-midnight hour.
    first_cost = _changes(await _read_sums(hass, cost_id))[start]
    assert first_cost == pytest.approx(_HOUR_GBP + _STANDING, abs=1e-3)

    # A second identical pass is a no-op (not dirty, nothing newer).
    again = await _reconcile(hass, meter, entries, window_start=start)
    assert again.filled == 0
    assert again.consumption_appended == again.cost_appended == 0


def test_slots_in_local_day_counts() -> None:
    assert slots_in_local_day(_SUMMER_DAY) == 48
    assert slots_in_local_day(_CLOCKS_BACK) == 50
    assert slots_in_local_day(_CLOCKS_FORWARD) == 46


def test_reconcile_window_edges_are_local_midnights() -> None:
    """Across the autumn clocks-back the window starts at a true local midnight."""
    # Two days after the clocks go back, noon-ish UTC.
    now = datetime.combine(
        _CLOCKS_BACK + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc
    ).replace(hour=12, minute=20, second=30)
    coordinator = EonNextCoordinator.__new__(EonNextCoordinator)
    coordinator.reconcile_days = 3

    with (
        patch.object(dt_util, "now", return_value=dt_util.as_local(now)),
        patch.object(dt_util, "utcnow", return_value=now),
    ):
        period_from, period_to = coordinator._reconcile_window()

    # The window starts the day before the change, still BST: local midnight is
    # 23:00 UTC the day before that.
    day_before = _CLOCKS_BACK - timedelta(days=1)
    assert period_from == datetime.combine(
        day_before - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    ).replace(hour=23)
    assert dt_util.as_local(period_from).hour == 0
    assert dt_util.as_local(period_from).date() == day_before
    # Now rounded up to the next half-hour.
    assert period_to == now.replace(minute=30, second=0)
    # The clocks-back day inside the window really has 50 slots.
    assert slots_in_local_day(_CLOCKS_BACK) == 50


def test_incomplete_days_lists_short_days_oldest_first() -> None:
    today = dt_util.now().date()
    window_start = utc_boundary(today - timedelta(days=4))

    def day_slots(days_ago: int, drop: int = 0) -> list[dict[str, Any]]:
        start, end = _day_bounds(days_ago)
        entries = _slots(start, end)
        return entries[: len(entries) - drop]

    entries = (
        # 4 days ago: missing entirely.  3 ago: complete.  2 ago: 47 of 48.
        day_slots(3)
        + day_slots(2, drop=1)
        + day_slots(1)
        # Today is excluded even though it is partial.
        + _slots(*_day_bounds(0))[:4]
    )
    result = EonNextCoordinator._incomplete_days(entries, window_start)

    assert result == [
        (today - timedelta(days=4)).isoformat(),
        (today - timedelta(days=2)).isoformat(),
    ]


# --- Dated, paged fetch ------------------------------------------------------


class _PagingEndpoint:
    """Fake consumption API honouring window, page size and a server page cap."""

    def __init__(
        self,
        entries: list[dict[str, Any]],
        *,
        max_page: int | None = None,
        newest_first: bool = True,
        fail_when_dated: bool = False,
    ) -> None:
        self.entries = entries
        self.max_page = max_page
        self.newest_first = newest_first
        self.fail_when_dated = fail_when_dated
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
        if self.fail_when_dated and period_from is not None:
            raise EonNextApiError("boom")
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
        return {
            "count": len(matching),
            "next": "https://example.invalid/next" if len(matching) > limit else None,
            "results": [entry for _start, entry in matching[:limit]],
        }


def _coordinator(api: Any, reconcile_days: int) -> EonNextCoordinator:
    coordinator = EonNextCoordinator.__new__(EonNextCoordinator)
    coordinator.api = api
    coordinator.reconcile_days = reconcile_days
    return coordinator


def _history(days: int = _RECONCILE_DAYS) -> list[dict[str, Any]]:
    """Complete half-hourly history for the last *days* local days plus today so far."""
    entries: list[dict[str, Any]] = []
    for days_ago in range(days, -1, -1):
        start, end = _day_bounds(days_ago)
        entries += _slots(start, min(end, dt_util.utcnow()))
    return entries


@pytest.mark.asyncio
async def test_dated_fetch_is_one_request_sized_for_the_window() -> None:
    endpoint = _PagingEndpoint(_history())
    coordinator = _coordinator(SimpleNamespace(async_get_consumption=endpoint), 7)

    entries, granularity, window_start = await coordinator._fetch_consumption_window(
        _meter("FETCH")
    )

    assert granularity == "half_hour"
    assert len(endpoint.calls) == 1  # one request per poll, as before
    call = endpoint.calls[0]
    assert call["page_size"] == 400  # (7 + 1) * 50
    expected_from = utc_boundary(dt_util.now().date() - timedelta(days=7))
    assert call["period_from"] == expected_from.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert window_start == expected_from
    assert entries is not None
    assert len(entries) == len(_history())
    starts = [e["interval_start"] for e in entries]
    assert starts == sorted(starts)  # oldest first


@pytest.mark.asyncio
@pytest.mark.parametrize("newest_first", [True, False])
async def test_dated_fetch_follows_next_pages_inside_the_window(
    newest_first: bool,
) -> None:
    history = _history()
    endpoint = _PagingEndpoint(history, max_page=len(history) // 2 + 10, newest_first=newest_first)
    coordinator = _coordinator(SimpleNamespace(async_get_consumption=endpoint), 7)

    entries, _granularity, _window = await coordinator._fetch_consumption_window(
        _meter("PAGES")
    )

    assert len(endpoint.calls) == 2
    assert entries is not None
    assert [e["interval_start"] for e in entries] == sorted(
        e["interval_start"] for e in history
    )


@pytest.mark.asyncio
async def test_dated_fetch_failure_falls_back_to_newest_100() -> None:
    history = _history()
    endpoint = _PagingEndpoint(history, fail_when_dated=True)
    coordinator = _coordinator(SimpleNamespace(async_get_consumption=endpoint), 7)

    entries, granularity, window_start = await coordinator._fetch_consumption_window(
        _meter("FALLBACK")
    )

    assert granularity == "half_hour"
    assert window_start is None  # newest-100 is never reconciled
    assert [c["page_size"] for c in endpoint.calls] == [400, 100]
    assert endpoint.calls[1]["period_from"] is None
    assert entries is not None and len(entries) == 100


@pytest.mark.asyncio
async def test_reconcile_days_zero_is_the_v211_fetch() -> None:
    endpoint = _PagingEndpoint(_history())
    coordinator = _coordinator(SimpleNamespace(async_get_consumption=endpoint), 0)

    entries, granularity, window_start = await coordinator._fetch_consumption_window(
        _meter("OFF")
    )

    assert granularity == "half_hour" and window_start is None
    assert endpoint.calls == [
        {"group_by": "half_hour", "page_size": 100, "period_from": None, "period_to": None}
    ]
    assert entries is not None and len(entries) == 100


@pytest.mark.asyncio
async def test_dated_fetch_converts_gas_to_kwh_before_reconciling() -> None:
    """Gas m3 is converted by the client, so reconcile compares kWh with kWh."""
    start, end = _day_bounds(2)
    raw = [_slot(s, 0.1) for s in [start + timedelta(minutes=30 * i) for i in range(4)]]

    class _Session:
        def get(self, url: str, params=None, headers=None):
            del url, params, headers

            class _Resp:
                status = 200

                async def __aenter__(self) -> "_Resp":
                    return self

                async def __aexit__(self, *_exc: Any) -> None:
                    return None

                async def json(self) -> Any:
                    return {"results": [dict(r) for r in raw]}

            return _Resp()

    api = EonNext()
    now = api._EonNext__current_timestamp()  # type: ignore[attr-defined]
    api.auth = {
        "issued": now,
        "token": {"token": "jwt", "expires": now + 3600},
        "refresh": {"token": "refresh", "expires": now + 7200},
    }
    api._get_session = AsyncMock(return_value=_Session())  # type: ignore[method-assign]
    gas = SimpleNamespace(type="gas", supply_point_id="mprn-1", serial="G4")
    coordinator = _coordinator(api, 7)

    entries, _granularity, _window = await coordinator._fetch_consumption_window(gas)

    assert entries is not None and end > start
    assert [e["consumption"] for e in entries] == pytest.approx(
        [0.1 * _KWH_PER_M3] * 4, abs=1e-6
    )


# --- Existing consumers cope with the larger, ascending list -----------------


def test_aggregations_handle_a_week_of_ascending_entries() -> None:
    history = _history()
    ascending = sorted(history, key=lambda e: e["interval_start"])
    newest_first = list(reversed(ascending))
    assert len(ascending) > 300  # far more than the old newest-100

    yesterday_start, yesterday_end = _day_bounds(1)
    expected_yesterday = _hours_in(yesterday_start, yesterday_end) * _HOUR_KWH
    today_expected = sum(
        e["consumption"]
        for e in ascending
        if dt_util.as_local(dt_util.parse_datetime(e["interval_start"])).date()  # type: ignore[arg-type]
        == dt_util.now().date()
    )

    for entries in (ascending, newest_first):
        yesterday = EonNextCoordinator._aggregate_yesterday_consumption_details(entries)
        assert yesterday["entry_count"] == _hours_in(yesterday_start, yesterday_end) * 2
        assert yesterday["total"] == pytest.approx(expected_yesterday, abs=1e-3)
        assert len(EonNextCoordinator._yesterday_entries(entries)) == yesterday["entry_count"]
        daily = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert daily["total"] == pytest.approx(today_expected, abs=1e-3)


# --- Coordinator update wiring -----------------------------------------------


def _update_coordinator(
    api_entries: list[dict[str, Any]], reconcile_days: int
) -> tuple[EonNextCoordinator, SimpleNamespace, _PagingEndpoint]:
    endpoint = _PagingEndpoint(api_entries)
    meter = SimpleNamespace(
        type="electricity",
        serial="UPD1",
        meter_id="m-1",
        supply_point_id="sp-UPD1",
        latest_reading=1.0,
        latest_reading_date=None,
        is_export=False,
        _update=AsyncMock(),
    )
    account = SimpleNamespace(
        account_number="A-1", balance=0, meters=[meter], ev_chargers=[]
    )
    api = SimpleNamespace(
        accounts=[account],
        async_get_consumption=endpoint,
        async_get_account_balances=AsyncMock(return_value={}),
        async_get_tariff_data=AsyncMock(
            return_value={
                "sp-UPD1": {
                    "tariff_name": "Flat",
                    "unit_rate": 25.0,  # pence/kWh
                    "standing_charge": 50.0,  # pence/day
                }
            }
        ),
    )
    coordinator = _coordinator(api, reconcile_days)
    coordinator.data = {}
    coordinator._cost_warning_logged = set()
    coordinator.hass = SimpleNamespace()
    return coordinator, meter, endpoint


@pytest.mark.asyncio
async def test_update_reconciles_instead_of_appending_and_reports_diagnostics() -> None:
    history = _history()
    # Drop two slots from 3 days ago: E.ON has not published them yet.
    late_start, _late_end = _day_bounds(3)
    history = [
        e
        for e in history
        if dt_util.parse_datetime(e["interval_start"])
        not in (late_start + timedelta(hours=3), late_start + timedelta(hours=3, minutes=30))
    ]
    coordinator, meter, endpoint = _update_coordinator(history, 7)

    with (
        patch(
            "custom_components.eon_next.coordinator.async_reconcile_recent_statistics",
            AsyncMock(return_value=SimpleNamespace(filled=3)),
        ) as reconcile,
        patch(
            "custom_components.eon_next.coordinator.async_import_consumption_statistics",
            AsyncMock(),
        ) as append_consumption,
        patch(
            "custom_components.eon_next.coordinator.async_import_cost_statistics",
            AsyncMock(),
        ) as append_cost,
    ):
        data = await coordinator._async_update_data()

    assert len(endpoint.calls) == 1  # one consumption request for the meter
    append_consumption.assert_not_awaited()
    append_cost.assert_not_awaited()
    reconcile.assert_awaited_once()
    args = reconcile.await_args
    assert args.args[1:3] == (meter.serial, meter.type)
    assert args.args[4]["tariff_unit_rate"] == pytest.approx(0.25)
    assert args.kwargs["window_start"] == utc_boundary(
        dt_util.now().date() - timedelta(days=7)
    )

    meter_data = data[meter.serial]
    assert meter_data["incomplete_days"] == [
        (dt_util.now().date() - timedelta(days=3)).isoformat()
    ]
    assert meter_data["last_late_fill"]["hours"] == 3
    # Previous-day aggregations work from the larger, ascending list.
    assert meter_data["previous_day_consumption_data_complete"] is True
    yesterday_start, yesterday_end = _day_bounds(1)
    assert meter_data["previous_day_consumption"] == pytest.approx(
        _hours_in(yesterday_start, yesterday_end) * _HOUR_KWH, abs=1e-3
    )
    assert meter_data["previous_day_cost"] == pytest.approx(
        _hours_in(yesterday_start, yesterday_end) * _HOUR_GBP + _STANDING, abs=1e-3
    )


@pytest.mark.asyncio
async def test_update_with_reconcile_off_appends_newest_100() -> None:
    coordinator, meter, endpoint = _update_coordinator(_history(), 0)

    with (
        patch(
            "custom_components.eon_next.coordinator.async_reconcile_recent_statistics",
            AsyncMock(),
        ) as reconcile,
        patch(
            "custom_components.eon_next.coordinator.async_import_consumption_statistics",
            AsyncMock(),
        ) as append_consumption,
        patch(
            "custom_components.eon_next.coordinator.async_import_cost_statistics",
            AsyncMock(),
        ) as append_cost,
    ):
        data = await coordinator._async_update_data()

    assert [c["page_size"] for c in endpoint.calls] == [100]
    reconcile.assert_not_awaited()
    append_consumption.assert_awaited_once()
    append_cost.assert_awaited_once()
    assert "incomplete_days" not in data[meter.serial]


@pytest.mark.asyncio
async def test_failed_fetch_writes_no_statistics() -> None:
    """If every fetch fails nothing is imported and nothing is reconciled."""
    coordinator, meter, _endpoint = _update_coordinator(_history(), 7)
    coordinator.api.async_get_consumption = AsyncMock(side_effect=EonNextApiError("x"))

    with (
        patch(
            "custom_components.eon_next.coordinator.async_reconcile_recent_statistics",
            AsyncMock(),
        ) as reconcile,
        patch(
            "custom_components.eon_next.coordinator.async_import_consumption_statistics",
            AsyncMock(),
        ) as append_consumption,
    ):
        data = await coordinator._async_update_data()

    reconcile.assert_not_awaited()
    append_consumption.assert_not_awaited()
    assert "consumption" not in data[meter.serial]
    assert "incomplete_days" not in data[meter.serial]


# --- Lock --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_and_reconcile_share_one_lock_and_never_overlap(
    hass: HomeAssistant,
) -> None:
    """A backfill chunk and a live reconcile are serialised by the same lock."""
    events: list[str] = []

    async def fake_reconcile(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        events.append("reconcile:start")
        await asyncio.sleep(0.05)
        events.append("reconcile:end")
        return SimpleNamespace(filled=0)

    async def fake_historical(*_args: Any, **_kwargs: Any) -> None:
        events.append("backfill:start")
        await asyncio.sleep(0.05)
        events.append("backfill:end")

    coordinator = _coordinator(SimpleNamespace(), 7)
    coordinator.hass = SimpleNamespace()
    meter = _meter("LOCK")
    day_start, day_end = _day_bounds(10)
    endpoint = _PagingEndpoint(_slots(day_start, day_end))
    entry = SimpleNamespace(
        entry_id="lock",
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
        accounts=[SimpleNamespace(meters=[meter])], async_get_consumption=endpoint
    )
    manager = EonNextBackfillManager(hass, entry, api, coordinator)
    manager._save_state = AsyncMock()  # type: ignore[method-assign]
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {
            meter.serial: {
                "next_start": dt_util.as_local(day_start).date().isoformat(),
                "done": False,
            }
        },
        "half_hourly": True,
    }

    with (
        patch(
            "custom_components.eon_next.coordinator.async_reconcile_recent_statistics",
            fake_reconcile,
        ),
        patch(
            "custom_components.eon_next.backfill.async_import_historical_statistics",
            fake_historical,
        ),
    ):
        await asyncio.gather(
            manager._run_backfill_cycle(),
            coordinator._reconcile_statistics(meter, _slots(day_start, day_end), None, day_start),
        )

    assert sorted(events) == sorted(
        ["backfill:start", "backfill:end", "reconcile:start", "reconcile:end"]
    )
    # Whichever ran first finished before the other began.
    assert events[0].split(":")[0] == events[1].split(":")[0]
    assert events[2].split(":")[0] == events[3].split(":")[0]


@pytest.mark.asyncio
async def test_concurrent_backfill_splice_and_reconcile_keep_sums_monotonic(
    hass: HomeAssistant,
) -> None:
    """Scheduled together, a backfill splice and a live reconcile do not regress sums."""
    await _ensure_recorder(hass)
    meter = _meter("MONO")
    stat_id, cost_id = _ids(meter)
    old_start, old_end = _day_bounds(20)  # backfill territory, outside the window
    d_start, d_end = _day_bounds(4)
    d1_start, d1_end = _day_bounds(3)
    hours = _hours_in(d_start, d_end)

    await _seed(
        hass,
        meter,
        _slots(d_start, d_end, hours={0, hours - 1}) + _slots(d1_start, d1_end),
    )

    lock = asyncio.Lock()
    coordinator = _coordinator(SimpleNamespace(), 7)
    coordinator._statistics_lock = lock  # type: ignore[attr-defined]
    coordinator.hass = hass

    async def backfill_chunk() -> None:
        async with lock:
            await async_import_historical_statistics(
                hass,
                meter.serial,
                meter.type,
                _slots(old_start, old_end),
                meter_data=_FLAT_TARIFF,
            )

    full = _slots(d_start, d_end) + _slots(d1_start, d1_end)
    await asyncio.gather(
        backfill_chunk(),
        coordinator._reconcile_statistics(meter, full, _FLAT_TARIFF, _window_start()),
    )

    old_hours = _hours_in(old_start, old_end)
    for sid, unit, standing_days in (
        (stat_id, _HOUR_KWH, 0),
        (cost_id, _HOUR_GBP, 3),  # old day, D and D+1 each carry one standing charge
    ):
        sums = await _read_sums(hass, sid)
        ordered = [sums[h] for h in sorted(sums)]
        assert ordered == sorted(ordered), "cumulative sums must never regress"
        total = (old_hours + 2 * hours) * unit + standing_days * (_STANDING if sid == cost_id else 0)
        assert ordered[-1] == pytest.approx(total, abs=1e-3)
        assert len(sums) == old_hours + 2 * hours


# --- Diagnostics sensor ------------------------------------------------------


def test_backfill_status_sensor_exposes_reconcile_diagnostics() -> None:
    from custom_components.eon_next.sensor import HistoricalBackfillStatusSensor

    status = {
        "state": "completed",
        "enabled": True,
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "total_meters": 1,
        "completed_meters": 1,
        "pending_meters": 0,
        "next_start_date": None,
        "meters_progress": {},
    }
    backfill = SimpleNamespace(
        get_status=lambda: status,
        entry=SimpleNamespace(entry_id="e1"),
    )
    missing_day = (date.today() - timedelta(days=5)).isoformat()
    last_fill = {"timestamp": dt_util.utcnow().isoformat(), "hours": 21}
    coordinator = SimpleNamespace(
        reconcile_days=7,
        data={
            "M1": {"incomplete_days": [missing_day], "last_late_fill": last_fill},
            "M2": {"incomplete_days": [], "last_late_fill": None},
            "acct": {"type": "account"},
        },
    )
    sensor = HistoricalBackfillStatusSensor.__new__(HistoricalBackfillStatusSensor)
    sensor.coordinator = coordinator  # type: ignore[attr-defined]
    sensor._backfill = backfill  # type: ignore[attr-defined]

    attrs = sensor.extra_state_attributes

    assert attrs["reconcile_days"] == 7
    assert attrs["reconcile"] == {
        "M1": {"incomplete_days": [missing_day], "last_late_fill": last_fill},
        "M2": {"incomplete_days": [], "last_late_fill": None},
    }
