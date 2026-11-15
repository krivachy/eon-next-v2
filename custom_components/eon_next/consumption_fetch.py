"""Shared half-hourly consumption fetch for the live poll and the backfill.

Both the coordinator's *reconcile window* and the historical backfill need
every half-hour in a dated window, however many pages the endpoint splits it
into.  They share this one implementation so paging, de-duplication and
ordering behave identically.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Half-hourly page size per day of window: 50 slots covers the 25-hour
# clocks-back day.  Capped so a long window does not ask for an unbounded page;
# anything beyond a page is fetched by paging.
SLOTS_PER_DAY = 50
MAX_PAGE_SIZE = 5000
MAX_PAGES = 100
HALF_HOUR = timedelta(minutes=30)


def utc_iso(value: datetime) -> str:
    """Format *value* as a UTC ISO 8601 timestamp the endpoint accepts."""
    return dt_util.as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_boundary(day: date) -> datetime:
    """Local midnight of *day* as a UTC datetime.

    Window boundaries must be true local-day edges: formatting a local date
    with a literal ``Z`` labels a local midnight as UTC, shifting the window by
    the local offset (an hour off during BST for the whole UK).  Going through
    ``start_of_local_day`` also makes clocks-change days 23 or 25 hours long.
    """
    return dt_util.as_utc(dt_util.start_of_local_day(day))


def utc_boundary_iso(day: date) -> str:
    """Local midnight of *day* as a UTC ISO 8601 timestamp."""
    return utc_iso(utc_boundary(day))


def page_size_for_days(day_count: int) -> int:
    """Page size sized for *day_count* local days of half-hourly slots."""
    return min(max(day_count, 1) * SLOTS_PER_DAY, MAX_PAGE_SIZE)


async def async_fetch_half_hourly(
    api: Any,
    meter: Any,
    period_from: datetime,
    period_to: datetime,
    day_count: int,
) -> list[dict[str, Any]]:
    """Fetch every half-hourly entry in ``[period_from, period_to)``.

    Requests one page sized for the window and, while the endpoint reports a
    ``next`` page, narrows the window past the entries already returned
    (whichever order the endpoint sorts them in) and asks again.  Entries are
    de-duplicated by ``interval_start`` and returned oldest first.  Raises
    ``EonNextApiError`` (or ``EonNextAuthError``) on any page failure so the
    caller can retry or fall back as a whole.
    """
    page_size = page_size_for_days(day_count)
    collected: dict[datetime, dict[str, Any]] = {}
    window_from, window_to = period_from, period_to

    for _ in range(MAX_PAGES):
        result = await api.async_get_consumption(
            meter.type,
            meter.supply_point_id,
            meter.serial,
            group_by="half_hour",
            page_size=page_size,
            period_from=utc_iso(window_from),
            period_to=utc_iso(window_to),
        )
        page = result.get("results") if result else None
        if not isinstance(page, list) or not page:
            break

        starts: list[datetime] = []
        added = 0
        for entry in page:
            if not isinstance(entry, dict):
                continue
            parsed = dt_util.parse_datetime(str(entry.get("interval_start", "")))
            if parsed is None:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt_util.UTC)
            parsed = dt_util.as_utc(parsed)
            if not period_from <= parsed < period_to:
                continue
            starts.append(parsed)
            if parsed not in collected:
                collected[parsed] = entry
                added += 1

        if not result or not result.get("next") or not starts or not added:
            break
        if starts[0] > starts[-1]:
            # Newest first: the next page is older than this one.
            window_to = min(starts)
        else:
            window_from = max(starts) + HALF_HOUR
        if window_from >= window_to:
            break
    else:
        _LOGGER.warning(
            "Half-hourly fetch for meter %s stopped paging after %d pages",
            meter.serial,
            MAX_PAGES,
        )

    return [collected[key] for key in sorted(collected)]
