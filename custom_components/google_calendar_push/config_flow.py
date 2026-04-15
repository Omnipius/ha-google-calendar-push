import logging
import re
from typing import Any
import voluptuous as vol

from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.config_entries import ConfigEntry, OptionsFlow, SOURCE_REAUTH
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SelectOptionDict,
)
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from .const import DOMAIN, CONF_CALENDARS, CONF_CALENDAR_ALIASES, OAUTH2_SCOPES

_LOGGER = logging.getLogger(__name__)

ALIAS_REGEX = re.compile(r"^[a-z0-9_]+$")

def get_calendars_from_google(token_data):
    """Fetch only editable calendars from Google."""
    credentials = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
    )
    
    service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    calendars_result = service.calendarList().list().execute()
    
    options = []
    for cal in calendars_result.get("items", []):
        if cal.get("accessRole") in ["writer", "owner"]:
            options.append(
                SelectOptionDict(
                    value=cal["id"], 
                    label=cal.get("summary", cal["id"])
                )
            )
    return options

def get_user_email(token_data):
    """Fetch user email for entry naming."""
    credentials = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
    )
    service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
    user_info = service.userinfo().get().execute()
    return user_info.get("email", "Google Calendar Push API")

def sanitize_alias(name: str) -> str:
    """Provide a safe default alias based on the calendar name."""
    slug = name.lower()
    slug = re.sub(r'[^a-z0-9_]+', '_', slug)
    return slug.strip('_')

class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Config flow to handle Google Calendar OAuth2 authentication."""
    DOMAIN = DOMAIN

    def __init__(self):
        super().__init__()
        self.selected_calendars = []
        self.calendar_names = {}
        self.account_email = "Google Calendar Push API"

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict:
        return {
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(OAUTH2_SCOPES),
        }

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm reauth dialog."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
            )
        
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict) -> FlowResult:
        """Create an entry or update existing upon returning from Google OAuth."""
        # Check if this is a Reauth flow instead of a fresh install
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )
            
        self.oauth_data = data
        return await self.async_step_calendars()

    async def async_step_calendars(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self.selected_calendars = user_input.get(CONF_CALENDARS, [])
            
            try:
                self.account_email = await self.hass.async_add_executor_job(
                    get_user_email, self.oauth_data["token"]
                )
            except Exception:
                pass

            if not self.selected_calendars:
                # If no calendars selected, skip alias mapping
                return self.async_create_entry(
                    title=self.account_email, 
                    data=self.oauth_data,
                    options={CONF_CALENDARS: [], CONF_CALENDAR_ALIASES: {}}
                )
            return await self.async_step_aliases()

        try:
            calendar_options = await self.hass.async_add_executor_job(
                get_calendars_from_google, self.oauth_data["token"]
            )
            # Store names to generate clean default aliases later
            self.calendar_names = {opt["value"]: opt["label"] for opt in calendar_options}
        except Exception as e:
            _LOGGER.error("Error fetching calendars: %s", e)
            return self.async_abort(reason="calendar_fetch_failed")

        return self.async_show_form(
            step_id="calendars",
            data_schema=vol.Schema({
                vol.Optional(CONF_CALENDARS, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=calendar_options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            })
        )

    async def async_step_aliases(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Dynamically ask for an alias for each selected calendar."""
        errors = {}

        if user_input is not None:
            aliases = list(user_input.values())
            
            # Check for duplicates
            if len(aliases) != len(set(aliases)):
                errors["base"] = "duplicate_alias"
            else:
                # Check regex rules
                valid = all(ALIAS_REGEX.match(alias) for alias in aliases)
                if not valid:
                    errors["base"] = "invalid_alias"
                else:
                    # Validated! Flip the dict to { "alias": "calendar_id" } for the API endpoint
                    alias_mapping = {alias: cal_id for cal_id, alias in user_input.items()}
                    return self.async_create_entry(
                        title=self.account_email, 
                        data=self.oauth_data,
                        options={
                            CONF_CALENDARS: self.selected_calendars,
                            CONF_CALENDAR_ALIASES: alias_mapping
                        }
                    )

        # Build dynamic schema based on selected calendars
        schema = {}
        for cal_id in self.selected_calendars:
            name = self.calendar_names.get(cal_id, cal_id.split("@")[0])
            default_alias = sanitize_alias(name)
            schema[vol.Required(cal_id, default=default_alias)] = str

        return self.async_show_form(
            step_id="aliases",
            data_schema=vol.Schema(schema),
            errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Handle options flow."""

    def __init__(self):
        self.selected_calendars = []
        self.calendar_names = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self.selected_calendars = user_input.get(CONF_CALENDARS, [])
            if not self.selected_calendars:
                return self.async_create_entry(title="", data={
                    CONF_CALENDARS: [], 
                    CONF_CALENDAR_ALIASES: {}
                })
            return await self.async_step_aliases()

        try:
            implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
                self.hass, self.config_entry
            )
            session = config_entry_oauth2_flow.OAuth2Session(self.hass, self.config_entry, implementation)
            await session.async_ensure_token_valid()
            
            calendar_options = await self.hass.async_add_executor_job(
                get_calendars_from_google, session.token
            )
            self.calendar_names = {opt["value"]: opt["label"] for opt in calendar_options}
        except Exception as e:
            _LOGGER.error("Options flow error: %s", e)
            return self.async_abort(reason="calendar_fetch_failed")

        current_calendars = self.config_entry.options.get(
            CONF_CALENDARS, self.config_entry.data.get(CONF_CALENDARS, [])
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_CALENDARS, default=current_calendars): SelectSelector(
                    SelectSelectorConfig(
                        options=calendar_options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            })
        )

    async def async_step_aliases(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors = {}

        if user_input is not None:
            aliases = list(user_input.values())
            if len(aliases) != len(set(aliases)):
                errors["base"] = "duplicate_alias"
            else:
                valid = all(ALIAS_REGEX.match(alias) for alias in aliases)
                if not valid:
                    errors["base"] = "invalid_alias"
                else:
                    alias_mapping = {alias: cal_id for cal_id, alias in user_input.items()}
                    return self.async_create_entry(title="", data={
                        CONF_CALENDARS: self.selected_calendars,
                        CONF_CALENDAR_ALIASES: alias_mapping
                    })

        # Fetch existing aliases so we can pre-fill the inputs if they haven't changed
        existing_mapping = self.config_entry.options.get(CONF_CALENDAR_ALIASES, {})
        reverse_mapping = {cal_id: alias for alias, cal_id in existing_mapping.items()}

        schema = {}
        for cal_id in self.selected_calendars:
            if cal_id in reverse_mapping:
                default_alias = reverse_mapping[cal_id]
            else:
                name = self.calendar_names.get(cal_id, cal_id.split("@")[0])
                default_alias = sanitize_alias(name)
            
            schema[vol.Required(cal_id, default=default_alias)] = str

        return self.async_show_form(
            step_id="aliases",
            data_schema=vol.Schema(schema),
            errors=errors
        )