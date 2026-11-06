"""Integration-style tests for config entry setup and sensor wiring."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
import datetime
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.eon_next as integration
from custom_components.eon_next.backfill import EonNextBackfillManager
from custom_components.eon_next.const import (
    CONF_BACKFILL_ENABLED,
    CONF_API_KEY,
    CONF_GAS_CALORIFIC_VALUE,
    CONF_GAS_CONSUMPTION_IN_M3,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.eon_next.coordinator import EonNextCoordinator
from custom_components.eon_next.eonnext import EonNextApiError, EonNextAuthError
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component


@pytest.fixture(autouse=True)
def _quiet_sqlalchemy_engine_logs() -> Generator[None, None, None]:
    """Keep recorder-backed tests from flooding output with SQL logs."""
    logger = logging.getLogger("sqlalchemy.engine")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


@dataclass(slots=True)
class FakeMeter:
    """Minimal meter object used by integration setup tests."""

    serial: str = "electric-meter-1"
    type: str = "electricity"
    supply_point_id: str = "mpxn-1"
    meter_id: str = "meter-id-1"
    latest_reading: float | None = None
    latest_reading_date: str | None = None

    async def _update(self) -> None:
        """Mirror async meter update API."""
        return None


@dataclass(slots=True)
class FakeAccount:
    """Minimal account shape expected by sensor setup."""

    meters: list[FakeMeter] = field(default_factory=list)
    ev_chargers: list[Any] = field(default_factory=list)


class FakeApi:
    """Minimal API client shape consumed by async_setup_entry."""

    def __init__(
        self,
        *,
        refresh_login_result: bool,
        api_key_login_result: bool = True,
    ) -> None:
        self.refresh_login_result = refresh_login_result
        self.api_key_login_result = api_key_login_result
        self.refresh_login_calls: list[str] = []
        self.api_key_login_calls: list[str] = []
        self.closed = False
        self.api_key: str | None = None
        self.gas_consumption_in_m3: bool | None = None
        self.gas_calorific_value: float | None = None
        self.accounts = [FakeAccount(meters=[FakeMeter()])]
        self._token_callback = None

    def set_token_update_callback(self, callback) -> None:
        """Capture callback registration from integration setup."""
        self._token_callback = callback

    async def login_with_refresh_token(self, token: str) -> bool:
        """Record refresh-token login usage."""
        self.refresh_login_calls.append(token)
        return self.refresh_login_result

    async def login_with_api_key(self, api_key: str) -> bool:
        """Record API key login fallback usage."""
        self.api_key_login_calls.append(api_key)
        return self.api_key_login_result

    async def async_close(self) -> None:
        """Track API close on unload."""
        self.closed = True


async def _fake_first_refresh(self: EonNextCoordinator) -> None:
    """Avoid network update during setup while marking coordinator healthy."""
    self.async_set_updated_data({})


async def _fake_backfill_run(self: EonNextBackfillManager) -> None:
    """Disable long-running backfill loop during tests."""
    return None


def _mock_entry(*, options: dict[str, Any] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Eon Next",
        data={
            CONF_API_KEY: "sk_live_test",
            CONF_REFRESH_TOKEN: "refresh-token",
        },
        version=2,
        options=options or {},
    )


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


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


def _status_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    registry = er.async_get(hass)
    expected_unique_id = f"{entry.entry_id}__historical_backfill_status"
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id == expected_unique_id:
            return registry_entry.entity_id
    raise AssertionError("Missing historical backfill status entity")


def _cost_tracker_entity_id(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    tracker_id: str,
) -> str:
    registry = er.async_get(hass)
    expected_unique_id = f"cost_tracker__{entry.entry_id}__{tracker_id}"
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id == expected_unique_id:
            return registry_entry.entity_id
    raise AssertionError(f"Missing cost tracker entity for {tracker_id}")


def _patch_integration(
    monkeypatch: pytest.MonkeyPatch,
    fake_api: FakeApi,
) -> None:
    monkeypatch.setattr(integration, "EonNext", lambda: fake_api)
    monkeypatch.setattr(
        EonNextCoordinator,
        "async_config_entry_first_refresh",
        _fake_first_refresh,
    )
    monkeypatch.setattr(EonNextBackfillManager, "_async_run", _fake_backfill_run)


@pytest.mark.asyncio
async def test_setup_uses_refresh_token_and_creates_status_sensor(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup should use refresh token first and register status sensor."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)

    assert fake_api.refresh_login_calls == ["refresh-token"]
    assert fake_api.api_key_login_calls == []
    assert entry.runtime_data.api is fake_api

    state = hass.states.get(_status_entity_id(hass, entry))
    assert state is not None
    assert state.state == "disabled"
    assert state.attributes["total_meters"] == 1
    assert state.attributes["pending_meters"] == 1


@pytest.mark.asyncio
async def test_setup_falls_back_to_api_key_when_refresh_fails(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup should fall back to the API key when the refresh token is invalid."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=False)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)

    assert fake_api.refresh_login_calls == ["refresh-token"]
    assert fake_api.api_key_login_calls == ["sk_live_test"]


@pytest.mark.asyncio
async def test_status_sensor_updates_when_backfill_state_changes(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status sensor should update when manager notifies listeners."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry(options={CONF_BACKFILL_ENABLED: True})

    await _setup_entry(hass, entry)
    entity_id = _status_entity_id(hass, entry)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "initializing"

    manager = entry.runtime_data.backfill
    manager._store.async_save = AsyncMock()  # noqa: SLF001 - mock store to avoid I/O
    manager._state = {  # noqa: SLF001 - set desired test state
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {
            "electric-meter-1": {
                "next_start": datetime.date.today().isoformat(),
                "done": True,
            }
        },
    }
    await manager._save_state()  # noqa: SLF001 - exercises save + notify codepath
    await hass.async_block_till_done()

    updated = hass.states.get(entity_id)
    assert updated is not None
    assert updated.state == "completed"
    assert updated.attributes["completed_meters"] == 1
    assert updated.attributes["pending_meters"] == 0


@pytest.mark.asyncio
async def test_unload_entry_closes_api_client(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unload should close API client and remove entities."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert fake_api.closed is True
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.asyncio
async def test_data_only_update_does_not_reload_entry(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisting a rotated refresh token must not reload the integration."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)
    runtime_data_before = entry.runtime_data

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        # Simulate the API client persisting a rotated refresh token, i.e.
        # a data-only entry update that fires the update listener.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_REFRESH_TOKEN: "rotated-token"},
        )
        await hass.async_block_till_done()

    mock_reload.assert_not_called()
    # Runtime data is untouched: no teardown/rebuild happened.
    assert entry.runtime_data is runtime_data_before
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-token"


@pytest.mark.asyncio
async def test_options_update_reloads_entry(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing options must still trigger a reload."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)

    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        hass.config_entries.async_update_entry(
            entry,
            options={CONF_BACKFILL_ENABLED: True},
        )
        await hass.async_block_till_done()

    mock_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected_in_m3", "expected_cv"),
    [
        ({}, True, 38.0),
        (
            {CONF_GAS_CONSUMPTION_IN_M3: False, CONF_GAS_CALORIFIC_VALUE: 39.3},
            False,
            39.3,
        ),
    ],
)
async def test_setup_applies_gas_unit_options_to_api(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    expected_in_m3: bool,
    expected_cv: float,
) -> None:
    """Gas unit options configure the API client before the first refresh."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry(options=options)

    await _setup_entry(hass, entry)

    assert fake_api.gas_consumption_in_m3 is expected_in_m3
    assert fake_api.gas_calorific_value == expected_cv


