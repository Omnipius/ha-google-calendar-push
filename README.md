# Google Calendar Push API for Home Assistant

![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)
![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)

A robust, Pydantic-validated webhook endpoint to receive and sync rich iCalendar (RFC 5545 / RFC 9775) events directly to Google Calendar. 

Instead of relying on slow polling intervals, this integration opens a dedicated, secure REST API endpoint in Home Assistant. It allows external applications, scripts, or mail parsers to actively **push** calendar changes to Google Calendar in real-time.

## Features

* **Asynchronous Batch Execution:** Automatically batches API requests to Google in safe, chunked threads. This allows massive historical syncs to be processed rapidly without triggering watchdog timeouts or blocking Home Assistant's main event loop.
* **Strict Validation:** Incoming payloads are rigorously validated against the `ical` module using Pydantic.
* **Recurrence Exceptions & Virtual Instances:** Intelligently maps `recurrence-id` to `originalStartTime`, allowing you to modify or delete specific instances of a recurring meeting without destroying the master series or breaking Google's base32hex ID chaining.
* **Idempotent Operations:** Automatically delegates sequence management to Google and intercepts `add` operations for existing events to prevent duplicate calendar entries.
* **Smart Timezone Handling:** Natively parses RFC 9775 timezone strings (e.g., `[America/Los_Angeles]`) and seamlessly strips conflicting UTC offsets. Furthermore, it automatically maps proprietary Microsoft Windows Timezone names (e.g., `GMT Standard Time`) to universal IANA standards for flawless Outlook-to-Google syncing.
* **Zero-Duration Safeguards:** Automatically catches and pads zero-duration exceptions or cancellations lacking end-times to comply with strict Google Calendar API schema requirements.
* **Graceful Restoration:** Correctly handles soft-deleted (cancelled) Google Calendar events.

## Prerequisites

Before installing this integration, you must configure a Google Cloud Project to generate OAuth2 credentials.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project and enable the **Google Calendar API**.
3. Go to **APIs & Services > OAuth consent screen** and configure it for *External* use. 
   * Add the following scopes:
     * `https://www.googleapis.com/auth/calendar.readonly`
     * `https://www.googleapis.com/auth/calendar.events`
     * `https://www.googleapis.com/auth/userinfo.email`
4. Go to **Credentials > Create Credentials > OAuth client ID** (Web application).
   * **Authorized redirect URIs:** Add your Home Assistant OAuth callback URL (e.g., `https://my.home-assistant.io/redirect/oauth` or `https://<YOUR_HA_URL>/auth/external/callback`).
5. Keep your **Client ID** and **Client Secret** handy.

## Installation

### Via HACS (Recommended)

1. Open Home Assistant and navigate to **HACS** > **Integrations**.
2. Click the three dots (⋮) in the top right corner and select **Custom repositories**.
3. Add the URL of this repository and select **Integration** as the category.
4. Click **Download**, then restart Home Assistant.

### Configuration

1. In Home Assistant, navigate to **Settings > Devices & Services > ⚙️ (Three dots) > Application Credentials**.
2. Add a new credential. Select **Google Calendar Push API** and input the Client ID and Secret you generated in Google Cloud.
3. Return to the Integrations page and click **+ Add Integration**. Search for **Google Calendar Push API**.
4. You will be redirected to Google to authorize the application. 
5. Select the editable calendars you wish to expose and assign a short, custom alias to each. (Spaces and special characters will be automatically and safely converted to underscores).

---

## API Usage

Once configured, the integration listens for `POST` requests at:

`http(s)://<YOUR_HA_URL>/api/google_calendar_push/<YOUR_CALENDAR_ALIAS>`

### Authentication
Requests must be authenticated using a Home Assistant Long-Lived Access Token.
**Header:** `Authorization: Bearer <YOUR_LONG_LIVED_TOKEN>`

### Payload Structure
The endpoint expects a JSON payload containing the `operation` (add, update, remove) and an array of `events` formatted to RFC 5545 / RFC 9775 standards.

**HTTP POST Example:**
```json
{
  "operation": "add",
  "events": [
    {
      "uid": "unique-event-id-12345",
      "summary": "Team Standup",
      "description": "Weekly status sync.",
      "location": "Conference Room A",
      "dtstart": "2026-04-08T09:35:00Z[America/Los_Angeles]",
      "dtend": "2026-04-08T10:00:00Z[America/Los_Angeles]",
      "status": "CONFIRMED",
      "transp": "OPAQUE",
      "class": "PUBLIC",
      "rrule": {
        "freq": "WEEKLY",
        "until": "2026-11-27T00:00:00Z",
        "byday": [{"weekday": "WE"}]
      },
      "valarm": [
        {
          "action": "DISPLAY",
          "trigger": "-PT15M"
        }
      ]
    }
  ]
}
```

### Supported Operations
* `add`: Inserts the event. If the uid already exists, it intelligently falls back to an update.
* `update`: Updates the event. (Returns a failure if the uid does not exist).
* `remove`: Deletes the event. (Idempotent: Succeeds even if the event is already deleted).

## Troubleshooting

* **400 Bad Request:** Google Calendar rejects invalid ISO strings or malformed configurations. Check the response body for Google's specific rejection reason.
* **207 Multi-Status:** Returned if a batch array contains both successful updates and validation failures. The response body will contain an errors array indicating which uid failed.
* **404 Not Found:** Ensure the alias in your URL exactly matches the alias you configured in the Home Assistant UI. Can also trigger if attempting to update a virtual instance of an event before Google's recurrence engine has finished generating the master series.