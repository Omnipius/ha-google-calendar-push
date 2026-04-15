"""application_credentials platform for Google Calendar Push."""
from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return authorization server for Google."""
    return AuthorizationServer(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
    )

async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Return description placeholders for the credentials dialog."""
    return {
        "name": "Google Calendar Push API",
        "client_id": "Client ID",
        "client_secret": "Client Secret",
    }