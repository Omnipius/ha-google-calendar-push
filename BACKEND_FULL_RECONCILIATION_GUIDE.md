# Backend Guide: Schema v1 Full-Calendar Reconciliation

## Purpose and safety boundary

The client now supports an authoritative full-calendar reconciliation for the
configured target alias. The target calendar is **dedicated to this sync**.
`replace_all` is intentionally destructive: finalization deletes every target
event whose UID is absent from the completed eligible Outlook snapshot.

Do not enable this mode for a shared calendar. The backend route and its
operator documentation must clearly identify the calendar as sync-owned.

The existing Schema Version 1 ordinary item contract remains unchanged:
upserts and removals are still ordered by `source_revision`, use stable
idempotency keys, return complete per-UID results, and write deletion
tombstones.

## Snapshot request envelope

Full reconciliation uses the existing Schema v1 endpoint:

```http
POST /api/google_calendar_push/{target_alias}
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: <deterministic-key>
X-Request-ID: <deterministic-request-id>
```

Every snapshot request contains:

```json
{
  "schema_version": 1,
  "client_version": "0.1.0",
  "request_id": "...",
  "idempotency_key": "...",
  "target_alias": "work",
  "snapshot": {
    "snapshot_id": "...",
    "mode": "replace_all",
    "phase": "begin",
    "sequence": 0,
    "expected_item_count": 42,
    "uid_digest": "sha256..."
  },
  "items": []
}
```

The phases are:

1. `begin`: opens/replaces the active server-side snapshot session. It has no
   item mutations.
2. `items`: submits one non-empty, monotonically numbered batch of normal
   Schema v1 `upsert` items. Each item includes `uid` and UTC
   `source_revision`.
3. `finalize`: has no item mutations. It verifies the complete snapshot and
   atomically applies the replace-all deletion.

The UID digest is the SHA-256 digest of the sorted unique source UID set using
the client/backend canonical delimiter rules. The backend must use the same
canonicalization when validating the final request.

## Durable snapshot sessions

Add durable storage keyed by `(target_alias, snapshot_id)`, including at least:

- mode (`replace_all`);
- current lifecycle status;
- expected item count and UID digest;
- received sequence numbers;
- received UID set;
- per-item processing state and source revisions;
- cached request receipts keyed by idempotency key;
- active/newest snapshot generation for the target;
- created, updated, begun, and finalized timestamps;
- supersession metadata and diagnostic code.

The snapshot session must survive backend restarts. Never keep the only copy of
the UID set or item sequence in process memory.

Only an explicitly recognized `mode: "replace_all"` may enter this destructive
path. Reject unknown modes.

## Idempotency and exact retries

All three phases must be idempotent:

- An exact retry with the same idempotency key and canonical body returns the
  cached response receipt without repeating Google mutations.
- Reusing an idempotency key with a different canonical body must be rejected
  with `409` / `IDEMPOTENCY_CONFLICT`.
- The response must echo the original `request_id`, `target_alias`, snapshot ID,
  phase, and server snapshot status.
- The client intentionally retries an exact phase request after timeouts; do
  not create a second snapshot generation for that retry.

The request-level idempotency key includes schema version, client version,
target alias, snapshot ID, phase, sequence, and canonical payload digest.

## Begin validation

`begin` must:

- validate the route alias and payload `target_alias`;
- validate schema version and `replace_all` mode;
- reject negative counts, malformed digest values, and invalid snapshot IDs;
- create or reopen the durable session;
- supersede any older active snapshot for the same target;
- prevent an older snapshot from finalizing later;
- return a control response with received and acknowledged counts.

Starting a newer snapshot must not delete target events. It only marks older
sessions superseded, so an old finalize cannot perform a stale destructive
cutover.

## Item phase validation

For each `items` request:

- require the snapshot session to exist and be active;
- reject a superseded or finalized snapshot;
- require `sequence` to be a positive, monotonically increasing sequence;
- reject missing, duplicate, or contradictory sequences;
- require a non-empty item list;
- require each UID to occur once in the request and once in the durable snapshot;
- require each item to be an `upsert`;
- apply normal Schema v1 validation, source-revision ordering, tombstone
  semantics, and per-UID idempotency;
- record the item UID and successful application in the durable session;
- return the normal complete per-UID result list.

An exact item-phase retry returns the cached receipt. A changed payload under
the same phase key is an idempotency conflict.

The response must include a snapshot control object, for example:

