"""Config-flow tests: API-key login, duplicate abort, and re-auth.

These cover the most security-sensitive paths (initial login and re-auth),
which previously had no test coverage (spec 07, finding 7.1).
"""

from __future__ import annotations

from collections.abc import Generator
import logging
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component

from custom_components.eon_next.const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.eon_next.eonnext import EonNextApiError, EonNextAuthError


@pytest.fixture(autouse=True)
def _quiet_sqlalchemy_engine_logs() -> Generator[None, None, None]:
    """Keep recorder-backed flow tests from flooding output with SQL logs."""
    logger = logging.getLogger("sqlalchemy.engine")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


@pytest.fixture(autouse=True)
def _mock_entry_lifecycle() -> Generator[None, None, None]:
    """These tests exercise the config flow itself, not entry setup.

    Creating (or re-auth reloading) an entry would otherwise run the real
    ``async_setup_entry``, which performs a refresh-token login and opens a
    real socket - rejected by the test harness. Mock the entry lifecycle so
    the flow assertions run without touching the network.
    """
    with (
        patch("custom_components.eon_next.async_setup_entry", return_value=True),
        patch("custom_components.eon_next.async_unload_entry", return_value=True),
    ):
        yield


API_KEY = "sk_live_test_key_123"
ACCOUNT = "A-1234ABCD"


class _FakeFlowApi:
    """Stand-in for EonNext used inside the config flow's key check."""

    def __init__(
        self,
        *,
        success: bool = True,
        token: str = "new-refresh-token",
        accounts: list[str] | None = None,
        raises: Exception | None = None,
        accounts_raise: Exception | None = None,
    ) -> None:
        self._success = success
        self._raises = raises
        self._accounts_raise = accounts_raise
        self._accounts = [ACCOUNT] if accounts is None else accounts
        self.auth: dict[str, Any] = {"refresh": {"token": token}}
        self.keys_tried: list[str] = []
        self.closed = False

    async def login_with_api_key(
        self, api_key: str, initialise: bool = False
    ) -> bool:
        del initialise
        self.keys_tried.append(api_key)
        if self._raises is not None:
            raise self._raises
        return self._success

    async def async_get_account_numbers(self) -> list[str]:
        if self._accounts_raise is not None:
            raise self._accounts_raise
        return self._accounts

    async def async_close(self) -> None:
        self.closed = True


def _patch_api(fake: _FakeFlowApi):
    return patch(
        "custom_components.eon_next.config_flow.EonNext", return_value=fake
    )


async def _ensure_recorder(hass: HomeAssistant) -> None:
    """The eon_next integration depends on recorder; a flow init loads it."""
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


async def _start_user_flow(hass: HomeAssistant, fake: _FakeFlowApi, key: str):
    with _patch_api(fake):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        assert list(result["data_schema"].schema) == [CONF_API_KEY]
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: key}
        )


@pytest.mark.asyncio
async def test_user_flow_creates_entry(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A valid key creates an entry keyed by account number."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    fake = _FakeFlowApi(token="rt-1", accounts=["A-2", ACCOUNT])
    result = await _start_user_flow(hass, fake, f"  {API_KEY} ")

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: API_KEY, CONF_REFRESH_TOKEN: "rt-1"}
    assert result["result"].unique_id == "A-1234ABCD"
    assert result["result"].version == 2
    assert fake.keys_tried == [API_KEY]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_user_flow_invalid_key(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A rejected key keeps the form open with invalid_auth."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    result = await _start_user_flow(hass, _FakeFlowApi(success=False), "sk_live_bad")

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_user_flow_key_rejected_on_account_fetch(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """An auth error fetching accounts is also invalid_auth."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    fake = _FakeFlowApi(accounts_raise=EonNextAuthError("nope"))
    result = await _start_user_flow(hass, fake, API_KEY)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.asyncio
async def test_user_flow_no_accounts(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    del enable_custom_integrations
    await _ensure_recorder(hass)
    result = await _start_user_flow(hass, _FakeFlowApi(accounts=[]), API_KEY)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_accounts"}


@pytest.mark.asyncio
async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport error surfaces as cannot_connect, and the key is not logged."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    fake = _FakeFlowApi(raises=EonNextApiError(f"boom {API_KEY}"))
    with caplog.at_level(logging.DEBUG, logger="custom_components.eon_next"):
        result = await _start_user_flow(hass, fake, API_KEY)

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
async def test_user_flow_duplicate_aborts(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Adding an already-configured account aborts on the unique_id."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        version=2,
        data={CONF_API_KEY: "sk_live_old", CONF_REFRESH_TOKEN: "rt"},
    ).add_to_hass(hass)

    result = await _start_user_flow(hass, _FakeFlowApi(), API_KEY)

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _no_reload(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't actually reload (which would run full setup) during the test."""
    monkeypatch.setattr(
        hass.config_entries, "async_schedule_reload", lambda *a, **k: None, raising=False
    )


async def _run_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, fake: _FakeFlowApi, user_input: dict
):
    with _patch_api(fake):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )


