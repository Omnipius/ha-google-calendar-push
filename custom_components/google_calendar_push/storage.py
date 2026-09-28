"""Durable storage manager for ha-google-calendar-push."""

import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    STORAGE_VERSION,
    STORAGE_KEY_PREFIX,
    STATUS_APPLIED,
    STATUS_ALREADY_APPLIED,
    STATUS_STALE_IGNORED,
    OPERATION_REMOVE,
    OPERATION_UPSERT,
    DEFAULT_IDEMPOTENCY_TTL_DAYS,
    DEFAULT_TOMBSTONE_RETENTION_DAYS,
    DEFAULT_SNAPSHOT_TTL_DAYS,
    SNAPSHOT_MODE_REPLACE_ALL,
    SNAPSHOT_STATUS_OPEN,
    SNAPSHOT_STATUS_RECEIVING,
    SNAPSHOT_STATUS_COMPLETED,
    SNAPSHOT_STATUS_SUPERSEDED,
    SNAPSHOT_STATUS_FAILED,
    CODE_SNAPSHOT_NOT_FOUND,
    CODE_SNAPSHOT_INCOMPLETE,
    CODE_SNAPSHOT_DIGEST_MISMATCH,
    CODE_SNAPSHOT_SEQUENCE_INVALID,
    CODE_SNAPSHOT_SUPERSEDED,
    CODE_SNAPSHOT_FINALIZE_CONFLICT,
    CODE_IDEMPOTENCY_CONFLICT,
    CODE_INVALID_REQUEST,
    CODE_INTERNAL_ERROR,
)

_LOGGER = logging.getLogger(__name__)

