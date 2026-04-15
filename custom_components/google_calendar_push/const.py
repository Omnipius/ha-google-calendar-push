from homeassistant.const import Platform

DOMAIN = "google_calendar_push"
CONF_CALENDARS = "configured_calendars"
CONF_CALENDAR_ALIASES = "calendar_aliases"

# Signal used to communicate between the REST API and the Sensors
SIGNAL_UPDATE_ENDPOINT = f"{DOMAIN}_update_endpoint"

# Tell HA to load our sensor file
PLATFORMS = [Platform.SENSOR]

OAUTH2_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/userinfo.email"
]