"""Constants for the Renault component."""

from homeassistant.const import Platform

DOMAIN = "renault"

CONF_LOCALE = "locale"
CONF_KAMEREON_ACCOUNT_ID = "kamereon_account_id"

DEFAULT_SCAN_INTERVAL = 420  # 7 minutes

# If throttled time to pause the updates, in seconds
COOLING_UPDATES_SECONDS = 60 * 15  # 15 minutes

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SELECT,
    Platform.SENSOR,
]
