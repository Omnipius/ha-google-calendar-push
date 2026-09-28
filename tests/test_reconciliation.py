"""Unit tests for Schema v1 Full-Calendar Reconciliation (replace_all mode)."""

import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import patch, MagicMock
import pytest
from httplib2 import Response

from custom_components.google_calendar_push.api import GoogleCalendarPushView
from custom_components.google_calendar_push.storage import PushStorageManager, compute_uid_digest
from custom_components.google_calendar_push.const import (
    CODE_IDEMPOTENCY_CONFLICT,
    CODE_INVALID_REQUEST,
    CODE_SNAPSHOT_DIGEST_MISMATCH,
    CODE_SNAPSHOT_FINALIZE_CONFLICT,
    CODE_SNAPSHOT_INCOMPLETE,
    CODE_SNAPSHOT_NOT_FOUND,
    CODE_SNAPSHOT_SEQUENCE_INVALID,
    CODE_SNAPSHOT_SUPERSEDED,
    OVERALL_ERROR,
    OVERALL_PARTIAL,
    OVERALL_SUCCESS,
    SCHEMA_VERSION,
    SNAPSHOT_MODE_REPLACE_ALL,
    SNAPSHOT_PHASE_BEGIN,
    SNAPSHOT_PHASE_FINALIZE,
    SNAPSHOT_PHASE_ITEMS,
    SNAPSHOT_STATUS_COMPLETED,
    SNAPSHOT_STATUS_OPEN,
    SNAPSHOT_STATUS_RECEIVING,
    SNAPSHOT_STATUS_SUPERSEDED,
    STATUS_APPLIED,
    STATUS_REJECTED,
)
from tests.conftest import MockHass, MockOAuthSession, MockRequest, MockGoogleService, MockStore

@pytest.fixture
def test_setup():
    hass = MockHass()
    session = MockOAuthSession()
    with patch("custom_components.google_calendar_push.storage.Store", MockStore):
        storage = PushStorageManager(hass, "test_entry")
        asyncio.run(storage.async_load())

    view = GoogleCalendarPushView(
        hass=hass,
        session=session,
        calendar_aliases={"work": "primary_cal_id"},
        storage=storage,
    )
    google_service = MockGoogleService()
    # Default list behavior: empty calendar
    google_service._events_mock.list.return_value.execute.return_value = {"items": []}
    view._get_google_service = lambda: google_service
    return hass, session, storage, view, google_service