@pytest.mark.asyncio
async def test_add_cost_tracker_service_creates_tracker_entity(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a cost tracker via service should create a new sensor entity."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)
    entry.runtime_data.cost_trackers._store.async_save = AsyncMock()  # noqa: SLF001

    hass.states.async_set(
        "sensor.washer_energy",
        "0",
        {"unit_of_measurement": "kWh", "device_class": "energy"},
    )

    await hass.services.async_call(
        DOMAIN,
        "add_cost_tracker",
        {
            "name": "Washing Machine",
            "tracked_entity_id": "sensor.washer_energy",
            "meter_serial": "electric-meter-1",
            "enabled": True,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    tracker_entity = _cost_tracker_entity_id(hass, entry, "washing_machine")
    state = hass.states.get(tracker_entity)
    assert state is not None
    assert state.state == "0.0"
    assert state.attributes["tracked_entity"] == "sensor.washer_energy"
    assert state.attributes["meter_serial"] == "electric-meter-1"
    assert state.attributes["enabled"] is True


@pytest.mark.asyncio
async def test_reset_and_update_cost_tracker_services(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset/update services should mutate tracker manager state."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _setup_entry(hass, entry)
    entry.runtime_data.cost_trackers._store.async_save = AsyncMock()  # noqa: SLF001

    hass.states.async_set(
        "sensor.dryer_energy",
        "0",
        {"unit_of_measurement": "kWh", "device_class": "energy"},
    )

    await hass.services.async_call(
        DOMAIN,
        "add_cost_tracker",
        {
            "name": "Dryer",
            "tracked_entity_id": "sensor.dryer_energy",
            "meter_serial": "electric-meter-1",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    manager = entry.runtime_data.cost_trackers
    tracker_id = "dryer"
    tracker_entity = _cost_tracker_entity_id(hass, entry, tracker_id)

    tracker_state = manager.get_state(tracker_id)
    assert tracker_state is not None
    tracker_state.today_cost = 2.5
    tracker_state.today_consumption_kwh = 7.25

    await hass.services.async_call(
        DOMAIN,
        "reset_cost_tracker",
        {"entity_id": tracker_entity},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert tracker_state.today_cost == 0.0
    assert tracker_state.today_consumption_kwh == 0.0

    await hass.services.async_call(
        DOMAIN,
        "update_cost_tracker",
        {"entity_id": tracker_entity, "enabled": False},
        blocking=True,
    )
    await hass.async_block_till_done()
    tracker_config = manager.get_config(tracker_id)
    assert tracker_config is not None
    assert tracker_config.enabled is False


class FakeApiWithApiError(FakeApi):
    """FakeApi variant that raises EonNextApiError on login calls."""

    def __init__(
        self,
        *,
        refresh_raises: bool = False,
        api_key_raises: bool = False,
    ) -> None:
        super().__init__(refresh_login_result=False)
        self._refresh_raises = refresh_raises
        self._api_key_raises = api_key_raises

    async def login_with_refresh_token(self, token: str) -> bool:
        self.refresh_login_calls.append(token)
        if self._refresh_raises:
            raise EonNextApiError("API unreachable")
        return self.refresh_login_result

    async def login_with_api_key(self, api_key: str) -> bool:
        self.api_key_login_calls.append(api_key)
        if self._api_key_raises:
            raise EonNextApiError("API unreachable")
        return self.api_key_login_result


@pytest.mark.asyncio
async def test_setup_raises_config_entry_not_ready_on_refresh_api_error(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EonNextApiError during refresh-token login should result in ConfigEntryNotReady."""
    del enable_custom_integrations
    fake_api = FakeApiWithApiError(refresh_raises=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_api.closed is True
    assert fake_api.refresh_login_calls == ["refresh-token"]
    # API key fallback should NOT have been attempted.
    assert fake_api.api_key_login_calls == []


@pytest.mark.asyncio
async def test_setup_raises_config_entry_not_ready_on_api_key_api_error(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EonNextApiError during API key login should result in ConfigEntryNotReady."""
    del enable_custom_integrations
    fake_api = FakeApiWithApiError(refresh_raises=False, api_key_raises=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_api.closed is True
    # Refresh token login returned False, so the API key fallback was attempted.
    assert fake_api.refresh_login_calls == ["refresh-token"]
    assert fake_api.api_key_login_calls == ["sk_live_test"]


class FakeApiWithAuthError(FakeApi):
    """FakeApi variant that raises EonNextAuthError on a login call."""

    def __init__(
        self,
        *,
        refresh_raises: bool = False,
        api_key_raises: bool = False,
    ) -> None:
        super().__init__(refresh_login_result=False, api_key_login_result=False)
        self._refresh_raises = refresh_raises
        self._api_key_raises = api_key_raises

    async def login_with_refresh_token(self, token: str) -> bool:
        self.refresh_login_calls.append(token)
        if self._refresh_raises:
            raise EonNextAuthError("token rejected")
        return self.refresh_login_result

    async def login_with_api_key(self, api_key: str) -> bool:
        self.api_key_login_calls.append(api_key)
        if self._api_key_raises:
            raise EonNextAuthError("API key rejected")
        return self.api_key_login_result


@pytest.mark.asyncio
async def test_setup_maps_auth_error_to_config_entry_auth_failed(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An EonNextAuthError during setup should trigger re-auth, not a bare error."""
    del enable_custom_integrations
    fake_api = FakeApiWithAuthError(api_key_raises=True)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # ConfigEntryAuthFailed => SETUP_ERROR and a re-auth flow is started.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_api.closed is True


@pytest.mark.asyncio
async def test_setup_auth_failed_when_api_key_login_returns_false(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both logins failing (no exception) is an auth failure, not a retry."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=False, api_key_login_result=False)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert fake_api.closed is True


# --- v1 (email/password) -> v2 (API key) migration ---


@pytest.mark.asyncio
async def test_v1_password_entry_migrates_and_requests_reauth(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A v1 email/password entry is kept, bumped to v2 and sent to re-auth."""
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Eon Next",
        unique_id="user@example.com",
        version=1,
        data={
            "email": "user@example.com",
            "password": "secret-password",
            CONF_REFRESH_TOKEN: "refresh-token",
        },
    )
    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entity = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "electric-meter-1__daily_consumption",
        config_entry=entry,
    )
    with caplog.at_level(logging.DEBUG, logger="custom_components.eon_next"):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.version == 2
    # Not deleted and not crashed: waiting for the user to supply a key.
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    # No login is attempted with credentials E.ON no longer accepts.
    assert fake_api.refresh_login_calls == []
    assert fake_api.api_key_login_calls == []
    # The unusable email/password are dropped; the refresh token and the
    # legacy unique_id (which marks the entry as pre-2.0) are kept.
    assert entry.data == {CONF_REFRESH_TOKEN: "refresh-token"}
    assert entry.unique_id == "user@example.com"
    # Entity ids are untouched.
    assert registry.async_get(entity.entity_id) is not None
    assert registry.async_get(entity.entity_id).unique_id == (
        "electric-meter-1__daily_consumption"
    )
    assert "secret-password" not in caplog.text


@pytest.mark.asyncio
async def test_migration_refuses_future_version(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    _patch_integration(monkeypatch, fake_api)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_API_KEY: "sk_live_test", CONF_REFRESH_TOKEN: "rt"},
    )

    await _ensure_recorder(hass)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR


@pytest.mark.asyncio
async def test_setup_logs_no_secrets_at_debug(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=False)
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()

    with caplog.at_level(logging.DEBUG, logger="custom_components.eon_next"):
        await _setup_entry(hass, entry)

    assert fake_api.api_key_login_calls == ["sk_live_test"]
    assert "sk_live_test" not in caplog.text
    assert "refresh-token" not in caplog.text


@pytest.mark.asyncio
async def test_listener_reloads_when_reauth_stores_new_credentials(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-auth on a loaded entry is applied by the update listener.

    The client persisting its own rotated token must not reload; a key or
    refresh token from a separate (re-auth) login must.
    """
    del enable_custom_integrations
    fake_api = FakeApi(refresh_login_result=True)
    fake_api.auth = {"refresh": {"token": "refresh-token"}}  # type: ignore[attr-defined]
    _patch_integration(monkeypatch, fake_api)
    entry = _mock_entry()
    await _setup_entry(hass, entry)

    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload:
        # Client rotates and persists its own token: no reload.
        fake_api.auth["refresh"]["token"] = "rotated"  # type: ignore[attr-defined]
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: "rotated"}
        )
        await hass.async_block_till_done()
        reload.assert_not_called()

        # Re-auth with the same key stores a token from a separate login.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: "from-reauth"}
        )
        await hass.async_block_till_done()
        assert reload.await_count == 1

        # Re-auth with a new key.
        fake_api.auth["refresh"]["token"] = "from-reauth"  # type: ignore[attr-defined]
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_API_KEY: "sk_live_new"}
        )
        await hass.async_block_till_done()
        assert reload.await_count == 2