def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 string or datetime into UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        clean_str = re.sub(r'\[.*?\]$', '', value.strip())
        try:
            dt = datetime.fromisoformat(clean_str.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

def compare_revisions(rev_a: Optional[str], rev_b: Optional[str]) -> Optional[int]:
    """
    Compare two revisions.
    Returns:
      1 if rev_a > rev_b
      0 if rev_a == rev_b
      -1 if rev_a < rev_b
      None if either is None or uncomparable
    """
    if rev_a is None or rev_b is None:
        return None
    if rev_a == rev_b:
        return 0

    # Try datetime comparison first
    dt_a = parse_iso_datetime(rev_a)
    dt_b = parse_iso_datetime(rev_b)
    if dt_a is not None and dt_b is not None:
        if dt_a > dt_b:
            return 1
        elif dt_a < dt_b:
            return -1
        return 0

    # Try numeric comparison
    try:
        num_a = float(rev_a)
        num_b = float(rev_b)
        if num_a > num_b:
            return 1
        elif num_a < num_b:
            return -1
        return 0
    except ValueError:
        pass

    # Fallback to string comparison
    if rev_a > rev_b:
        return 1
    elif rev_a < rev_b:
        return -1
    return 0


def normalize_digest(digest: Optional[str]) -> Optional[str]:
    """Normalize a SHA-256 digest string by stripping prefix and whitespace and lowercasing."""
    if not digest:
        return None
    d = str(digest).strip().lower()
    if d.startswith("sha256:"):
        d = d[7:]
    return d


def compute_uid_digest(uids: Any) -> str:
    """
    Compute canonical SHA-256 UID digest.
    Sorted lexicographically, deduplicated, joined by newline (no trailing newline).
    Empty list produces SHA-256 of empty string (e3b0c442...).
    """
    unique_sorted = sorted(set(uids or []))
    content = "\n".join(unique_sorted)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class PushStorageManager:
    """Manages persistent idempotency records, event revisions, and tombstones."""

    def __init__(self, hass: HomeAssistant, entry_id: str):
        self.hass = hass
        self.entry_id = entry_id
        storage_key = f"{STORAGE_KEY_PREFIX}_{entry_id}"
        self._store = Store(hass, STORAGE_VERSION, storage_key)
        self._lock = asyncio.Lock()
        self._data: Dict[str, Any] = {
            "idempotency": {},
            "events": {},
            "snapshots": {},
            "active_snapshots": {},
        }
        self._loaded = False

    async def async_load(self) -> None:
        """Load data from Home Assistant storage."""
        async with self._lock:
            stored = await self._store.async_load()
            if stored and isinstance(stored, dict):
                self._data = {
                    "idempotency": stored.get("idempotency", {}),
                    "events": stored.get("events", {}),
                    "snapshots": stored.get("snapshots", {}),
                    "active_snapshots": stored.get("active_snapshots", {}),
                }
            else:
                self._data = {
                    "idempotency": {},
                    "events": {},
                    "snapshots": {},
                    "active_snapshots": {},
                }
            self._prune_expired_locked()
            self._loaded = True

    async def async_save(self) -> None:
        """Persist data to Home Assistant storage."""
        async with self._lock:
            self._prune_expired_locked()
            await self._store.async_save(self._data)

    def _event_key(self, alias: str, uid: str) -> str:
        return f"{alias}:{uid}"

    def _prune_expired_locked(self) -> None:
        """Prune expired idempotency receipts, tombstones, and snapshots (must hold lock)."""
        now = datetime.now(timezone.utc)
        idemp_cutoff = now - timedelta(days=DEFAULT_IDEMPOTENCY_TTL_DAYS)
        tombstone_cutoff = now - timedelta(days=DEFAULT_TOMBSTONE_RETENTION_DAYS)
        snapshot_cutoff = now - timedelta(days=DEFAULT_SNAPSHOT_TTL_DAYS)

        # Prune idempotency receipts
        idemp = self._data.get("idempotency", {})
        expired_idemp = []
        for key, record in idemp.items():
            created_at = parse_iso_datetime(record.get("created_at"))
            if created_at and created_at < idemp_cutoff:
                expired_idemp.append(key)
        for k in expired_idemp:
            idemp.pop(k, None)

        # Prune tombstones
        events = self._data.get("events", {})
        expired_events = []
        for key, record in events.items():
            if record.get("status") == "tombstone":
                tombstoned_at = parse_iso_datetime(record.get("tombstoned_at") or record.get("updated_at"))
                if tombstoned_at and tombstoned_at < tombstone_cutoff:
                    expired_events.append(key)
        for k in expired_events:
            events.pop(k, None)

        # Prune snapshot sessions
        snapshots = self._data.get("snapshots", {})
        expired_snaps = []
        for sid, snap in snapshots.items():
            updated_at = parse_iso_datetime(snap.get("updated_at") or snap.get("created_at"))
            if updated_at and updated_at < snapshot_cutoff:
                expired_snaps.append(sid)
        for sid in expired_snaps:
            snapshots.pop(sid, None)

        active = self._data.get("active_snapshots", {})
        for alias, sid in list(active.items()):
            if sid in expired_snaps:
                active.pop(alias, None)

    async def get_idempotency(self, idempotency_key: str) -> Optional[dict]:
        """Retrieve an idempotency receipt if it exists and has not expired."""
        if not idempotency_key:
            return None
        async with self._lock:
            record = self._data.get("idempotency", {}).get(idempotency_key)
            if not record:
                return None
            created_at = parse_iso_datetime(record.get("created_at"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_IDEMPOTENCY_TTL_DAYS)
            if created_at and created_at < cutoff:
                self._data["idempotency"].pop(idempotency_key, None)
                return None
            return record

    async def record_idempotency(
        self,
        idempotency_key: str,
        payload_hash: str,
        target_alias: str,
        request_id: str,
        status_code: int,
        response_data: dict,
    ) -> None:
        """Store an idempotency receipt."""
        if not idempotency_key:
            return
        async with self._lock:
            self._data.setdefault("idempotency", {})[idempotency_key] = {
                "payload_hash": payload_hash,
                "target_alias": target_alias,
                "request_id": request_id,
                "status_code": status_code,
                "response_data": response_data,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        await self.async_save()

    async def check_revision_and_status(
        self,
        alias: str,
        uid: str,
        incoming_rev: Optional[str],
        incoming_op: str,
        payload_hash: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Evaluate an incoming operation against existing revision state and tombstones.

        Returns:
            Tuple of:
                should_execute: bool (whether downstream Google API mutation should execute)
                status: str (applied, already_applied, stale_ignored)
                message: Optional[str] (diagnostic reason if ignored/already applied)
        """
        key = self._event_key(alias, uid)
        async with self._lock:
            record = self._data.get("events", {}).get(key)
            if not record:
                # No recorded state yet: must execute against downstream
                return True, STATUS_APPLIED, None

            stored_status = record.get("status")  # "active" or "tombstone"
            stored_rev = record.get("latest_source_revision")
            stored_hash = record.get("last_payload_hash")

            # Handle tombstone state (event was previously deleted)
            if stored_status == "tombstone":
                if incoming_op == OPERATION_REMOVE:
                    # Deleting a tombstoned event is idempotent
                    return False, STATUS_ALREADY_APPLIED, "Event is already deleted."

                # Incoming upsert on a tombstone
                if incoming_rev is None:
                    # Without a revision, protect the tombstone against delayed replays
                    return False, STATUS_STALE_IGNORED, "Event was previously deleted. Provide a newer source_revision to recreate."

                comp = compare_revisions(incoming_rev, stored_rev)
                if comp is not None and comp <= 0:
                    # Stale or equal revision cannot resurrect a deleted event
                    return False, STATUS_STALE_IGNORED, "Event was deleted by a newer or equal source_revision."

                # Newer revision legitimately resurrects/recreates the event
                return True, STATUS_APPLIED, None

            # Handle active state
            if stored_status == "active":
                if incoming_rev is not None and stored_rev is not None:
                    comp = compare_revisions(incoming_rev, stored_rev)
                    if comp is not None:
                        if comp < 0:
                            # Older revision arriving after newer revision
                            return False, STATUS_STALE_IGNORED, "Received revision is older than the current event revision."
                        elif comp == 0:
                            # Equal revision: idempotent replay
                            if incoming_op == OPERATION_REMOVE:
                                return True, STATUS_APPLIED, None
                            # If payload matches or revision is equal, consider already applied
                            return False, STATUS_ALREADY_APPLIED, "Event has already been applied at this source_revision."

                # Check if identical payload was already applied
                if payload_hash and stored_hash and payload_hash == stored_hash and incoming_op != OPERATION_REMOVE:
                    return False, STATUS_ALREADY_APPLIED, "Identical event payload has already been applied."

                return True, STATUS_APPLIED, None

        return True, STATUS_APPLIED, None

    async def record_event_mutation(
        self,
        alias: str,
        uid: str,
        status: str,  # "active" or "tombstone"
        source_revision: Optional[str] = None,
        payload_hash: Optional[str] = None,
        google_event_id: Optional[str] = None,
    ) -> None:
        """Update or record state for an event or tombstone."""
        key = self._event_key(alias, uid)
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            events = self._data.setdefault("events", {})
            existing = events.get(key, {})

            # Preserve latest source revision if existing is newer
            best_rev = source_revision
            existing_rev = existing.get("latest_source_revision")
            if existing_rev and source_revision:
                comp = compare_revisions(source_revision, existing_rev)
                if comp is not None and comp < 0:
                    best_rev = existing_rev
            elif not best_rev:
                best_rev = existing_rev

            record = {
                "uid": uid,
                "alias": alias,
                "status": status,
                "latest_source_revision": best_rev,
                "last_payload_hash": payload_hash or existing.get("last_payload_hash"),
                "google_event_id": google_event_id or existing.get("google_event_id"),
                "updated_at": now_iso,
            }
            if status == "tombstone":
                record["tombstoned_at"] = existing.get("tombstoned_at") or now_iso

            events[key] = record

    async def get_tombstones_count(self, alias: Optional[str] = None) -> int:
        """Count active tombstones, optionally filtered by calendar alias."""
        async with self._lock:
            events = self._data.get("events", {})
            count = 0
            for record in events.values():
                if record.get("status") == "tombstone":
                    if alias is None or record.get("alias") == alias:
                        count += 1
            return count

    async def begin_snapshot(
        self,
        snapshot_id: str,
        alias: str,
        mode: str,
        expected_item_count: int,
        expected_uid_digest: str,
        request_id: Optional[str] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        Begin or resume a snapshot reconciliation session.
        Returns: (snapshot_dict, error_code, error_message)
        """
        norm_digest = normalize_digest(expected_uid_digest)
        now_iso = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            snapshots = self._data.setdefault("snapshots", {})
            active_snapshots = self._data.setdefault("active_snapshots", {})

            if snapshot_id in snapshots:
                existing = snapshots[snapshot_id]
                # Check for idempotent replay
                if (
                    existing.get("alias") == alias
                    and existing.get("mode") == mode
                    and existing.get("expected_item_count") == expected_item_count
                    and existing.get("expected_uid_digest") == norm_digest
                ):
                    return existing, None, None
                return existing, CODE_SNAPSHOT_FINALIZE_CONFLICT, "Snapshot session already exists with different configuration."

            # Mark any currently active snapshot for this alias as superseded
            active_id = active_snapshots.get(alias)
            if active_id and active_id in snapshots:
                active_snap = snapshots[active_id]
                if active_snap.get("status") in (SNAPSHOT_STATUS_OPEN, SNAPSHOT_STATUS_RECEIVING):
                    active_snap["status"] = SNAPSHOT_STATUS_SUPERSEDED
                    active_snap["superseded_by"] = snapshot_id
                    active_snap["updated_at"] = now_iso

            record = {
                "snapshot_id": snapshot_id,
                "alias": alias,
                "mode": mode,
                "status": SNAPSHOT_STATUS_OPEN,
                "expected_item_count": expected_item_count,
                "expected_uid_digest": norm_digest,
                "received_uids": [],
                "unresolved_uids": [],
                "processed_sequences": [],
                "superseded_by": None,
                "created_at": now_iso,
                "updated_at": now_iso,
                "completed_at": None,
                "deleted_count": 0,
                "target_total_before": 0,
                "target_total_after": 0,
                "error": None,
            }
            snapshots[snapshot_id] = record
            active_snapshots[alias] = snapshot_id

        await self.async_save()
        return record, None, None

    async def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve snapshot record by id."""
        async with self._lock:
            return self._data.get("snapshots", {}).get(snapshot_id)

    async def get_active_snapshot(self, alias: str) -> Optional[Dict[str, Any]]:
        """Retrieve active snapshot record for alias."""
        async with self._lock:
            active_id = self._data.get("active_snapshots", {}).get(alias)
            if active_id:
                return self._data.get("snapshots", {}).get(active_id)
            return None

    async def validate_items_phase(
        self,
        snapshot_id: str,
        alias: str,
        sequence: int,
        is_final: bool,
        incoming_uids: List[str],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """Validate preconditions for snapshot items phase chunk."""
        async with self._lock:
            snap = self._data.get("snapshots", {}).get(snapshot_id)
            if not snap:
                return None, CODE_SNAPSHOT_NOT_FOUND, f"Snapshot session '{snapshot_id}' does not exist."
            if snap.get("alias") != alias:
                return snap, CODE_INVALID_REQUEST, f"Snapshot session belongs to alias '{snap.get('alias')}', not '{alias}'."
            if snap.get("status") == SNAPSHOT_STATUS_SUPERSEDED or snap.get("superseded_by"):
                return snap, CODE_SNAPSHOT_SUPERSEDED, f"Snapshot session has been superseded by '{snap.get('superseded_by')}'."
            if snap.get("status") == SNAPSHOT_STATUS_COMPLETED:
                return snap, CODE_SNAPSHOT_FINALIZE_CONFLICT, "Snapshot session has already completed."
            if snap.get("status") == SNAPSHOT_STATUS_FAILED:
                return snap, CODE_INTERNAL_ERROR, f"Snapshot session is in failed state: {snap.get('error')}."

            processed_seqs = snap.get("processed_sequences", [])
            expected_seq = len(processed_seqs) + 1
            if sequence != expected_seq:
                return snap, CODE_SNAPSHOT_SEQUENCE_INVALID, f"Expected sequence {expected_seq}, received {sequence}."

            return snap, None, None

    async def record_snapshot_items(
        self,
        snapshot_id: str,
        sequence: int,
        uids: List[str],
        failed_uids: Optional[List[str]] = None,
        succeeded_uids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record processed items chunk, append unique UIDs, and track unresolved items."""
        async with self._lock:
            snap = self._data.get("snapshots", {}).get(snapshot_id)
            if not snap:
                raise ValueError(f"Snapshot {snapshot_id} not found")
            processed_seqs = snap.setdefault("processed_sequences", [])
            if sequence not in processed_seqs:
                processed_seqs.append(sequence)

            current_uids = set(snap.get("received_uids", []))
            current_uids.update(uids)
            snap["received_uids"] = sorted(list(current_uids))

            unresolved = set(snap.get("unresolved_uids", []))
            if failed_uids:
                unresolved.update(failed_uids)
            if succeeded_uids:
                unresolved.difference_update(succeeded_uids)
            snap["unresolved_uids"] = sorted(list(unresolved))

            snap["status"] = SNAPSHOT_STATUS_RECEIVING
            snap["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self.async_save()
        return snap

    async def validate_finalize_phase(
        self,
        snapshot_id: str,
        alias: str,
        total_items: Optional[int],
        uid_digest: Optional[str],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
        """Validate preconditions for snapshot finalize phase."""
        async with self._lock:
            snap = self._data.get("snapshots", {}).get(snapshot_id)
            if not snap:
                return None, CODE_SNAPSHOT_NOT_FOUND, f"Snapshot session '{snapshot_id}' does not exist."
            if snap.get("alias") != alias:
                return snap, CODE_INVALID_REQUEST, f"Snapshot session belongs to alias '{snap.get('alias')}', not '{alias}'."
            if snap.get("status") == SNAPSHOT_STATUS_SUPERSEDED or snap.get("superseded_by"):
                return snap, CODE_SNAPSHOT_SUPERSEDED, f"Snapshot session has been superseded by '{snap.get('superseded_by')}'."

            # Idempotent finalize on already completed snapshot
            if snap.get("status") == SNAPSHOT_STATUS_COMPLETED:
                return snap, None, None

            if snap.get("status") == SNAPSHOT_STATUS_FAILED:
                return snap, CODE_INTERNAL_ERROR, f"Snapshot session is in failed state: {snap.get('error')}."

            unresolved = snap.get("unresolved_uids", [])
            if unresolved:
                return snap, CODE_SNAPSHOT_FINALIZE_CONFLICT, f"{len(unresolved)} items failed during ingestion and must be resolved before finalization."

            expected_count = snap.get("expected_item_count", 0)
            received_uids = snap.get("received_uids", [])
            received_count = len(received_uids)

            if total_items is not None and total_items != expected_count:
                return snap, CODE_SNAPSHOT_INCOMPLETE, f"Finalize total_items ({total_items}) does not match expected_item_count ({expected_count})."

            if received_count != expected_count:
                return snap, CODE_SNAPSHOT_INCOMPLETE, f"Snapshot incomplete: expected {expected_count} items, received {received_count}."

            expected_digest = snap.get("expected_uid_digest")
            actual_digest = compute_uid_digest(received_uids)

            if actual_digest != expected_digest:
                return snap, CODE_SNAPSHOT_DIGEST_MISMATCH, f"UID digest mismatch: expected '{expected_digest}', computed '{actual_digest}'."

            if uid_digest is not None:
                norm_finalize_digest = normalize_digest(uid_digest)
                if norm_finalize_digest != actual_digest:
                    return snap, CODE_SNAPSHOT_DIGEST_MISMATCH, f"UID digest mismatch: finalize specified '{norm_finalize_digest}', computed '{actual_digest}'."

            return snap, None, None

    async def complete_snapshot(
        self,
        snapshot_id: str,
        deleted_count: int,
        target_total_before: int,
        target_total_after: int,
    ) -> Dict[str, Any]:
        """Mark snapshot session completed and record reconciliation counts."""
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            snap = self._data.get("snapshots", {}).get(snapshot_id)
            if not snap:
                raise ValueError(f"Snapshot {snapshot_id} not found")
            snap["status"] = SNAPSHOT_STATUS_COMPLETED
            snap["deleted_count"] = deleted_count
            snap["target_total_before"] = target_total_before
            snap["target_total_after"] = target_total_after
            snap["completed_at"] = now_iso
            snap["updated_at"] = now_iso
        await self.async_save()
        return snap

    async def fail_snapshot(self, snapshot_id: str, error_message: str) -> None:
        """Mark a snapshot as failed."""
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            snap = self._data.get("snapshots", {}).get(snapshot_id)
            if snap:
                snap["status"] = SNAPSHOT_STATUS_FAILED
                snap["error"] = error_message
                snap["updated_at"] = now_iso
        await self.async_save()

    async def get_latest_snapshot(self, alias: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the most recently updated snapshot record for an alias."""
        async with self._lock:
            snaps = self._data.get("snapshots", {})
            matching = [
                s for s in snaps.values()
                if alias is None or s.get("alias") == alias
            ]
            if not matching:
                return None
            matching.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "", reverse=True)
            return matching[0]
