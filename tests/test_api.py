"""Comprehensive unit tests for GoogleCalendarPushView covering all 17 scenarios."""

import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import patch, MagicMock
import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from custom_components.google_calendar_push.api import GoogleCalendarPushView
from custom_components.google_calendar_push.storage import PushStorageManager
from custom_components.google_calendar_push.const import (
    CODE_AUTHENTICATION_FAILED,
    CODE_AUTHORIZATION_FAILED,
    CODE_DOWNSTREAM_TIMEOUT,
    CODE_IDEMPOTENCY_CONFLICT,
    CODE_INTERNAL_ERROR,
    CODE_INVALID_EVENT,
    CODE_INVALID_REQUEST,
    CODE_RATE_LIMITED,
    CODE_TARGET_UNAVAILABLE,
    CODE_UNSUPPORTED_RECURRENCE,
    HEADER_IDEMPOTENCY_KEY,
    HEADER_REQUEST_ID,
    HEADER_RETRY_AFTER,
    OPERATION_REMOVE,
    OPERATION_UPSERT,
    OVERALL_ERROR,
    OVERALL_PARTIAL,
    OVERALL_SUCCESS,
    SCHEMA_VERSION,
    STATUS_ALREADY_APPLIED,
    STATUS_APPLIED,
    STATUS_REJECTED,
    STATUS_RETRYABLE,
    STATUS_STALE_IGNORED,
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
    view._get_google_service = lambda: google_service
    return hass, session, storage, view, google_service

@pytest.mark.asyncio
async def test_01_one_item_complete_success(test_setup):
    hass, session, storage, view, google_service = test_setup


    payload = {
        "schema_version": 1,
        "request_id": "req-01",
        "target_alias": "work",
        "items": [
            {
                "uid": "event-1",
                "operation": "upsert",
                "source_revision": "2026-09-28T01:00:00Z",
                "summary": "Team Standup",
                "dtstart": "2026-09-28T09:00:00Z",
                "dtend": "2026-09-28T09:30:00Z",
            }
        ]
    }

    req = MockRequest(payload)
    resp = await view.post(req, "work")

    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["schema_version"] == 1
    assert data["request_id"] == "req-01"
    assert data["target_alias"] == "work"
    assert data["overall_status"] == OVERALL_SUCCESS
    assert data["events_processed"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["uid"] == "event-1"
    assert data["results"][0]["operation"] == OPERATION_UPSERT
    assert data["results"][0]["status"] == STATUS_APPLIED
    assert data["results"][0]["source_revision"] == "2026-09-28T01:00:00Z"

@pytest.mark.asyncio
async def test_02_multi_item_complete_success(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "schema_version": 1,
        "request_id": "req-02",
        "target_alias": "work",
        "items": [
            {
                "uid": "event-1",
                "operation": "upsert",
                "summary": "Meeting 1",
                "dtstart": "2026-09-28T09:00:00Z",
            },
            {
                "uid": "event-2",
                "operation": "upsert",
                "summary": "Meeting 2",
                "dtstart": "2026-09-28T10:00:00Z",
            }
        ]
    }

    req = MockRequest(payload)
    resp = await view.post(req, "work")

    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["overall_status"] == OVERALL_SUCCESS
    assert data["events_processed"] == 2
    assert len(data["results"]) == 2
    assert [r["status"] for r in data["results"]] == [STATUS_APPLIED, STATUS_APPLIED]

@pytest.mark.asyncio
async def test_03_207_partial_success_with_per_uid_breakdown(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "schema_version": 1,
        "request_id": "req-03",
        "target_alias": "work",
        "items": [
            {
                "uid": "valid-uid",
                "operation": "upsert",
                "summary": "Valid meeting",
                "dtstart": "2026-09-28T09:00:00Z",
            },
            {
                "uid": "bad-uid",
                "operation": "upsert",
                "summary": "Bad meeting",
                "dtstart": "invalid-datetime-format-xyz",
            }
        ]
    }

    req = MockRequest(payload)
    resp = await view.post(req, "work")

    assert resp.status == 207  # Multi-Status
    data = json.loads(resp.body.decode())
    assert data["overall_status"] == OVERALL_PARTIAL
    assert data["events_processed"] == 1
    assert len(data["results"]) == 2

    res_valid = next(r for r in data["results"] if r["uid"] == "valid-uid")
    assert res_valid["status"] == STATUS_APPLIED

    res_bad = next(r for r in data["results"] if r["uid"] == "bad-uid")
    assert res_bad["status"] == STATUS_REJECTED
    assert res_bad["code"] == CODE_INVALID_EVENT
    assert res_bad["retryable"] is False

@pytest.mark.asyncio
async def test_04_completeness_check(test_setup):
    hass, session, storage, view, google_service = test_setup

    submitted_uids = ["u1", "u2", "u3"]
    payload = {
        "items": [
            {"uid": u, "operation": "upsert", "summary": f"Meeting {u}", "dtstart": "2026-09-28T09:00:00Z"}
            for u in submitted_uids
        ]
    }

    req = MockRequest(payload)
    resp = await view.post(req, "work")
    data = json.loads(resp.body.decode())

    result_uids = [r["uid"] for r in data["results"]]
    assert result_uids == submitted_uids
    assert len(set(result_uids)) == len(submitted_uids)
    for r in data["results"]:
        assert "status" in r

@pytest.mark.asyncio
async def test_05_duplicate_idempotency_key_replay(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "idempotency_key": "idemp-test-1",
        "items": [
            {"uid": "ev-replay", "operation": "upsert", "summary": "Sync", "dtstart": "2026-09-28T09:00:00Z"}
        ]
    }

    # First request
    req1 = MockRequest(payload, headers={HEADER_IDEMPOTENCY_KEY: "idemp-test-1"})
    resp1 = await view.post(req1, "work")
    assert resp1.status == 200
    data1 = json.loads(resp1.body.decode())

    # Second identical request with same key
    req2 = MockRequest(payload, headers={HEADER_IDEMPOTENCY_KEY: "idemp-test-1"})
    resp2 = await view.post(req2, "work")
    assert resp2.status == 200
    data2 = json.loads(resp2.body.decode())

    # Results match cached output
    assert data1["results"] == data2["results"]
    assert data1["events_processed"] == data2["events_processed"]

@pytest.mark.asyncio
async def test_06_idempotency_key_reuse_different_payload(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload1 = {
        "idempotency_key": "conflict-key",
        "items": [{"uid": "ev-1", "operation": "upsert", "summary": "Call A", "dtstart": "2026-09-28T09:00:00Z"}]
    }
    payload2 = {
        "idempotency_key": "conflict-key",
        "items": [{"uid": "ev-1", "operation": "upsert", "summary": "Call B", "dtstart": "2026-09-28T09:00:00Z"}]
    }

    req1 = MockRequest(payload1, headers={HEADER_IDEMPOTENCY_KEY: "conflict-key"})
    resp1 = await view.post(req1, "work")
    assert resp1.status == 200

    req2 = MockRequest(payload2, headers={HEADER_IDEMPOTENCY_KEY: "conflict-key"})
    resp2 = await view.post(req2, "work")
    assert resp2.status == 409  # Conflict
    data2 = json.loads(resp2.body.decode())
    assert data2["errors"][0]["code"] == CODE_IDEMPOTENCY_CONFLICT

@pytest.mark.asyncio
async def test_07_source_revision_ordering_stale_ignored(test_setup):
    hass, session, storage, view, google_service = test_setup

    # Apply newer revision first (rev 2)
    p1 = {
        "items": [{
            "uid": "rev-event",
            "operation": "upsert",
            "source_revision": "2026-09-28T02:00:00Z",
            "summary": "Rev 2",
            "dtstart": "2026-09-28T09:00:00Z"
        }]
    }
    r1 = await view.post(MockRequest(p1), "work")
    assert r1.status == 200
    assert json.loads(r1.body.decode())["results"][0]["status"] == STATUS_APPLIED

    # Delayed older revision arrives (rev 1)
    p2 = {
        "items": [{
            "uid": "rev-event",
            "operation": "upsert",
            "source_revision": "2026-09-28T01:00:00Z",
            "summary": "Rev 1",
            "dtstart": "2026-09-28T09:00:00Z"
        }]
    }
    r2 = await view.post(MockRequest(p2), "work")
    assert r2.status == 200
    data2 = json.loads(r2.body.decode())
    assert data2["overall_status"] == OVERALL_SUCCESS
    assert data2["results"][0]["status"] == STATUS_STALE_IGNORED

@pytest.mark.asyncio
async def test_08_equal_revision_already_applied(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "items": [{
            "uid": "eq-event",
            "operation": "upsert",
            "source_revision": "2026-09-28T01:00:00Z",
            "summary": "Equal Rev",
            "dtstart": "2026-09-28T09:00:00Z"
        }]
    }
    await view.post(MockRequest(payload), "work")

    # Resend identical revision
    r2 = await view.post(MockRequest(payload), "work")
    assert r2.status == 200
    data2 = json.loads(r2.body.decode())
    assert data2["results"][0]["status"] == STATUS_ALREADY_APPLIED

@pytest.mark.asyncio
async def test_09_newer_update_after_removal_resurrects(test_setup):
    hass, session, storage, view, google_service = test_setup

    # Step 1: Remove event at rev 1
    p_remove = {
        "items": [{
            "uid": "resurrect-event",
            "operation": "remove",
            "source_revision": "2026-09-28T01:00:00Z"
        }]
    }
    r1 = await view.post(MockRequest(p_remove), "work")
    assert r1.status == 200

    # Step 2: New update at rev 2 arrives
    p_upsert = {
        "items": [{
            "uid": "resurrect-event",
            "operation": "upsert",
            "source_revision": "2026-09-28T02:00:00Z",
            "summary": "Resurrected Meeting",
            "dtstart": "2026-09-28T09:00:00Z"
        }]
    }
    r2 = await view.post(MockRequest(p_upsert), "work")
    assert r2.status == 200
    assert json.loads(r2.body.decode())["results"][0]["status"] == STATUS_APPLIED

@pytest.mark.asyncio
async def test_10_delayed_update_after_tombstone_ignored(test_setup):
    hass, session, storage, view, google_service = test_setup

    # Step 1: Delete at rev 2
    p_remove = {
        "items": [{
            "uid": "zombie-event",
            "operation": "remove",
            "source_revision": "2026-09-28T02:00:00Z"
        }]
    }
    await view.post(MockRequest(p_remove), "work")

    # Step 2: Delayed older update arrives at rev 1
    p_stale = {
        "items": [{
            "uid": "zombie-event",
            "operation": "upsert",
            "source_revision": "2026-09-28T01:00:00Z",
            "summary": "Zombie Attempt",
            "dtstart": "2026-09-28T09:00:00Z"
        }]
    }
    r2 = await view.post(MockRequest(p_stale), "work")
    assert r2.status == 200
    data2 = json.loads(r2.body.decode())
    assert data2["results"][0]["status"] == STATUS_STALE_IGNORED

@pytest.mark.asyncio
async def test_11_duplicate_removal_already_applied(test_setup):
    hass, session, storage, view, google_service = test_setup

    p_remove = {
        "items": [{
            "uid": "dup-remove-event",
            "operation": "remove",
            "source_revision": "2026-09-28T01:00:00Z"
        }]
    }
    await view.post(MockRequest(p_remove), "work")

    # Duplicate removal
    r2 = await view.post(MockRequest(p_remove), "work")
    assert r2.status == 200
    data2 = json.loads(r2.body.decode())
    assert data2["results"][0]["status"] == STATUS_ALREADY_APPLIED

@pytest.mark.asyncio
async def test_12_unsupported_recurrence_rejection(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "items": [{
            "uid": "bad-rrule",
            "operation": "upsert",
            "summary": "Bad RRULE",
            "dtstart": "2026-09-28T09:00:00Z",
            "rrule": "THIS IS NOT A VALID RRULE"
        }]
    }
    resp = await view.post(MockRequest(payload), "work")
    assert resp.status == 422
    data = json.loads(resp.body.decode())
    res = data["results"][0]
    assert res["status"] == STATUS_REJECTED
    assert res["retryable"] is False
    assert res["code"] in (CODE_UNSUPPORTED_RECURRENCE, CODE_INVALID_EVENT)

@pytest.mark.asyncio
async def test_13_rate_limiting_and_retry_after(test_setup):
    hass, session, storage, view, google_service = test_setup

    # Configure mock Google service to return 429
    resp_headers = {"status": "429", "retry-after": "45"}
    http_err = HttpError(Response(resp_headers), b'{"error": {"message": "Rate Limit Exceeded"}}')

    def fail_callback(req, req_id):
        return None, http_err

    google_service.batch_behavior = fail_callback

    payload = {
        "items": [{
            "uid": "rate-limited-ev",
            "operation": "upsert",
            "summary": "Meeting",
            "dtstart": "2026-09-28T09:00:00Z"
        }]
    }
    resp = await view.post(MockRequest(payload), "work")
    assert resp.status == 429
    assert resp.headers.get(HEADER_RETRY_AFTER) == "45"

    data = json.loads(resp.body.decode())
    res = data["results"][0]
    assert res["status"] == STATUS_RETRYABLE
    assert res["retryable"] is True
    assert res["code"] == CODE_RATE_LIMITED

@pytest.mark.asyncio
async def test_14_missing_null_blank_classification_allowed(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "items": [
            {"uid": "ev-null-class", "operation": "upsert", "summary": "Null Class", "dtstart": "2026-09-28T09:00:00Z", "class": None},
            {"uid": "ev-blank-class", "operation": "upsert", "summary": "Blank Class", "dtstart": "2026-09-28T09:00:00Z", "class": ""},
            {"uid": "ev-missing-class", "operation": "upsert", "summary": "Missing Class", "dtstart": "2026-09-28T09:00:00Z"},
        ]
    }
    resp = await view.post(MockRequest(payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["events_processed"] == 3
    assert all(r["status"] == STATUS_APPLIED for r in data["results"])

@pytest.mark.asyncio
async def test_15_valid_classification_mapped(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "items": [
            {"uid": "ev-pub", "operation": "upsert", "summary": "Public", "dtstart": "2026-09-28T09:00:00Z", "class": "PUBLIC"},
            {"uid": "ev-priv", "operation": "upsert", "summary": "Private", "dtstart": "2026-09-28T09:00:00Z", "class": "PRIVATE"},
        ]
    }
    resp = await view.post(MockRequest(payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())
    assert data["events_processed"] == 2

@pytest.mark.asyncio
async def test_16_target_alias_mismatch_detection(test_setup):
    hass, session, storage, view, google_service = test_setup

    payload = {
        "target_alias": "personal",  # URL is 'work'
        "items": [{"uid": "ev-mismatch", "operation": "upsert", "summary": "Sync", "dtstart": "2026-09-28T09:00:00Z"}]
    }
    resp = await view.post(MockRequest(payload), "work")
    assert resp.status == 400
    data = json.loads(resp.body.decode())
    assert data["overall_status"] == OVERALL_ERROR
    assert data["errors"][0]["code"] == CODE_INVALID_REQUEST

@pytest.mark.asyncio
async def test_17_backward_compatibility_legacy_format(test_setup):
    hass, session, storage, view, google_service = test_setup

    legacy_payload = {
        "operation": "add",
        "events": [
            {
                "uid": "legacy-ev-1",
                "summary": "Legacy Event",
                "dtstart": "2026-09-28T09:00:00Z",
                "dtend": "2026-09-28T09:30:00Z",
            }
        ]
    }
    resp = await view.post(MockRequest(legacy_payload), "work")
    assert resp.status == 200
    data = json.loads(resp.body.decode())

    # Has legacy fields
    assert data["events_processed"] == 1
    assert data["operation"] == "add"
    assert data["target_alias"] == "work"

    # Also has new fields!
    assert data["schema_version"] == 1
    assert data["overall_status"] == OVERALL_SUCCESS
    assert "request_id" in data
    assert len(data["results"]) == 1
    assert data["results"][0]["uid"] == "legacy-ev-1"
    assert data["results"][0]["status"] == STATUS_APPLIED
