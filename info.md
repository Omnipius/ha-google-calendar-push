# Google Calendar Push API

Creates a Pydantic-validated webhook endpoint to receive and sync iCalendar (RFC 5545/9775) events directly to Google Calendar.

**Features:**
* Strict Pydantic validation via the `ical` module.
* Native handling of RFC 9775 timezone strings.
* Idempotent Add/Update/Remove operations.
* Automatic alarm and notification generation.