def _v2_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        version=2,
        data={CONF_API_KEY: "sk_live_old", CONF_REFRESH_TOKEN: "old-rt"},
    )


@pytest.mark.asyncio
async def test_reauth_with_new_key(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del enable_custom_integrations
    await _ensure_recorder(hass)
    entry = _v2_entry()
    entry.add_to_hass(hass)
    _no_reload(hass, monkeypatch)

    fake = _FakeFlowApi(token="fresh-rt")
    result = await _run_reauth(hass, entry, fake, {CONF_API_KEY: API_KEY})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert fake.keys_tried == [API_KEY]
    assert entry.data == {CONF_API_KEY: API_KEY, CONF_REFRESH_TOKEN: "fresh-rt"}
    assert entry.unique_id == ACCOUNT


@pytest.mark.asyncio
async def test_reauth_blank_field_reuses_existing_key(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del enable_custom_integrations
    await _ensure_recorder(hass)
    entry = _v2_entry()
    entry.add_to_hass(hass)
    _no_reload(hass, monkeypatch)

    fake = _FakeFlowApi(token="fresh-rt")
    result = await _run_reauth(hass, entry, fake, {})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert fake.keys_tried == ["sk_live_old"]
    assert entry.data[CONF_API_KEY] == "sk_live_old"
    assert entry.data[CONF_REFRESH_TOKEN] == "fresh-rt"


@pytest.mark.asyncio
async def test_reauth_invalid_key_keeps_form(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del enable_custom_integrations
    await _ensure_recorder(hass)
    entry = _v2_entry()
    entry.add_to_hass(hass)
    _no_reload(hass, monkeypatch)

    result = await _run_reauth(
        hass, entry, _FakeFlowApi(success=False), {CONF_API_KEY: "sk_live_bad"}
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_API_KEY] == "sk_live_old"


@pytest.mark.asyncio
async def test_reauth_rejects_different_account(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key for another account aborts instead of rebinding the entry."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    entry = _v2_entry()
    entry.add_to_hass(hass)
    _no_reload(hass, monkeypatch)

    result = await _run_reauth(
        hass, entry, _FakeFlowApi(accounts=["B-OTHER"]), {CONF_API_KEY: API_KEY}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_account_mismatch"
    assert entry.data[CONF_API_KEY] == "sk_live_old"
    assert entry.unique_id == ACCOUNT


def _legacy_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        version=2,  # already migrated: data still holds the v1 credentials
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "old",
            CONF_REFRESH_TOKEN: "old-rt",
        },
    )


@pytest.mark.asyncio
async def test_reauth_legacy_entry_requires_key_and_scrubs_password(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-2.0 entry swaps email/password for a key and re-keys its unique_id."""
    del enable_custom_integrations
    await _ensure_recorder(hass)
    entry = _legacy_entry()
    entry.add_to_hass(hass)
    _no_reload(hass, monkeypatch)

    fake = _FakeFlowApi(token="fresh-rt")
    with _patch_api(fake):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        # No stored key: blank input is not accepted by the schema.
        schema = result["data_schema"].schema
        assert [str(k) for k in schema] == [CONF_API_KEY]
        assert next(iter(schema)).__class__.__name__ == "Required"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: API_KEY}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_API_KEY: API_KEY, CONF_REFRESH_TOKEN: "fresh-rt"}
    assert entry.unique_id == ACCOUNT


@pytest.mark.asyncio
async def test_reauth_legacy_entry_aborts_if_account_already_configured(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del enable_custom_integrations
    await _ensure_recorder(hass)
    _v2_entry().add_to_hass(hass)
    entry = _legacy_entry()
    entry.add_to_hass(hass)
    _no_reload(hass, monkeypatch)

    result = await _run_reauth(hass, entry, _FakeFlowApi(), {CONF_API_KEY: API_KEY})

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_EMAIL] == "user@example.com"
    assert entry.unique_id == "user@example.com"
