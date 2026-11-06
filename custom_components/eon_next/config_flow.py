"""Config flow to configure Eon Next."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_API_KEY,
    CONF_BACKFILL_CHUNK_DAYS,
    CONF_BACKFILL_DELAY_SECONDS,
    CONF_BACKFILL_ENABLED,
    CONF_BACKFILL_LOOKBACK_DAYS,
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
    CONF_BACKFILL_RUN_INTERVAL_MINUTES,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SHOW_CARD,
    CONF_SHOW_PANEL,
    CONFIG_ENTRY_VERSION,
    DEFAULT_BACKFILL_CHUNK_DAYS,
    DEFAULT_BACKFILL_DELAY_SECONDS,
    DEFAULT_BACKFILL_ENABLED,
    DEFAULT_BACKFILL_LOOKBACK_DAYS,
    DEFAULT_BACKFILL_REBUILD_STATISTICS,
    DEFAULT_BACKFILL_REQUESTS_PER_RUN,
    DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES,
    DEFAULT_SHOW_CARD,
    DEFAULT_SHOW_PANEL,
    DOMAIN,
)
from .eonnext import EonNext, EonNextApiError, EonNextAuthError
from .redact import redact_secrets

_LOGGER = logging.getLogger(__name__)

API_KEY_DOCS_URL = "https://github.com/krivachy/eon-next-v2/blob/main/API_KEY.md"

_API_KEY_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
)


class InvalidApiKey(Exception):
    """Raised when E.ON rejects the API key."""


class NoAccounts(Exception):
    """Raised when the API key authenticates but no accounts are visible."""


@dataclass(slots=True)
class _ValidatedKey:
    """Result of a successful API key validation."""

    refresh_token: str
    account_numbers: list[str]

    @property
    def primary_account(self) -> str:
        """Stable identifier for the entry: the lowest account number."""
        return sorted(self.account_numbers)[0]


async def _validate_api_key(api_key: str) -> _ValidatedKey:
    """Log in with ``api_key`` and fetch the accounts it can see.

    Raises ``InvalidApiKey`` when E.ON rejects the key, ``NoAccounts`` when no
    account is visible, and ``EonNextApiError`` on transport/API failures.
    """
    api = EonNext()
    try:
        try:
            logged_in = await api.login_with_api_key(api_key, initialise=False)
        except EonNextAuthError as err:
            # e.g. a malformed token payload: treat as a rejected key.
            raise InvalidApiKey from err
        if not logged_in:
            raise InvalidApiKey
        refresh_token = api.auth["refresh"]["token"]
        try:
            account_numbers = await api.async_get_account_numbers()
        except EonNextAuthError as err:
            raise InvalidApiKey from err
        if not account_numbers:
            raise NoAccounts
        return _ValidatedKey(
            refresh_token=refresh_token, account_numbers=account_numbers
        )
    finally:
        await api.async_close()


def _entry_title(account_number: str) -> str:
    """Entry title that tells several E.ON accounts apart."""
    return f"Eon Next ({account_number})"


def _is_legacy_unique_id(unique_id: str | None) -> bool:
    """Return True for pre-2.0 entries, which used the login email as unique_id."""
    return unique_id is None or "@" in unique_id


class EonNextConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Eon Next config flow."""

    VERSION = CONFIG_ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EonNextOptionsFlow:
        """Return options flow for this handler."""
        return EonNextOptionsFlow()

    async def _async_check_key(
        self, api_key: str, errors: dict[str, str]
    ) -> _ValidatedKey | None:
        """Validate ``api_key``, filling ``errors`` on failure."""
        try:
            return await _validate_api_key(api_key)
        except InvalidApiKey:
            errors["base"] = "invalid_auth"
        except NoAccounts:
            errors["base"] = "no_accounts"
        except EonNextApiError as err:
            _LOGGER.debug(
                "API/connection error while validating API key: %s",
                redact_secrets(err),
            )
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected error while validating API key")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Invoked when a user initiates a flow via the user interface."""
        errors: dict[str, str] = {}

        # A pre-2.0 entry (keyed by email) can't be matched to an account
        # number until it is re-authenticated.  Adding a second entry for the
        # same account would duplicate every meter entity, so send the user
        # to re-authenticate the existing entry instead.
        if any(
            _is_legacy_unique_id(entry.unique_id)
            for entry in self._async_current_entries(include_ignore=False)
        ):
            return self.async_abort(reason="legacy_entry_needs_reauth")

        if user_input is not None:
            api_key = str(user_input.get(CONF_API_KEY) or "").strip()
            if not api_key:
                errors["base"] = "invalid_auth"
            elif (validated := await self._async_check_key(api_key, errors)) is not None:
                await self.async_set_unique_id(validated.primary_account)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_entry_title(validated.primary_account),
                    data={
                        CONF_API_KEY: api_key,
                        CONF_REFRESH_TOKEN: validated.refresh_token,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): _API_KEY_SELECTOR}),  # pyright: ignore[reportArgumentType]
            errors=errors,
            description_placeholders={"api_key_url": API_KEY_DOCS_URL},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Handle initiation of re-auth flow."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ):
        """Ask for an API key; a blank field reuses the stored key."""
        errors: dict[str, str] = {}

        reauth_entry = self._get_reauth_entry()
        stored_key = str(reauth_entry.data.get(CONF_API_KEY) or "")

        if user_input is not None:
            api_key = str(user_input.get(CONF_API_KEY) or "").strip() or stored_key
            if not api_key:
                errors["base"] = "invalid_auth"
            elif (validated := await self._async_check_key(api_key, errors)) is not None:
                current_unique_id = reauth_entry.unique_id
                title = reauth_entry.title
                if _is_legacy_unique_id(current_unique_id):
                    # Pre-2.0 entry keyed by email: re-key it to the account
                    # number, unless another entry already owns that account.
                    new_unique_id = validated.primary_account
                    existing = await self.async_set_unique_id(new_unique_id)
                    if (
                        existing is not None
                        and existing.entry_id != reauth_entry.entry_id
                    ):
                        return self.async_abort(reason="already_configured")
                    title = _entry_title(new_unique_id)
                elif current_unique_id not in validated.account_numbers:
                    # Re-auth must stay on the same account: rebinding the
                    # entry to another account would leave its unique_id and
                    # meter-serial unique_ids pointing at the wrong data.
                    return self.async_abort(reason="reauth_account_mismatch")
                else:
                    new_unique_id = current_unique_id

                data = {
                    key: value
                    for key, value in reauth_entry.data.items()
                    if key not in (CONF_EMAIL, CONF_PASSWORD)
                }
                data[CONF_API_KEY] = api_key
                data[CONF_REFRESH_TOKEN] = validated.refresh_token
                if reauth_entry.update_listeners:
                    # Loaded entry: its update listener reloads it when the
                    # stored credentials no longer match the running client
                    # (HA deprecates the flow-side reload for such entries).
                    return self.async_update_and_abort(
                        reauth_entry,
                        unique_id=new_unique_id,
                        title=title,
                        data=data,
                        reason="reauth_successful",
                    )
                # Failed/not-loaded entry (no listener): reload it from here.
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    unique_id=new_unique_id,
                    title=title,
                    data=data,
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(  # pyright: ignore[reportArgumentType]
                {
                    (
                        vol.Optional(CONF_API_KEY)
                        if stored_key
                        else vol.Required(CONF_API_KEY)
                    ): _API_KEY_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders={"api_key_url": API_KEY_DOCS_URL},
        )


class EonNextOptionsFlow(config_entries.OptionsFlow):
    """Handle Eon Next options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage Eon Next options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(  # pyright: ignore[reportArgumentType]
                {
                    vol.Required(
                        CONF_SHOW_PANEL,
                        default=options.get(
                            CONF_SHOW_PANEL, DEFAULT_SHOW_PANEL
                        ),
                    ): bool,
                    vol.Required(
                        CONF_SHOW_CARD,
                        default=options.get(
                            CONF_SHOW_CARD, DEFAULT_SHOW_CARD
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BACKFILL_ENABLED,
                        default=options.get(
                            CONF_BACKFILL_ENABLED, DEFAULT_BACKFILL_ENABLED
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BACKFILL_REBUILD_STATISTICS,
                        default=options.get(
                            CONF_BACKFILL_REBUILD_STATISTICS,
                            DEFAULT_BACKFILL_REBUILD_STATISTICS,
                        ),
                    ): bool,
                    vol.Required(
                        CONF_BACKFILL_LOOKBACK_DAYS,
                        default=options.get(
                            CONF_BACKFILL_LOOKBACK_DAYS,
                            DEFAULT_BACKFILL_LOOKBACK_DAYS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=36500)),
                    vol.Required(
                        CONF_BACKFILL_CHUNK_DAYS,
                        default=options.get(
                            CONF_BACKFILL_CHUNK_DAYS,
                            DEFAULT_BACKFILL_CHUNK_DAYS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=31)),
                    vol.Required(
                        CONF_BACKFILL_REQUESTS_PER_RUN,
                        default=options.get(
                            CONF_BACKFILL_REQUESTS_PER_RUN,
                            DEFAULT_BACKFILL_REQUESTS_PER_RUN,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                    vol.Required(
                        CONF_BACKFILL_RUN_INTERVAL_MINUTES,
                        default=options.get(
                            CONF_BACKFILL_RUN_INTERVAL_MINUTES,
                            DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                    vol.Required(
                        CONF_BACKFILL_DELAY_SECONDS,
                        default=options.get(
                            CONF_BACKFILL_DELAY_SECONDS,
                            DEFAULT_BACKFILL_DELAY_SECONDS,
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3600)),
                }
            ),
        )