```json
{
  "schema_version": 1,
  "request_id": "...",
  "target_alias": "work",
  "snapshot": {
    "snapshot_id": "...",
    "phase": "items",
    "sequence": 1,
    "status": "receiving",
    "received_count": 25,
    "acknowledged_count": 25
  },
  "overall_status": "success",
  "results": [
    {"uid": "...", "operation": "upsert", "status": "applied"}
  ]
}
```

## Finalization requirements

Reject `finalize` unless all of the following hold:

- snapshot session exists and was opened;
- snapshot is not superseded by a newer snapshot;
- all expected item sequences are present and non-contradictory;
- received item count equals `expected_item_count`;
- the durable received UID digest equals `uid_digest`;
- every received item is terminally applied, already applied, or
  stale-ignored according to ordinary Schema v1 semantics;
- no item is still queued, retryable, or unresolved;
- the final request's expected count and digest match the session.

Return these machine-readable failures as appropriate:

- `SNAPSHOT_NOT_FOUND`
- `SNAPSHOT_INCOMPLETE`
- `SNAPSHOT_DIGEST_MISMATCH`
- `SNAPSHOT_SEQUENCE_INVALID`
- `SNAPSHOT_SUPERSEDED`
- `SNAPSHOT_FINALIZE_CONFLICT`

Finalization must use one backend transaction (or an equivalent atomic
database boundary) to:

1. Reconfirm that every snapshot item is terminally applied.
2. Confirm the snapshot generation is still the newest generation for the
   target alias.
3. Delete every target-calendar event whose UID is not in the snapshot UID
   set.
4. Write deletion tombstones using the authoritative snapshot generation and a
   revision/generation ordering value that cannot be defeated by delayed
   incremental messages.
5. Mark the snapshot complete and record deletion count.
6. Commit the entire cutover atomically.

If any precondition fails, do not delete anything. Return
`SNAPSHOT_FINALIZE_CONFLICT` or the more specific snapshot error.

An older snapshot must never delete events after a newer snapshot has begun or
completed. Incremental updates arriving after successful finalization must
continue to apply normally under source-revision ordering.

Finalization response example:

```json
{
  "schema_version": 1,
  "request_id": "...",
  "target_alias": "work",
  "snapshot": {
    "snapshot_id": "...",
    "phase": "finalize",
    "status": "completed",
    "received_count": 42,
    "acknowledged_count": 42,
    "deleted_uid_count": 3
  },
  "overall_status": "success",
  "results": []
}
```

## Error, auth, and retry behavior

Preserve the ordinary Schema v1 policy:

- `401` / `403`: authentication or authorization failure; no snapshot
  mutation should be acknowledged.
- `429`, `500`, `503`: retryable, with `Retry-After` where available.
- `400`, `404`, `409`, `422`: permanent or operator-action failures.
- Transport timeouts: the client retries the exact same request.

Snapshot control errors must include a stable `code`, safe `message`, request
ID, target alias, snapshot ID, and phase. Do not return bearer tokens or
sensitive event contents in diagnostics.

## Concurrency and incremental compatibility

- Allow at most one active snapshot generation per target alias.
- A new `begin` supersedes the prior active generation without deleting target
  data.
- Finalize checks the active generation immediately before deletion.
- Ordinary incremental upserts/removals may continue to arrive, but the
  backend must serialize them against finalization so a post-finalization
  change is not lost.
- After finalization, ordinary messages continue to use UID/source-revision
  ordering and deletion tombstones.

## Metrics, logs, and documentation

Add metrics and structured logs for:

- snapshot start/begin;
- target alias and snapshot ID;
- number of item batches;
- received and acknowledged item count;
- finalize attempts and completions;
- deleted UID count;
- blocked/incomplete finalizations;
- digest or sequence failures;
- superseded snapshots;
- idempotency receipt hits/conflicts;
- elapsed capture-to-finalize duration.

Logs should include request ID and snapshot ID but never event bodies or
credentials.

Update the backend integration guide with:

- the full request/response examples above;
- exact idempotency behavior;
- `replace_all` destructive-scope warning;
- lifecycle and failure state diagrams;
- operator recovery steps for blocked finalization.

## Required backend tests

Add tests for:

- begin/item/finalize happy path;
- empty snapshot begin/finalize, which deletes all target events;
- exact retry returning the same receipt;
- changed payload reusing a phase key returning
  `IDEMPOTENCY_CONFLICT`;
- missing or contradictory sequences;
- missing/extra/duplicate UIDs;
- digest mismatch;
- incomplete item processing;
- unknown/superseded snapshots;
- older snapshots unable to delete after newer begin/complete;
- atomic deletion and tombstone creation;
- incremental updates after finalization;
- existing ordinary Schema v1 mixed traffic remaining unchanged.