@pytest.mark.asyncio
async def test_reconciliation_happy_path(test_setup):
    """Test full reconciliation lifecycle: begin -> items -> finalize with cutover deletion."""
    hass, session, storage, view, google_service = test_setup

    uids = ["work-event-1", "work-event-2"]
    digest = compute_uid_digest(uids)

    # 1. Phase: begin
    begin_payload = {
        "schema_version": 1,
        "request_id": "req-begin-1",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-001",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 2,
            "expected_uid_digest": digest,
        }
    }
    resp = await view.post(MockRequest(begin_payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["overall_status"] == OVERALL_SUCCESS
    assert data["snapshot"]["status"] == SNAPSHOT_STATUS_OPEN
    assert data["snapshot"]["expected_item_count"] == 2
    assert data["snapshot"]["received_item_count"] == 0

    # 2. Phase: items (sequence 1)
    items_payload = {
        "schema_version": 1,
        "request_id": "req-items-1",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-001",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 1,
            "is_final": True,
        },
        "items": [
            {
                "uid": "work-event-1",
                "operation": "upsert",
                "source_revision": "2026-09-28T09:00:00Z",
                "summary": "Team Sync",
                "dtstart": "2026-09-28T10:00:00Z",
                "dtend": "2026-09-28T10:30:00Z",
            },
            {
                "uid": "work-event-2",
                "operation": "upsert",
                "source_revision": "2026-09-28T09:00:00Z",
                "summary": "Project Review",
                "dtstart": "2026-09-28T14:00:00Z",
                "dtend": "2026-09-28T15:00:00Z",
            }
        ]
    }
    resp = await view.post(MockRequest(items_payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["overall_status"] == OVERALL_SUCCESS
    assert data["snapshot"]["status"] == SNAPSHOT_STATUS_RECEIVING
    assert data["snapshot"]["received_item_count"] == 2
    assert data["events_processed"] == 2

    # 3. Target calendar has 1 stale event and 1 matching event before finalize
    google_service._events_mock.list.return_value.execute.return_value = {
        "items": [
            {"id": "g_stale", "iCalUID": "old-stale-event"},
            {"id": "g_keep", "iCalUID": "work-event-1"},
        ]
    }

    # 4. Phase: finalize
    finalize_payload = {
        "schema_version": 1,
        "request_id": "req-finalize-1",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-001",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_FINALIZE,
            "total_items": 2,
            "uid_digest": f"sha256:{digest}",
        }
    }
    resp = await view.post(MockRequest(finalize_payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["overall_status"] == OVERALL_SUCCESS
    assert data["snapshot"]["status"] == SNAPSHOT_STATUS_COMPLETED
    assert "reconciliation" in data
    recon = data["reconciliation"]
    assert recon["target_total_before"] == 2
    assert recon["items_applied"] == 2
    assert recon["deleted_count"] == 1
    assert recon["target_total_after"] == 1

    # Verify tombstone was created for stale event
    t_count = await storage.get_tombstones_count("work")
    assert t_count == 1

@pytest.mark.asyncio
async def test_reconciliation_empty_snapshot(test_setup):
    """Test empty snapshot reconciliation (deletes all events on target calendar)."""
    hass, session, storage, view, google_service = test_setup

    empty_digest = compute_uid_digest([])
    assert empty_digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # Target calendar currently has 2 events
    google_service._events_mock.list.return_value.execute.return_value = {
        "items": [
            {"id": "g1", "iCalUID": "old-1"},
            {"id": "g2", "iCalUID": "old-2"},
        ]
    }

    # Begin with 0 items
    begin_payload = {
        "schema_version": 1,
        "request_id": "req-empty-begin",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-empty",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 0,
            "expected_uid_digest": empty_digest,
        }
    }
    resp = await view.post(MockRequest(begin_payload), "work")
    assert resp.status == 200

    # Finalize with total_items: 0 (skipping items phase)
    finalize_payload = {
        "schema_version": 1,
        "request_id": "req-empty-fin",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-empty",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_FINALIZE,
            "total_items": 0,
            "uid_digest": empty_digest,
        }
    }
    resp = await view.post(MockRequest(finalize_payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["snapshot"]["status"] == SNAPSHOT_STATUS_COMPLETED
    assert data["reconciliation"]["deleted_count"] == 2
    assert data["reconciliation"]["target_total_after"] == 0

@pytest.mark.asyncio
async def test_reconciliation_exact_retries_and_idempotent_finalize(test_setup):
    """Test idempotent replay of begin, items, and finalize."""
    hass, session, storage, view, google_service = test_setup

    digest = compute_uid_digest(["ev-1"])

    # 1. Begin with Idempotency-Key
    begin_payload = {
        "schema_version": 1,
        "request_id": "req-b-1",
        "idempotency_key": "idemp-snap-begin",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-retry",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 1,
            "expected_uid_digest": digest,
        }
    }
    resp1 = await view.post(MockRequest(begin_payload), "work")
    assert resp1.status == 200

    # Replay begin with same key -> cached 200
    resp2 = await view.post(MockRequest(begin_payload), "work")
    assert resp2.status == 200

    # 2. Items
    items_payload = {
        "schema_version": 1,
        "request_id": "req-i-1",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-retry",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 1,
            "is_final": True,
        },
        "items": [
            {
                "uid": "ev-1",
                "operation": "upsert",
                "summary": "Meeting",
                "dtstart": "2026-09-28T10:00:00Z",
                "dtend": "2026-09-28T11:00:00Z",
            }
        ]
    }
    resp_items = await view.post(MockRequest(items_payload), "work")
    assert resp_items.status == 200

    # 3. Finalize
    fin_payload = {
        "schema_version": 1,
        "request_id": "req-f-1",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-retry",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_FINALIZE,
            "total_items": 1,
            "uid_digest": digest,
        }
    }
    resp_fin1 = await view.post(MockRequest(fin_payload), "work")
    assert resp_fin1.status == 200
    data_fin1 = json.loads(resp_fin1.body.decode())
    assert data_fin1["snapshot"]["status"] == SNAPSHOT_STATUS_COMPLETED

    # 4. Replay Finalize without key -> returns 200 with completed snapshot state
    resp_fin2 = await view.post(MockRequest(fin_payload), "work")
    assert resp_fin2.status == 200
    data_fin2 = json.loads(resp_fin2.body.decode())
    assert data_fin2["snapshot"]["status"] == SNAPSHOT_STATUS_COMPLETED
    assert data_fin2["reconciliation"] == data_fin1["reconciliation"]

@pytest.mark.asyncio
async def test_reconciliation_sequence_validation(test_setup):
    """Test sequence validation in items phase: skipped sequence or duplicates rejected."""
    hass, session, storage, view, google_service = test_setup

    digest = compute_uid_digest(["ev-1", "ev-2"])

    # Begin
    await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-b",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-seq",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 2,
            "expected_uid_digest": digest,
        }
    }), "work")

    # Send sequence 2 directly (skipped sequence 1)
    bad_seq_payload = {
        "schema_version": 1,
        "request_id": "req-seq-2",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-seq",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 2,
            "is_final": True,
        },
        "items": [
            {
                "uid": "ev-2",
                "operation": "upsert",
                "summary": "M2",
                "dtstart": "2026-09-28T10:00:00Z",
                "dtend": "2026-09-28T11:00:00Z",
            }
        ]
    }
    resp = await view.post(MockRequest(bad_seq_payload), "work")
    assert resp.status == 400
    data = json.loads(resp.body.decode())
    assert data["errors"][0]["code"] == CODE_SNAPSHOT_SEQUENCE_INVALID
    assert "Expected sequence 1, received 2" in data["errors"][0]["message"]

    # Send sequence 1 -> succeeds
    seq1_payload = {
        "schema_version": 1,
        "request_id": "req-seq-1",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-seq",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 1,
            "is_final": False,
        },
        "items": [
            {
                "uid": "ev-1",
                "operation": "upsert",
                "summary": "M1",
                "dtstart": "2026-09-28T09:00:00Z",
                "dtend": "2026-09-28T10:00:00Z",
            }
        ]
    }
    resp1 = await view.post(MockRequest(seq1_payload), "work")
    assert resp1.status == 200

    # Repeat sequence 1 without idempotency key -> rejected as invalid sequence (expects sequence 2)
    resp1_repeat = await view.post(MockRequest(seq1_payload), "work")
    assert resp1_repeat.status == 400
    data_repeat = json.loads(resp1_repeat.body.decode())
    assert data_repeat["errors"][0]["code"] == CODE_SNAPSHOT_SEQUENCE_INVALID
    assert "Expected sequence 2, received 1" in data_repeat["errors"][0]["message"]

