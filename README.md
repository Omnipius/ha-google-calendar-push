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

For complete client developer documentation, see [`CLIENT_INTEGRATION_GUIDE.md`](./CLIENT_INTEGRATION_GUIDE.md).

### Authentication & Headers
* **Authorization** (Required): `Bearer <YOUR_LONG_LIVED_TOKEN>`
* **Idempotency-Key** (Recommended): Stable request UUID for server-side deduplication.
* **X-Request-ID** (Optional): Request correlation ID echoed in logs and response envelopes.

### Payload Structure (Schema Version 1)

The endpoint accepts both the modern Schema Version 1 batch envelope (supporting mixed operations) and legacy payloads:

```json
{
  "schema_version": 1,
  "request_id": "8d5c90b2-3f1a-4d22-9bb4-681f08a514d2",
  "idempotency_key": "idemp-sync-2026-09-28-001",
  "target_alias": "work",
  "items": [
    {
      "uid": "unique-event-id-12345",
      "operation": "upsert",
      "source_revision": "2026-09-28T01:24:32.000000Z",
      "summary": "Team Standup",
      "dtstart": "2026-04-08T09:35:00Z[America/Los_Angeles]",
      "dtend": "2026-04-08T10:00:00Z[America/Los_Angeles]",
      "status": "CONFIRMED",
      "class": "PUBLIC"
    },
    {
      "uid": "cancelled-meeting-id-6789",
      "operation": "remove",
      "source_revision": "2026-09-28T01:25:00.000000Z"
    }
  ]
}
```

### Response Envelope (Per-UID Acknowledgements)

Every response contains an explicit status for each submitted UID:

```json
{
  "schema_version": 1,
  "request_id": "8d5c90b2-3f1a-4d22-9bb4-681f08a514d2",
  "target_alias": "work",
  "overall_status": "success",
  "events_processed": 2,
  "results": [
    {
      "uid": "unique-event-id-12345",
      "operation": "upsert",
      "source_revision": "2026-09-28T01:24:32.000000Z",
      "status": "applied"
    },
    {
      "uid": "cancelled-meeting-id-6789",
      "operation": "remove",
      "source_revision": "2026-09-28T01:25:00.000000Z",
      "status": "applied"
    }
  ]
}
```

### Supported Operations
* `upsert` (or `add` / `update`): Inserts or updates the event.
* `remove` (or `delete`): Deletes the event and places a 30-day deletion tombstone to prevent resurrection from delayed packets.

### HTTP Response Codes
* **200 OK**: All items processed successfully (`applied`, `already_applied`, or `stale_ignored`).
* **207 Multi-Status**: Mixed outcome batch containing both successful and rejected/retryable items.
* **400 Bad Request**: Malformed JSON or target alias mismatch.
* **401 Unauthorized**: Invalid or expired Home Assistant / Google authentication.
* **409 Conflict**: Idempotency key reused with a different payload body.
* **422 Unprocessable Entity**: Non-retryable event schema or recurrence validation failures.
* **429 Too Many Requests**: Google Calendar API rate limit exceeded (includes `Retry-After` header).
* **503 Service Unavailable**: Downstream timeout or temporary failure (includes `Retry-After` header).