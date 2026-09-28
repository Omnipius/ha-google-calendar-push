# Calendar Push Endpoint: Client Integration Guide (Schema Version 1)

This document specifies the interface contract, semantics, and implementation requirements for client agents integrating with the `google_calendar_push` endpoint on Home Assistant.

---

## 1. Overview & Key Capabilities

The `google_calendar_push` endpoint has been upgraded to **Schema Version 1** with enterprise synchronization guarantees:

1. **Per-UID Acknowledgements**: Every submitted UID receives an explicit result record indicating terminal state (`applied`, `already_applied`, `stale_ignored`, `rejected`, `retryable`, `conflict`).
2. **Durable Server-Side Idempotency**: Sending an `Idempotency-Key` guarantees that duplicate requests or retries return cached receipts without repeating downstream Google Calendar mutations.
3. **Source-Revision Ordering & Tombstones**: Out-of-order network replays cannot overwrite newer calendar events, and deletion tombstones (30-day retention) prevent delayed updates from resurrecting deleted events ("zombie events").
4. **Stable Machine-Readable Error Codes & Retryability**: Every item failure explicitly declares whether the condition is `retryable` and provides a standard machine-readable code.
5. **Partial Success (HTTP 207 Multi-Status)**: Mixed batches return HTTP 207 with a complete per-UID breakdown of successes and failures.
6. **Backward Compatibility**: Existing legacy envelopes (`{ "operation": "add", "events": [...] }`) remain supported while benefiting from per-UID results and idempotency guarantees.

---

## 2. Endpoint & HTTP Headers

### Endpoint URL
```http
POST /api/google_calendar_push/{calendar_alias}
```
- `{calendar_alias}`: The configured alias of the target calendar (e.g., `work`, `personal`).

### HTTP Request Headers
| Header | Requirement | Description |
|---|---|---|
| `Authorization` | **Required** | Home Assistant Long-Lived Access Token: `Bearer <TOKEN>` |
| `Content-Type` | **Required** | `application/json` |
| `Idempotency-Key` | **Recommended** | Stable UUID or hash per submission. If retried with the exact same payload, the backend returns the original result without re-executing mutations. |
| `X-Request-ID` | **Recommended** | Correlation ID for tracing across client and backend logs. If omitted, the backend generates a UUID4. |

### HTTP Response Headers
| Header | Description |
|---|---|
| `Retry-After` | Included on `429` (Rate Limited) and `503` (Downstream Timeout/Failure) indicating suggested backoff in seconds. |

---

## 3. Request Payloads

The backend accepts two envelope formats:

### A. Recommended: Schema Version 1 (Mixed or Batched Operations)
```json
{
  "schema_version": 1,
  "client_version": "0.1.0",
  "request_id": "8d5c90b2-3f1a-4d22-9bb4-681f08a514d2",
  "idempotency_key": "idemp-batch-2026-09-28-001",
  "target_alias": "work",
  "items": [
    {
      "uid": "040000008200E0004B50...",
      "operation": "upsert",
      "source_revision": "2026-09-28T01:24:32.000000Z",
      "summary": "Sprint Planning",
      "dtstart": "2026-09-28T10:00:00Z[America/New_York]",
      "dtend": "2026-09-28T11:00:00Z[America/New_York]",
      "status": "CONFIRMED",
      "class": "PUBLIC"
    },
    {
      "uid": "040000008200E0004B50-OLD-MEETING",
      "operation": "remove",
      "source_revision": "2026-09-28T01:25:00.000000Z"
    }
  ]
}
```

*Note*: You can either supply event fields top-level within each item object, or nested in an `"event": { ... }` sub-object. Both are automatically parsed.

### B. Legacy Format (Backward-Compatible)
```json
{
  "operation": "add",
  "events": [
    {
      "uid": "legacy-uid-1",
      "source_revision": "2026-09-28T01:24:32Z",
      "summary": "Team Sync",
      "dtstart": "2026-09-28T09:00:00Z"
    }
  ]
}
```

### Supported Operations
- `upsert` (or legacy `add`, `update`): Inserts or updates the event.
- `remove` (or legacy `delete`): Deletes the event and places a deletion tombstone.

---

## 4. Response Schema & Semantics

Every response returns an envelope containing:
- `schema_version`: `1`
- `request_id`: Echoes request correlation ID.
- `target_alias`: Echoes canonical target calendar alias.
- `overall_status`: `"success"`, `"partial"`, or `"error"`.
- `events_processed`: Integer count of successful operations (informational).
- `results`: **List containing exactly one result for every submitted UID.**

### Example: Complete Success (HTTP 200)
```json
{
  "schema_version": 1,
  "request_id": "8d5c90b2-3f1a-4d22-9bb4-681f08a514d2",
  "idempotency_key": "idemp-batch-2026-09-28-001",
  "target_alias": "work",
  "overall_status": "success",
  "events_processed": 2,
  "results": [
    {
      "uid": "040000008200E0004B50...",
      "operation": "upsert",
      "source_revision": "2026-09-28T01:24:32.000000Z",
      "status": "applied"
    },
    {
      "uid": "040000008200E0004B50-OLD-MEETING",
      "operation": "remove",
      "source_revision": "2026-09-28T01:25:00.000000Z",
      "status": "applied"
    }
  ]
}
```