@pytest.mark.asyncio
async def test_reconciliation_incomplete_and_digest_mismatch(test_setup):
    """Test finalize rejection on item count mismatch and digest mismatch."""
    hass, session, storage, view, google_service = test_setup

    correct_digest = compute_uid_digest(["ev-1", "ev-2"])

    # Begin expecting 2 items
    await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-b",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-mismatch",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 2,
            "expected_uid_digest": correct_digest,
        }
    }), "work")

    # Send only 1 item in sequence 1
    await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-i",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-mismatch",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 1,
            "is_final": True,
        },
        "items": [
            {
                "uid": "ev-1",
                "operation": "upsert",
                "summary": "M1",
                "dtstart": "2026-09-28T09:00:00Z",
                "dtend": "2026-09-28T10:00:00Z",
            }
        ]
    }), "work")

    # Finalize specifying total_items: 2 (expected 2, but only 1 received)
    resp_inc = await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-f",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-mismatch",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_FINALIZE,
            "total_items": 2,
            "uid_digest": correct_digest,
        }
    }), "work")
    assert resp_inc.status == 400
    data_inc = json.loads(resp_inc.body.decode())
    assert data_inc["errors"][0]["code"] == CODE_SNAPSHOT_INCOMPLETE

    # Send sequence 2 with ev-wrong instead of ev-2
    await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-i-2",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-mismatch",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 2,
            "is_final": True,
        },
        "items": [
            {
                "uid": "ev-wrong",
                "operation": "upsert",
                "summary": "M Wrong",
                "dtstart": "2026-09-28T11:00:00Z",
                "dtend": "2026-09-28T12:00:00Z",
            }
        ]
    }), "work")

    # Now item count is 2 (ev-1, ev-wrong), but digest does not match expected_uid_digest
    resp_dig = await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-f-2",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-mismatch",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_FINALIZE,
            "total_items": 2,
            "uid_digest": correct_digest,
        }
    }), "work")
    assert resp_dig.status == 400
    data_dig = json.loads(resp_dig.body.decode())
    assert data_dig["errors"][0]["code"] == CODE_SNAPSHOT_DIGEST_MISMATCH

