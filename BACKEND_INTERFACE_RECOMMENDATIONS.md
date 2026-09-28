# Calendar Push Backend Interface Recommendations

These recommendations describe changes to the calendar push endpoint that would
make delivery safer, easier to reconcile, and less dependent on ambiguous
response fields.

## Priority summary

1. **Per-UID acknowledgement results** — release-blocking.
2. **Durable idempotency and source-revision ordering** — release-blocking.
3. **Stable error codes and retryability** — highly recommended.
4. **Removal tombstones** — highly recommended.
5. **Schema versioning and target validation** — recommended.
6. **Correlation IDs, receipts, and richer observability** — recommended.
7. **A future mixed-operation/batch envelope** — useful, but not required for
   the immediate recovery.

## 1. Use explicit per-UID acknowledgements

The client must not use a count such as `events_processed` as proof that
particular UIDs were accepted. A count may represent a target-calendar total,
an aggregate metric, or another backend quantity.

The response should contain exactly one result for every submitted UID:

```json
{
  "schema_version": 1,
  "request_id": "8d5c...",
  "target_alias": "work",
  "overall_status": "success",
  "results": [
    {
      "uid": "040000008200E000...",
      "operation": "upsert",
      "source_revision": "2026-09-28T01:24:32.000000Z",
      "status": "accepted"
    }
  ]
}
```

For partial success:

```json
{
  "schema_version": 1,
  "request_id": "8d5c...",
  "target_alias": "work",
  "overall_status": "partial",
  "results": [
    {
      "uid": "uid-ok",
      "operation": "upsert",
      "status": "accepted"
    },
    {
      "uid": "uid-bad",
      "operation": "upsert",
      "status": "rejected",
      "code": "UNSUPPORTED_RECURRENCE",
      "retryable": false,
      "message": "The recurrence pattern is not supported."
    }
  ]
}
```

`events_processed` may remain as an informational metric, but it must not be
used as the acknowledgement mechanism.

The client should require that:

- every submitted UID appears exactly once;
- no unknown UIDs appear;
- every result has an explicit status; and
- missing or malformed results are treated as ambiguous failures and retained
  in the durable outbox.

Once the backend returns complete per-UID results, the client should stop
inferring that UIDs absent from an `errors` list succeeded.

## 2. Define acknowledgement semantics

The backend should distinguish among these states:

- `accepted`: durably stored or placed on the backend's own durable queue;
- `applied`: target calendar state has been updated;
- `already_applied`: idempotent replay or duplicate request;
- `stale_ignored`: an older source revision was safely ignored;
- `rejected`: permanent validation or policy failure;
- `retryable`: temporary backend or downstream failure;
- `conflict`: conflicting revision or idempotency-key reuse.

For this application, the client can remove an outbox item after `accepted`
only if the backend guarantees that processing is durable from that point
forward. The backend should not report success before the request is safely
persisted.

## 3. Make idempotency a server-side guarantee

The client sends a stable `Idempotency-Key`. The backend should persist
idempotency receipts and guarantee that a retry with the same key does not
repeat the side effect.

Recommended behavior:

- same key and same request: return the original result;
- same key and different payload: return `409 Conflict`;
- same UID with an older source revision: return `stale_ignored` or
  `already_applied`;
- same UID with a newer source revision: apply the newer update;
- scope idempotency and revision checks to the target calendar and UID.

Idempotency receipts should be retained for at least the maximum expected retry
and reconciliation period. Several days, or retention until superseded by a
newer revision, is safer than a short expiry.

The idempotency key should be echoed in the response, and may also be included
in each item result for easier audit and replay diagnostics.

## 4. Use source revisions for ordering

Every upsert and removal should carry a source revision:

```json
{
  "uid": "040000008200E000...",
  "source_revision": "2026-09-28T01:30:00.000000Z"
}
```

The backend should prevent an older update from overwriting a newer update or
resurrecting an item after a newer removal.

Important details:

- comparisons should be made per target calendar and UID;
- timestamps should be normalized to UTC at the wire boundary;
- equal revisions should be idempotent;
- an idempotency conflict should not be silently interpreted as success.

## 5. Add stable machine-readable error codes

Free-form messages are useful for operators but should not drive client retry
behavior. Item-level results should include stable codes:

```text
AUTHENTICATION_FAILED
AUTHORIZATION_FAILED
INVALID_REQUEST
INVALID_EVENT
UNSUPPORTED_RECURRENCE
STALE_REVISION
IDEMPOTENCY_CONFLICT
TARGET_UNAVAILABLE
DOWNSTREAM_TIMEOUT
RATE_LIMITED
INTERNAL_ERROR
```

Each item-level failure should identify whether retrying is appropriate:

