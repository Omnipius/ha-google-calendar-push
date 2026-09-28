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

---

## 8. Full-Calendar Reconciliation (`replace_all` mode)

For scenarios requiring complete calendar alignment, the backend supports **Schema Version 1 Full-Calendar Reconciliation** using a three-phase session protocol: `begin`, `items` (chunked 1..N), and `finalize`.

In `replace_all` mode, the target calendar is treated as dedicated to this synchronization source. Events present on Google Calendar that were not received in the snapshot are deleted during the atomic finalization cutover, and tombstones are recorded.

```
       Client                                Backend
         |                                      |
         | --- 1. begin (count, digest) ------> | Session opened (supersedes older sessions)
         | <--- 200 OK (status: open) --------- |
         |                                      |
         | --- 2. items seq=1 (events) -------> | Validates & applies chunk
         | <--- 200 OK (status: receiving) ---- |
         |                                      |
         | --- 3. items seq=2 (events) -------> | Contiguous sequence validation
         | <--- 200 OK (status: receiving) ---- |
         |                                      |
         | --- 4. finalize (total, digest) ---> | Preconditions verified;
         |                                      | Stale events deleted from Google;
         |                                      | Tombstones created;
         | <--- 200 OK (status: completed) ---- | Session marked completed
```

### Phase 1: Begin Session (`phase: "begin"`)
Initializes a new reconciliation session. If an earlier session was in progress for the same calendar alias, it is automatically marked as `superseded`.

**Request:**
```json
{
  "schema_version": 1,
  "request_id": "req-snap-01-begin",
  "idempotency_key": "idemp-snap-01-begin",
  "target_alias": "work",
  "snapshot": {
    "snapshot_id": "snap-2026-09-28-001",
    "mode": "replace_all",
    "phase": "begin",
    "expected_item_count": 42,
    "expected_uid_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
}
```

**Response (HTTP 200):**
```json
{
  "schema_version": 1,
  "request_id": "req-snap-01-begin",
  "target_alias": "work",
  "overall_status": "success",
  "snapshot": {
    "snapshot_id": "snap-2026-09-28-001",
    "mode": "replace_all",
    "phase": "begin",
    "status": "open",
    "expected_item_count": 42,
    "received_item_count": 0
  },
  "results": [],
  "errors": []
}
```

### Phase 2: Ingest Item Chunks (`phase: "items"`)
Transmits items in one or more contiguous, 1-based sequence chunks (`sequence: 1`, `sequence: 2`, etc.).

**Request:**
```json
{
  "schema_version": 1,
  "request_id": "req-snap-01-chunk-1",
  "idempotency_key": "idemp-snap-01-chunk-1",
  "target_alias": "work",
  "snapshot": {
    "snapshot_id": "snap-2026-09-28-001",
    "mode": "replace_all",
    "phase": "items",
    "sequence": 1,
    "is_final": false
  },
  "items": [
    {
      "uid": "event-101",
      "operation": "upsert",
      "source_revision": "2026-09-28T12:00:00Z",
      "summary": "Engineering Sync",
      "dtstart": "2026-09-28T14:00:00Z",
      "dtend": "2026-09-28T15:00:00Z"
    }
  ]
}
```

**Response (HTTP 200 or 207):**
```json
{
  "schema_version": 1,
  "request_id": "req-snap-01-chunk-1",
  "target_alias": "work",
  "overall_status": "success",
  "snapshot": {
    "snapshot_id": "snap-2026-09-28-001",
    "mode": "replace_all",
    "phase": "items",
    "sequence": 1,
    "status": "receiving",
    "expected_item_count": 42,
    "received_item_count": 1
  },
  "events_processed": 1,
  "results": [
    {
      "uid": "event-101",
      "operation": "upsert",
      "status": "applied",
      "source_revision": "2026-09-28T12:00:00Z"
    }
  ]
}
```

### Phase 3: Finalize & Atomic Cutover (`phase: "finalize"`)
Instructs the backend to verify completeness and perform cutover deletion of stale events.

**Preconditions checked before deletion:**
1. Snapshot status is `receiving` or `open`.
2. Snapshot has not been superseded by a newer session.
3. Count of unique received UIDs matches `expected_item_count` (and `total_items`).
4. Canonical SHA-256 digest of received UIDs matches `expected_uid_digest` (and `uid_digest`).
5. All items in the snapshot succeeded; no unresolved or failed items remain.

**Request:**
```json
{
  "schema_version": 1,
  "request_id": "req-snap-01-finalize",
  "idempotency_key": "idemp-snap-01-finalize",
  "target_alias": "work",
  "snapshot": {
    "snapshot_id": "snap-2026-09-28-001",
    "mode": "replace_all",
    "phase": "finalize",
    "total_items": 42,
    "uid_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
}
```

**Response (HTTP 200):**
```json
{
  "schema_version": 1,
  "request_id": "req-snap-01-finalize",
  "target_alias": "work",
  "overall_status": "success",
  "snapshot": {
    "snapshot_id": "snap-2026-09-28-001",
    "mode": "replace_all",
    "phase": "finalize",
    "status": "completed",
    "expected_item_count": 42,
    "received_item_count": 42
  },
  "reconciliation": {
    "target_total_before": 45,
    "items_applied": 42,
    "deleted_count": 3,
    "target_total_after": 42
  },
  "results": [],
  "errors": []
}
```

*Note on Retries*: Re-sending `finalize` on an already-completed snapshot returns HTTP 200 with the completed snapshot state and identical reconciliation counts.

---

### Canonical UID Digest Specification

The UID digest ensures that both client and server agree on the exact set of events ingested before any deletions occur:

1. Deduplicate the UID list.
2. Sort the unique UIDs lexicographically (standard ASCII sort).
3. Join them with a newline character (`\n`) without a trailing newline.
4. Calculate the SHA-256 hexadecimal hash.

```python
import hashlib

def compute_uid_digest(uids: list[str]) -> str:
    unique_sorted = sorted(set(uids))
    content = "\n".join(unique_sorted)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
```

- **Empty snapshot digest** (`expected_item_count: 0`): SHA-256 of empty string `""` -> `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- **Normalization**: The backend automatically strips the optional `sha256:` prefix, lowercases the string, and trims whitespace.

---

### Clearing a Calendar (Empty Snapshot)

To delete all events on a calendar safely:
1. Send `phase: "begin"` with `expected_item_count: 0` and `expected_uid_digest: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"`.
2. Skip the `items` phase.
3. Send `phase: "finalize"` with `total_items: 0` and `uid_digest: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"`.
4. The backend deletes every event currently on the calendar and returns `deleted_count: N, target_total_after: 0`.

---

### Snapshot Reconciliation Error Codes

| Code | HTTP Status | Description |
|---|---|---|
| `SNAPSHOT_NOT_FOUND` | `404` | Specified `snapshot_id` does not exist or has expired. |
| `SNAPSHOT_INCOMPLETE` | `400` | Count of received items does not match `expected_item_count` or `total_items`. |
| `SNAPSHOT_DIGEST_MISMATCH` | `400` | Computed SHA-256 digest of received UIDs does not match `expected_uid_digest` or `uid_digest`. |
| `SNAPSHOT_SEQUENCE_INVALID` | `400` | Sequence chunk is out of order (skipped sequence or duplicate sequence). |
| `SNAPSHOT_SUPERSEDED` | `409` | A newer snapshot session began for this calendar alias; this session has been invalidated. |
| `SNAPSHOT_FINALIZE_CONFLICT` | `409` | Finalization blocked because unresolved/failed items remain in the session. |