@pytest.mark.asyncio
async def test_reconciliation_superseded_by_newer_begin(test_setup):
    """Test that a new snapshot session supersedes an in-progress session."""
    hass, session, storage, view, google_service = test_setup

    digest = compute_uid_digest(["ev-1"])

    # 1. Begin snapshot A
    await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-a-b",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-A",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 1,
            "expected_uid_digest": digest,
        }
    }), "work")

    # 2. Begin snapshot B for the same alias
    resp_b = await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-b-b",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-B",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 1,
            "expected_uid_digest": digest,
        }
    }), "work")
    assert resp_b.status == 200

    # 3. Snapshot A attempts items -> rejected with SNAPSHOT_SUPERSEDED
    resp_a_items = await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-a-items",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-A",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 1,
            "is_final": True,
        },
        "items": [
            {
                "uid": "ev-1",
                "operation": "upsert",
                "summary": "M",
                "dtstart": "2026-09-28T10:00:00Z",
                "dtend": "2026-09-28T11:00:00Z",
            }
        ]
    }), "work")
    assert resp_a_items.status == 409
    data_a = json.loads(resp_a_items.body.decode())
    assert data_a["errors"][0]["code"] == CODE_SNAPSHOT_SUPERSEDED
    assert "snap-B" in data_a["errors"][0]["message"]

@pytest.mark.asyncio
async def test_reconciliation_unresolved_items_block_finalize(test_setup):
    """Test that ingestion errors block finalization with SNAPSHOT_FINALIZE_CONFLICT."""
    hass, session, storage, view, google_service = test_setup

    uids = ["ev-valid", "ev-invalid"]
    digest = compute_uid_digest(uids)

    # Begin
    await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-b",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-fail",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_BEGIN,
            "expected_item_count": 2,
            "expected_uid_digest": digest,
        }
    }), "work")

    # Send items chunk where ev-invalid has invalid dtstart (missing datetime structure)
    resp_items = await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-i",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-fail",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_ITEMS,
            "sequence": 1,
            "is_final": True,
        },
        "items": [
            {
                "uid": "ev-valid",
                "operation": "upsert",
                "summary": "Valid Event",
                "dtstart": "2026-09-28T10:00:00Z",
                "dtend": "2026-09-28T11:00:00Z",
            },
            {
                "uid": "ev-invalid",
                "operation": "upsert",
                "summary": "Invalid Event",
                "dtstart": "not-a-datetime",
            }
        ]
    }), "work")
    assert resp_items.status == 207  # Partial success
    data_items = json.loads(resp_items.body.decode())
    assert data_items["results"][0]["status"] == STATUS_APPLIED
    assert data_items["results"][1]["status"] == STATUS_REJECTED

    # Attempt to finalize -> blocked because ev-invalid is unresolved
    resp_fin = await view.post(MockRequest({
        "schema_version": 1,
        "request_id": "req-fin",
        "target_alias": "work",
        "snapshot": {
            "snapshot_id": "snap-fail",
            "mode": SNAPSHOT_MODE_REPLACE_ALL,
            "phase": SNAPSHOT_PHASE_FINALIZE,
            "total_items": 2,
            "uid_digest": digest,
        }
    }), "work")
    assert resp_fin.status == 409
    data_fin = json.loads(resp_fin.body.decode())
    assert data_fin["errors"][0]["code"] == CODE_SNAPSHOT_FINALIZE_CONFLICT
    assert "1 items failed during ingestion and must be resolved before finalization" in data_fin["errors"][0]["message"]