### Example: Partial Success (HTTP 207 Multi-Status)
```json
{
  "schema_version": 1,
  "request_id": "8d5c90b2-3f1a-4d22-9bb4-681f08a514d2",
  "target_alias": "work",
  "overall_status": "partial",
  "events_processed": 1,
  "results": [
    {
      "uid": "uid-ok",
      "operation": "upsert",
      "status": "applied"
    },
    {
      "uid": "uid-bad-rrule",
      "operation": "upsert",
      "status": "rejected",
      "code": "UNSUPPORTED_RECURRENCE",
      "retryable": false,
      "message": "Unsupported recurrence pattern: invalid RRULE syntax."
    }
  ],
  "errors": [
    {
      "uid": "uid-bad-rrule",
      "error": "Unsupported recurrence pattern: invalid RRULE syntax."
    }
  ]
}
```

---

## 5. Acknowledgement Statuses

| Status | Meaning | Recommended Client Action |
|---|---|---|
| `applied` | Target calendar mutation succeeded downstream in Google Calendar. | Remove item from client outbox. |
| `already_applied` | Idempotent replay or redundant deletion (event was already in requested state). | Remove item from client outbox. |
| `stale_ignored` | Received source revision is older than the currently applied revision or deletion tombstone. Safely ignored to prevent overwrites or resurrection. | Remove item from client outbox. |
| `rejected` | Permanent validation or policy failure (bad datetime, unsupported RRULE, schema violation). | Move to `needs_attention` queue. Do NOT retry automatically. |
| `retryable` | Temporary downstream failure (Google 429 quota/rate limit, 503 service unavailable, network timeout). | Retain in client outbox. Retry with exponential backoff (honor `Retry-After`). |
| `conflict` | Idempotency key conflict or revision conflict. | Inspect outbox for duplicate key generation. |

---

## 6. Stable Machine-Readable Error Codes

| Code | HTTP Status | Retryable? | Description |
|---|---|---|---|
| `AUTHENTICATION_FAILED` | `401` | No | Home Assistant or Google OAuth token is expired or unauthorized. Pause delivery and alert operator. |
| `AUTHORIZATION_FAILED` | `403` | No | Permission denied accessing the target Google Calendar. |
| `INVALID_REQUEST` | `400` | No | Malformed JSON, target alias mismatch, or missing `items`/`events` list. |
| `INVALID_EVENT` | `422` / `207` | No | Pydantic / RFC 5545 schema validation failure on event attributes. |
| `UNSUPPORTED_RECURRENCE` | `422` / `207` | No | Recurrence rule contains syntax or patterns rejected by Google Calendar. |
| `IDEMPOTENCY_CONFLICT` | `409` | No | Idempotency key was reused with a different payload body. |
| `TARGET_UNAVAILABLE` | `404` | No | Endpoint alias or target Google Calendar ID does not exist. |
| `RATE_LIMITED` | `429` / `207` | Yes | Google API quota or rate limit exceeded. Check `Retry-After` header. |
| `DOWNSTREAM_TIMEOUT` | `503` / `207` | Yes | Google API or network timeout while executing mutations. Retry later. |
| `INTERNAL_ERROR` | `500` / `207` | Yes | Unexpected backend execution error. |

---

## 7. Client Synchronization Best Practices

### A. Idempotency Keys
- Generate a deterministic or persistent UUID for each batch or operation.
- Retrying with the same `Idempotency-Key` and payload will return the original response immediately without issuing calls to Google.
- Never reuse the same `Idempotency-Key` for different payload contents (will return `409 Conflict`).

### B. Source Revision Ordering & Tombstones
- Supply `source_revision` (UTC ISO 8601 string, e.g. `2026-09-28T01:30:00.000000Z`) on every upsert and removal.
- The backend automatically detects out-of-order deliveries. Older revisions arriving after newer revisions return `status: "stale_ignored"`.
- When an event is deleted, the backend retains a tombstone for 30 days. Any delayed upserts with revisions older than or equal to the deletion will return `status: "stale_ignored"` rather than resurrecting the event.
- If an event is intentionally recreated after deletion, send it with a strictly newer `source_revision` than the deletion tombstone.

### C. Classification Semantics
- Security and classification fields are optional:
  - Missing, `null`, or blank `""` classification is recognized as **unrestricted** (`PUBLIC`).
  - Explicit values (`PUBLIC`, `PRIVATE`, `CONFIDENTIAL`) are mapped directly to Google visibility.

### D. Target Alias Validation
- Always populate `"target_alias": "<alias>"` in your request payload matching the URL path `/api/google_calendar_push/<alias>`.
- The backend verifies that the payload alias matches the route alias, guarding against proxy routing or client configuration mistakes.
