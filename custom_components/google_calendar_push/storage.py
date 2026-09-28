"""Durable storage manager for ha-google-calendar-push."""

import asyncio
from datetime import datetime, timezone, timedelta
import logging
import re
from typing import Any, Dict, Optional, Tuple

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
                }
            else:
                self._data = {"idempotency": {}, "events": {}}
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
        """Prune expired idempotency receipts and tombstones (must hold lock)."""
        now = datetime.now(timezone.utc)
        idemp_cutoff = now - timedelta(days=DEFAULT_IDEMPOTENCY_TTL_DAYS)
        tombstone_cutoff = now - timedelta(days=DEFAULT_TOMBSTONE_RETENTION_DAYS)

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