```json
{
  "uid": "uid-bad",
  "status": "rejected",
  "code": "UNSUPPORTED_RECURRENCE",
  "retryable": false,
  "message": "The recurrence pattern is not supported."
}
```

Suggested client behavior:

- permanent validation errors: move directly to `needs_attention`;
- transient errors: retry with exponential backoff;
- authentication errors: pause delivery and alert rather than retrying
  indefinitely;
- rate limits: honor `Retry-After`;
- backend/downstream failures: retry.

For `429` and `503`, the backend should send a standard `Retry-After` header.

## 6. Make removal semantics explicit

Removals should remain UID-only:

```json
{
  "operation": "remove",
  "uid": "040000008200E000...",
  "source_revision": "2026-09-28T01:30:00.000000Z"
}
```

The backend should retain deletion tombstones long enough to prevent a delayed
older update from recreating the event. A removal should return one of:

```json
{
  "uid": "040000008200E000...",
  "operation": "remove",
  "status": "accepted"
}
```

or:

```json
{
  "uid": "040000008200E000...",
  "operation": "remove",
  "status": "already_applied"
}
```

Tombstone retention should exceed the maximum expected retry and reconciliation
delay.

## 7. Version the wire contract

Add an explicit schema version and client metadata:

```json
{
  "schema_version": 1,
  "client_version": "0.1.0",
  "target_alias": "work",
  "items": []
}
```

The current client sends one operation per request. That format can remain
supported for compatibility. For a future version, consider an envelope where
each item carries its own operation:

```json
{
  "schema_version": 1,
  "target_alias": "work",
  "items": [
    {
      "uid": "uid-1",
      "operation": "upsert",
      "source_revision": "2026-09-28T01:24:32Z",
      "idempotency_key": "key-1",
      "event": {}
    },
    {
      "uid": "uid-2",
      "operation": "remove",
      "source_revision": "2026-09-28T01:25:00Z",
      "idempotency_key": "key-2"
    }
  ]
}
```

This will make mixed-operation batching easier without requiring an immediate
client migration.

## 8. Validate the target calendar

The backend should echo the canonical target alias in the response. The client
should reject a response whose target does not match the requested target.

This protects against configuration mistakes, proxy routing errors, or an
endpoint accidentally applying an update to the wrong calendar.

## 9. Preserve classification semantics

Security and classification fields must remain optional. Their absence is a
legitimate signal that the event is unrestricted.

Required semantics:

- missing, `null`, or blank classification: unrestricted; allow the event;
- explicit restricted values: the client normally filters the event before
  sending it;
- malformed non-empty values: the client omits the event and records a
  privacy-safe diagnostic.

The backend should not make classification mandatory, and should not reject an
event solely because the field is absent. If the backend performs defensive
classification checks, it should preserve the same distinction between absent
and malformed values.

## 10. Add request correlation and delivery receipts

Each request should have a correlation ID, either supplied by the client or
generated by the backend:

```json
{
  "request_id": "8d5c...",
  "idempotency_key": "..."
}
```

The backend should echo the ID and log it with:

- target alias;
- UID;
- operation;
- source revision;
- idempotency key;
- result code; and
- processing duration.

Logs must not contain event bodies, bearer tokens, or sensitive classification
values.

For asynchronous backend processing, consider:

```text
POST /v1/calendar-push
GET  /v1/calendar-push/receipts/{receipt_id}
```

The initial POST should return `accepted` only after the backend has durably
queued the request.

## Suggested HTTP status semantics

| Condition | HTTP status |
|---|---:|
| All items accepted/applied | `200` |
| Mixed per-UID results | `207` or `200` with `overall_status: partial` |
| Malformed request envelope | `400` |
| Authentication failure | `401` |
| Authorization failure | `403` |
| Idempotency or revision conflict | `409` |
| Valid request with permanent item failures | `422` or `207` |
| Rate limited | `429` |
| Temporary backend/downstream failure | `503` |
| Unexpected backend failure | `500` |

`207 Multi-Status` is reasonable if its JSON format is documented and
consistent. An alternative is to return `200` for every syntactically valid
envelope and use explicit per-item statuses for all success and failure
outcomes.

## Recommended backend acceptance tests

The backend should test at least:

- one-item complete success;
- multi-item complete success;
- `207` partial success with explicit results for every UID;
- malformed or missing per-UID results;
- duplicate idempotency-key replay;
- idempotency-key reuse with a different payload;
- older revision after a newer revision;
- newer update after a removal;
- duplicate removal;
- delayed update after a removal tombstone;
- unsupported recurrence as a permanent item-level rejection;
- temporary downstream failure with `retryable: true`;
- `429`/`503` responses with `Retry-After`;
- missing, `null`, and blank classification fields being accepted as
  unrestricted;
- restricted and malformed classification behavior;
- target-alias mismatch detection;
- redaction of tokens and event-sensitive data from logs.

