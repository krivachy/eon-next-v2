"""Constants for the Eon Next integration."""

DOMAIN = "eon_next"
INTEGRATION_VERSION = "2.1.1"  # x-release-please-version

# Config entry schema version.  v1 stored email/password; v2 stores an API key.
CONFIG_ENTRY_VERSION = 2

# Authentication
CONF_API_KEY = "api_key"
CONF_REFRESH_TOKEN = "refresh_token"
# Legacy v1 credentials.  E.ON no longer accepts password logins; these keys
# are only read to detect pre-2.0 entries and are removed on re-auth.
CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# Frontend / dashboard
CONF_SHOW_PANEL = "show_panel"
CONF_SHOW_CARD = "show_card"
DEFAULT_SHOW_PANEL = True
DEFAULT_SHOW_CARD = True
PANEL_TITLE = "EON Next"
PANEL_ICON = "mdi:lightning-bolt"
PANEL_URL = f"/api/{DOMAIN}/panel"
CARDS_URL = f"/{DOMAIN}/cards"

# Backfill
CONF_BACKFILL_ENABLED = "backfill_enabled"
CONF_BACKFILL_LOOKBACK_DAYS = "backfill_lookback_days"
CONF_BACKFILL_CHUNK_DAYS = "backfill_chunk_days"
CONF_BACKFILL_REQUESTS_PER_RUN = "backfill_requests_per_run"
CONF_BACKFILL_RUN_INTERVAL_MINUTES = "backfill_run_interval_minutes"
CONF_BACKFILL_DELAY_SECONDS = "backfill_delay_seconds"
CONF_BACKFILL_REBUILD_STATISTICS = "backfill_rebuild_statistics"

# Gas units.  SMETS2 gas meters report consumption in m3 on the REST
# consumption endpoint; SMETS1 meters report kWh.  There is no reliable
# signal to tell them apart, so the user chooses (default: m3).
CONF_GAS_CONSUMPTION_IN_M3 = "gas_consumption_in_m3"
CONF_GAS_CALORIFIC_VALUE = "gas_calorific_value"
PLATFORMS = ["sensor", "binary_sensor", "event"]
DEFAULT_UPDATE_INTERVAL_MINUTES = 30
DEFAULT_BACKFILL_ENABLED = False
DEFAULT_BACKFILL_LOOKBACK_DAYS = 3650
DEFAULT_BACKFILL_CHUNK_DAYS = 1
DEFAULT_BACKFILL_REQUESTS_PER_RUN = 1
DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES = 180
DEFAULT_BACKFILL_DELAY_SECONDS = 300
DEFAULT_BACKFILL_REBUILD_STATISTICS = True
API_BASE_URL = "https://api.eonnext-kraken.energy/v1"
# Gas volume -> energy: kWh = m3 x volume correction x calorific value (MJ/m3)
# / 3.6 (MJ per kWh).  The calorific value is a fixed approximation; the
# real per-period value varies slightly and is shown on the gas bill.
GAS_CALORIC_VALUE = 38.0
GAS_VOLUME_CORRECTION = 1.02264
GAS_MJ_PER_KWH = 3.6
DEFAULT_GAS_CONSUMPTION_IN_M3 = True
DEFAULT_GAS_CALORIFIC_VALUE = GAS_CALORIC_VALUE
MIN_GAS_CALORIFIC_VALUE = 30.0
MAX_GAS_CALORIFIC_VALUE = 45.0
