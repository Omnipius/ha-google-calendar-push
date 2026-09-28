"""Unit tests for storage.py."""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest

from custom_components.google_calendar_push.storage import (
    PushStorageManager,
    parse_iso_datetime,
    compare_revisions,
)
from custom_components.google_calendar_push.const import (
    STATUS_APPLIED,
    STATUS_ALREADY_APPLIED,
    STATUS_STALE_IGNORED,
    OPERATION_REMOVE,
    OPERATION_UPSERT,
)
from tests.conftest import MockHass, MockStore

def test_parse_iso_datetime():
    assert parse_iso_datetime(None) is None
    dt = parse_iso_datetime("2026-09-28T01:00:00Z")
    assert dt == datetime(2026, 9, 28, 1, 0, 0, tzinfo=timezone.utc)

    dt2 = parse_iso_datetime("2026-09-28T01:00:00+00:00[UTC]")
    assert dt2 == datetime(2026, 9, 28, 1, 0, 0, tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    assert parse_iso_datetime(now) == now

def test_compare_revisions():
    assert compare_revisions(None, "2026-01-01") is None
    assert compare_revisions("2026-01-01", None) is None
    assert compare_revisions("2026-09-28T02:00:00Z", "2026-09-28T01:00:00Z") == 1
    assert compare_revisions("2026-09-28T01:00:00Z", "2026-09-28T02:00:00Z") == -1
    assert compare_revisions("2026-09-28T01:00:00Z", "2026-09-28T01:00:00Z") == 0

    # Numeric
    assert compare_revisions("2", "1") == 1
    assert compare_revisions("1", "2") == -1
    assert compare_revisions("2", "2") == 0

@pytest.mark.asyncio
async def test_idempotency_storage():
    hass = MockHass()
    with patch("custom_components.google_calendar_push.storage.Store", MockStore):
        manager = PushStorageManager(hass, "test_entry")
        await manager.async_load()

        # Check non-existent
        assert await manager.get_idempotency("key1") is None

        # Record receipt
        await manager.record_idempotency(
            idempotency_key="key1",
            payload_hash="hash123",
            target_alias="work",
            request_id="req1",
            status_code=200,
            response_data={"status": "success"},
        )

        receipt = await manager.get_idempotency("key1")
        assert receipt is not None
        assert receipt["payload_hash"] == "hash123"
        assert receipt["status_code"] == 200
        assert receipt["response_data"]["status"] == "success"

@pytest.mark.asyncio
async def test_idempotency_ttl_expiration():
    hass = MockHass()
    with patch("custom_components.google_calendar_push.storage.Store", MockStore):
        manager = PushStorageManager(hass, "test_entry")
        await manager.async_load()

        # Seed expired receipt (older than 7 days)
        old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        manager._data["idempotency"]["expired_key"] = {
            "payload_hash": "hash_old",
            "target_alias": "work",
            "request_id": "req_old",
            "status_code": 200,
            "response_data": {},
            "created_at": old_time,
        }

        # Should return None and prune
        assert await manager.get_idempotency("expired_key") is None
        assert "expired_key" not in manager._data["idempotency"]

@pytest.mark.asyncio
async def test_revision_and_tombstone_lifecycle():
    hass = MockHass()
    with patch("custom_components.google_calendar_push.storage.Store", MockStore):
        manager = PushStorageManager(hass, "test_entry")
        await manager.async_load()

        alias = "work"
        uid = "event_1"

        # 1. First event submission with revision 10
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T01:00:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is True
        assert status == STATUS_APPLIED

        # Record the successful mutation
        await manager.record_event_mutation(
            alias, uid, status="active", source_revision="2026-09-28T01:00:00Z"
        )

        # 2. Duplicate submission with same revision (idempotent)
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T01:00:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is False
        assert status == STATUS_ALREADY_APPLIED

        # 3. Older revision arriving after newer (out of order network delivery)
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T00:30:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is False
        assert status == STATUS_STALE_IGNORED

        # 4. Newer revision arriving
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T02:00:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is True
        assert status == STATUS_APPLIED
        await manager.record_event_mutation(
            alias, uid, status="active", source_revision="2026-09-28T02:00:00Z"
        )

        # 5. Removal with newer revision
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T03:00:00Z", incoming_op=OPERATION_REMOVE
        )
        assert should_exec is True
        assert status == STATUS_APPLIED
        await manager.record_event_mutation(
            alias, uid, status="tombstone", source_revision="2026-09-28T03:00:00Z"
        )

        # Tombstones count check
        assert await manager.get_tombstones_count(alias) == 1

        # 6. Delayed older upsert arrives after removal (prevent zombie event!)
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T02:30:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is False
        assert status == STATUS_STALE_IGNORED

        # 7. Equal revision upsert arrives after removal
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T03:00:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is False
        assert status == STATUS_STALE_IGNORED

        # 8. Duplicate removal arrives (idempotent)
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T03:00:00Z", incoming_op=OPERATION_REMOVE
        )
        assert should_exec is False
        assert status == STATUS_ALREADY_APPLIED

        # 9. Legitimate recreation with newer revision
        should_exec, status, msg = await manager.check_revision_and_status(
            alias, uid, incoming_rev="2026-09-28T04:00:00Z", incoming_op=OPERATION_UPSERT
        )
        assert should_exec is True
        assert status == STATUS_APPLIED
        await manager.record_event_mutation(
            alias, uid, status="active", source_revision="2026-09-28T04:00:00Z"
        )
        assert await manager.get_tombstones_count(alias) == 0
