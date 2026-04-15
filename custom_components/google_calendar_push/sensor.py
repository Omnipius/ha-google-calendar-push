import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from .const import DOMAIN, SIGNAL_UPDATE_ENDPOINT

_LOGGER = logging.getLogger(__name__)

def get_calendar_names(token_data, calendar_ids):
    """Fetch the real names of the calendars from Google in a single optimized pass."""
    credentials = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
    )
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    names = {}
    
    try:
        # Fetch all calendars once O(1) instead of iterating
        calendars_result = service.calendarList().list().execute()
        items = calendars_result.get("items", [])
        
        # Create a fast lookup map
        cal_map = {item["id"]: item.get("summary", item["id"]) for item in items}
        
        for cid in calendar_ids:
            names[cid] = cal_map.get(cid, cid)
            
    except Exception as e:
        _LOGGER.error("Error fetching bulk calendar list: %s", e)
        # Fallback to pure IDs if the network fetch fails
        for cid in calendar_ids:
            names[cid] = cid
            
    return names

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Set up the sensors from the config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    session = data["session"]
    aliases = data["aliases"] # Format: { "alias": "calendar_id" }
    
    if not aliases:
        return

    # Ensure token is valid before hitting Google API
    if not session.valid_token:
        await session.async_ensure_token_valid()

    # Get human-readable names for the attributes
    names = await hass.async_add_executor_job(
        get_calendar_names, session.token, list(aliases.values())
    )

    entities = []
    for alias, cid in aliases.items():
        entities.append(
            GoogleCalendarEndpointSensor(alias, cid, names.get(cid, cid), entry.entry_id)
        )
        
    async_add_entities(entities)

class GoogleCalendarEndpointSensor(SensorEntity):
    """Representation of a Google Calendar Push Endpoint."""

    # Using standard HA icons
    _attr_icon = "mdi:calendar-arrow-right"
    _attr_has_entity_name = True

    def __init__(self, alias, calendar_id, calendar_name, entry_id):
        """Initialize the sensor."""
        self._alias = alias
        
        # Name the sensor based on the alias
        self._attr_name = f"Push Endpoint ({alias})"
        self._attr_unique_id = f"{entry_id}_{alias}"
        
        # Initial state
        self._attr_native_value = "listening"
        
        # Build the URL. Google uses standard formatting for web access.
        encoded_cid = calendar_id.replace("@", "%40")
        web_url = f"https://calendar.google.com/calendar/r?cid={encoded_cid}"

        # Initialize the attributes panel
        self._attr_extra_state_attributes = {
            "calendar_name": calendar_name,
            "endpoint_alias": alias,
            "calendar_id": calendar_id,
            "web_url": web_url,
            "last_operation": "None",
            "last_events_processed": 0,
            "last_received": "Never"
        }
        
    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        # Connect to the dispatcher signal from api.py
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, 
                f"{SIGNAL_UPDATE_ENDPOINT}_{self._alias}", 
                self._handle_push_update
            )
        )

    @callback
    def _handle_push_update(self, operation, count):
        """Handle a push notification update."""
        self._attr_native_value = "received_push"
        self._attr_extra_state_attributes["last_operation"] = operation
        self._attr_extra_state_attributes["last_events_processed"] = count
        self._attr_extra_state_attributes["last_received"] = dt_util.now().isoformat()
        
        # Tell HA to immediately update the dashboard
        self.async_write_ha_state()