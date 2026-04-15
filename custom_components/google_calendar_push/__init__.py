import logging
import re
import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2TokenRequestReauthError

from .const import DOMAIN, CONF_CALENDARS, CONF_CALENDAR_ALIASES, PLATFORMS
from .api import GoogleCalendarPushView

_LOGGER = logging.getLogger(__name__)

def slugify_fallback(name: str) -> str:
    slug = name.lower()
    return re.sub(r'[^a-z0-9_]+', '_', slug).strip('_')

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Google Calendar Push from a config entry."""
    
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    
    # --- ADDED: Proactively test the token on boot ---
    try:
        await session.async_ensure_token_valid()
    except OAuth2TokenRequestReauthError as err:
        # Triggers the "Reconfigure" UI in Home Assistant
        raise ConfigEntryAuthFailed(f"OAuth token is invalid or expired: {err}") from err
    except aiohttp.client_exceptions.ClientResponseError as err:
        if 400 <= err.status < 500:
            raise ConfigEntryAuthFailed(f"OAuth authorization error: {err}") from err
        raise ConfigEntryNotReady from err
    except Exception as err:
        raise ConfigEntryNotReady(f"Unknown error verifying token: {err}") from err
    # -------------------------------------------------

    calendar_aliases = entry.options.get(
        CONF_CALENDAR_ALIASES, entry.data.get(CONF_CALENDAR_ALIASES, {})
    )

    if not calendar_aliases and entry.options.get(CONF_CALENDARS):
        legacy_cals = entry.options.get(CONF_CALENDARS, [])
        calendar_aliases = {slugify_fallback(c): c for c in legacy_cals}

    # Store data for the sensor platform to access
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "session": session,
        "aliases": calendar_aliases
    }

    # Register the custom REST View
    hass.http.register_view(GoogleCalendarPushView(hass, session, calendar_aliases))
    
    # Forward the setup to sensor.py
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("Google Calendar Push API registered. Endpoints: /api/google_calendar_push/<alias>")

    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok