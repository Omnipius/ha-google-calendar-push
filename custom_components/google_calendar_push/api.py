import asyncio
from datetime import datetime, date, timedelta, timezone
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from aiohttp import web
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util
import pytz

from .const import (
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
    SIGNAL_UPDATE_ENDPOINT,
    STATUS_ALREADY_APPLIED,
    STATUS_APPLIED,
    STATUS_CONFLICT,
    STATUS_REJECTED,
    STATUS_RETRYABLE,
    STATUS_STALE_IGNORED,
)
from .ical_patch import Event

_LOGGER = logging.getLogger(__name__)

WINDOWS_TO_IANA_MAP = {
    "GMT Standard Time": "Europe/London",
    "Pacific Standard Time": "America/Los_Angeles",
    "Mountain Standard Time": "America/Denver",
    "Central Standard Time": "America/Chicago",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indianapolis",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Prague",
    "Romance Standard Time": "Europe/Paris",
    "India Standard Time": "Asia/Calcutta",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
}

def _parse_rfc9775_datetime(data):
    """Recursively parses RFC 9775 datetime strings into proper timezone-aware Python datetime objects."""
    if isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            new_k = k
            if isinstance(k, str):
                match = re.search(r'^(.*?T\d{2}:\d{2}:\d{2}.*?)\[(.*?)\]$', k)
                if match:
                    new_k = match.group(1)
            new_dict[new_k] = _parse_rfc9775_datetime(v)
        return new_dict
    elif isinstance(data, list):
        return [_parse_rfc9775_datetime(v) for v in data]
    elif isinstance(data, str):
        match = re.search(r'^(.*?T\d{2}:\d{2}:\d{2}.*?)\[(.*?)\]$', data)
        if match:
            iso_str = match.group(1)
            raw_tz_name = match.group(2).strip()
            iana_tz_name = WINDOWS_TO_IANA_MAP.get(raw_tz_name, raw_tz_name)
            try:
                naive_dt = datetime.fromisoformat(iso_str.replace('Z', ''))
                tz_obj = dt_util.get_time_zone(iana_tz_name)
                if tz_obj is None:
                    raise ValueError(f"Unrecognized Timezone: {iana_tz_name}")
                localized_dt = naive_dt.replace(tzinfo=tz_obj)
                return localized_dt
            except Exception as e:
                _LOGGER.warning("RFC 9775 Parsing fallback for %s: %s", data, e)
                try:
                    return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
                except ValueError:
                    return iso_str
    return data

def _get_tz_name_and_dt(dt_obj):
    """Safely extract an IANA timezone string and return a safe datetime object."""
    ha_tz = dt_util.DEFAULT_TIME_ZONE
    ha_tz_name = str(ha_tz)

    if dt_obj.tzinfo is None:
        return ha_tz_name, dt_obj.replace(tzinfo=ha_tz)

    if dt_obj.tzinfo == timezone.utc or str(dt_obj.tzinfo) in ["UTC", "GMT", "UTC+00:00"]:
        return "UTC", dt_obj

    if hasattr(dt_obj.tzinfo, "key"):
        return dt_obj.tzinfo.key, dt_obj

    if hasattr(dt_obj.tzinfo, "zone"):
        return dt_obj.tzinfo.zone, dt_obj

    naive_dt = dt_obj.replace(tzinfo=None)
    localized_dt = naive_dt.replace(tzinfo=ha_tz)
    return ha_tz_name, localized_dt

def _format_google_datetime(dt_obj, tz_name):
    """Format a datetime object for Google API, avoiding duplicate offset/timezone conflicts."""
    if tz_name and tz_name != "UTC":
        try:
            tz = pytz.timezone(tz_name)
            if dt_obj.tzinfo is None:
                dt_obj = tz.localize(dt_obj)
            else:
                dt_obj = dt_obj.astimezone(tz)
        except Exception:
            pass
        return dt_obj.replace(tzinfo=None).isoformat()
    return dt_obj.isoformat()

def _convert_ical_to_google(event: Event, raw_event: dict):
    """Strictly map to Google Calendar API format."""
    body = {}

    uid = getattr(event, "uid", None) or getattr(event, "icaluid", None)
    if uid:
        body["iCalUID"] = str(uid)

    if getattr(event, "summary", None): body["summary"] = event.summary
    if getattr(event, "description", None): body["description"] = event.description
    if getattr(event, "location", None): body["location"] = event.location

    dtstart = getattr(event, "dtstart", None)
    dtend = getattr(event, "dtend", None)

    if dtstart and not dtend:
        if isinstance(dtstart, datetime):
            dtend = dtstart + timedelta(minutes=30)
        elif isinstance(dtstart, date):
            dtend = dtstart + timedelta(days=1)

    if dtstart and dtend and dtstart == dtend:
        if isinstance(dtstart, datetime):
            dtend = dtstart + timedelta(minutes=30)
        elif isinstance(dtstart, date):
            dtend = dtstart + timedelta(days=1)

    if dtstart:
        if isinstance(dtstart, datetime):
            tz_name, safe_dtstart = _get_tz_name_and_dt(dtstart)
            body["start"] = {
                "dateTime": _format_google_datetime(safe_dtstart, tz_name),
                "timeZone": tz_name
            }
        elif isinstance(dtstart, date):
            body["start"] = {"date": dtstart.isoformat()}

    if dtend:
        if isinstance(dtend, datetime):
            tz_name, safe_dtend = _get_tz_name_and_dt(dtend)
            body["end"] = {
                "dateTime": _format_google_datetime(safe_dtend, tz_name),
                "timeZone": tz_name
            }
        elif isinstance(dtend, date):
            body["end"] = {"date": dtend.isoformat()}

    recurrence_id = getattr(event, "recurrence_id", None)
    if recurrence_id:
        if isinstance(recurrence_id, datetime):
            tz_name, safe_rec_id = _get_tz_name_and_dt(recurrence_id)
            body["originalStartTime"] = {
                "dateTime": _format_google_datetime(safe_rec_id, tz_name),
                "timeZone": tz_name
            }
        elif isinstance(recurrence_id, date):
            body["originalStartTime"] = {"date": recurrence_id.isoformat()}

    status = getattr(event, "status", None)
    if status:
        body["status"] = str(status.value).lower() if hasattr(status, 'value') else str(status).lower()

    transparency = getattr(event, "transparency", None)
    if transparency:
        body["transparency"] = str(transparency.value).lower() if hasattr(transparency, 'value') else str(transparency).lower()

    classification = getattr(event, "classification", None)
    if classification:
        class_val = str(classification.value).lower() if hasattr(classification, 'value') else str(classification).lower()
        if class_val in ("public", "private", "confidential"):
            body["visibility"] = class_val

    rrule = getattr(event, "rrule", None)
    if rrule:
        recurrence_rules = []
        rrule_list = rrule if isinstance(rrule, list) else [rrule]
        for rule in rrule_list:
            if hasattr(rule, "as_rrule_str"):
                recurrence_rules.append(f"RRULE:{rule.as_rrule_str()}")
            else:
                rule_str = str(rule)
                if not rule_str.startswith("RRULE:"):
                    recurrence_rules.append(f"RRULE:{rule_str}")
                else:
                    recurrence_rules.append(rule_str)
        if recurrence_rules:
            body["recurrence"] = recurrence_rules

    alarms_list = raw_event.get("valarm") or raw_event.get("alarms") or []
    if alarms_list and isinstance(alarms_list, list):
        overrides = []
        for alarm_dict in alarms_list:
            if not isinstance(alarm_dict, dict): continue
            action = str(alarm_dict.get("action", "")).upper()
            method = "email" if "EMAIL" in action else "popup"
            trigger = alarm_dict.get("trigger", "")
            if isinstance(trigger, datetime):
                if isinstance(dtstart, datetime):
                    trigger_dt = trigger
                    if dtstart.tzinfo and not trigger_dt.tzinfo:
                        trigger_dt = trigger_dt.replace(tzinfo=dtstart.tzinfo)
                    elif not dtstart.tzinfo and trigger_dt.tzinfo:
                        trigger_dt = trigger_dt.replace(tzinfo=None)
                    delta = dtstart - trigger_dt
                    mins = max(0, int(delta.total_seconds() / 60))
                else:
                    mins = 10
            else:
                trigger_str = str(trigger)
                clean_trigger = trigger_str.lstrip('+-')
                mins = 10
                match = re.match(r'^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$', clean_trigger)
                if match:
                    weeks = int(match.group(1) or 0)
                    days = int(match.group(2) or 0)
                    hours = int(match.group(3) or 0)
                    minutes = int(match.group(4) or 0)
                    mins = (weeks * 10080) + (days * 1440) + (hours * 60) + minutes
                elif trigger_str:
                    try:
                        trigger_dt_str = re.sub(r'\[.*?\]$', '', trigger_str)
                        trigger_dt = datetime.fromisoformat(trigger_dt_str.replace('Z', '+00:00'))
                        if isinstance(dtstart, datetime):
                            if dtstart.tzinfo and not trigger_dt.tzinfo:
                                trigger_dt = trigger_dt.replace(tzinfo=dtstart.tzinfo)
                            elif not dtstart.tzinfo and trigger_dt.tzinfo:
                                trigger_dt = trigger_dt.replace(tzinfo=None)
                            delta = dtstart - trigger_dt
                            mins = max(0, int(delta.total_seconds() / 60))
                    except Exception:
                        pass

            mins = max(0, min(mins, 40320))
            override_entry = {"method": method, "minutes": mins}
            if override_entry not in overrides:
                overrides.append(override_entry)

        if overrides:
            body["reminders"] = {
                "useDefault": False,
                "overrides": overrides[:5]
            }

    attendees = getattr(event, "attendees", None)
    if attendees:
        google_attendees = []
        for att in attendees:
            email = getattr(att, "cal_address", str(att))
            if email.lower().startswith("mailto:"):
                email = email[7:]
            if "@" in email:
                google_attendees.append({"email": email})
        if google_attendees:
            body["attendees"] = google_attendees

    organizer = getattr(event, "organizer", None)
    if organizer:
        email = getattr(organizer, "cal_address", str(organizer))
        if email.lower().startswith("mailto:"):
            email = email[7:]
        if "@" in email:
            body["organizer"] = {"email": email}

    url = getattr(event, "url", None)
    if url:
        body["source"] = {"url": str(url), "title": "Original Event Link"}

    categories = getattr(event, "categories", None)
    if categories:
        cat_str = f"\n\nCategories: {', '.join(categories)}"
        body["description"] = body.get("description", "") + cat_str

    return body

def _classify_google_error(exc: Exception) -> Tuple[str, bool, str, Optional[int]]:
    """
    Classify an exception into a machine-readable code, retryable flag, message, and Retry-After.
    """
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", 500)
        reason = getattr(exc, "_get_reason", lambda: str(exc))()
        retry_after = None
        if hasattr(exc, "resp") and hasattr(exc.resp, "get"):
            ra_hdr = exc.resp.get("retry-after")
            if ra_hdr and ra_hdr.isdigit():
                retry_after = int(ra_hdr)

        if status == 401:
            return CODE_AUTHENTICATION_FAILED, False, f"Google authentication failed: {reason}", None
        elif status == 403:
            if "quota" in str(exc).lower() or "ratelimit" in str(exc).lower() or "userRateLimitExceeded" in str(exc):
                return CODE_RATE_LIMITED, True, f"Google API rate limit exceeded: {reason}", retry_after or 60
            return CODE_AUTHORIZATION_FAILED, False, f"Google calendar authorization failed: {reason}", None
        elif status == 404:
            return CODE_TARGET_UNAVAILABLE, False, f"Target calendar or resource unavailable: {reason}", None
        elif status == 409:
            return CODE_IDEMPOTENCY_CONFLICT, False, f"Google API conflict: {reason}", None
        elif status == 429:
            return CODE_RATE_LIMITED, True, f"Google API rate limit exceeded: {reason}", retry_after or 60
        elif status in (500, 502, 503, 504):
            code = CODE_DOWNSTREAM_TIMEOUT if status in (504, 408) else CODE_INTERNAL_ERROR
            return code, True, f"Google API downstream error ({status}): {reason}", retry_after or 15
        elif status in (400, 422):
            if "recurrence" in str(exc).lower() or "rrule" in str(exc).lower():
                return CODE_UNSUPPORTED_RECURRENCE, False, f"Unsupported recurrence pattern: {reason}", None
            return CODE_INVALID_EVENT, False, f"Invalid event data rejected by Google: {reason}", None
        return CODE_INTERNAL_ERROR, False, f"Google API error ({status}): {reason}", None

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return CODE_DOWNSTREAM_TIMEOUT, True, "Downstream request timed out", 10

    return CODE_INTERNAL_ERROR, True, str(exc), None


class GoogleCalendarPushView(HomeAssistantView):
    """REST API endpoint for pushing events to Google Calendar."""

    url = "/api/google_calendar_push/{calendar_alias}"
    name = "api:google_calendar_push"
    requires_auth = True

    def __init__(self, hass, session, calendar_aliases, storage=None):
        self.hass = hass
        self.session = session
        self.calendar_aliases = calendar_aliases
        self.storage = storage

    def _get_google_service(self):
        credentials = Credentials(
            token=self.session.token["access_token"],
            refresh_token=self.session.token.get("refresh_token"),
            token_uri=self.session.token.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=self.session.token.get("client_id"),
            client_secret=self.session.token.get("client_secret"),
        )
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def _parse_event_item(self, raw_event):
        """Parse and validate a single event dictionary."""
        processed_event = _parse_rfc9775_datetime(raw_event)
        validated_event = Event.model_validate(processed_event)
        return validated_event, processed_event

    async def _execute_batch_chunk(self, service, batch_reqs):
        """Execute a chunk of requests in a thread to prevent blocking the event loop."""
        def _run_batch():
            batch = service.new_batch_http_request()
            for req, req_id, cb in batch_reqs:
                batch.add(req, request_id=req_id, callback=cb)
            batch.execute()

        await self.hass.async_add_executor_job(_run_batch)

    async def _process_operation(
        self,
        service,
        calendar_id: str,
        calendar_alias: str,
        active_uids: List[str],
        uid_trackers: Dict[str, dict],
    ):
        """Execute Google search and mutation passes for active UIDs."""
        masters_data = []
        exceptions_data = []
        newly_created_masters = {}
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=60)

        # Track mutations per UID to verify all sub-tasks succeed
        uid_subtasks: Dict[str, Set[str]] = {uid: set() for uid in active_uids}
        uid_failures: Dict[str, dict] = {}
        req_id_to_uid: Dict[str, str] = {}
        req_id_to_body: Dict[str, dict] = {}

        for uid in active_uids:
            tracker = uid_trackers[uid]
            if tracker["is_removal"]:
                # Removals are handled as master deletions
                continue

            event = tracker["valid_event"]
            raw_event = tracker["processed_raw_event"]

            if getattr(event, "recurrence_id", None):
                exceptions_data.append((event, raw_event, uid))
            else:
                masters_data.append((event, raw_event, uid))

            exceptions = getattr(event, "exceptions", None)
            if exceptions:
                raw_exceptions = raw_event.get("exceptions", {}) or {}
                tz_name = None
                if event.dtstart and isinstance(event.dtstart, datetime):
                    tz_name, _ = _get_tz_name_and_dt(event.dtstart)

                for exc_key, exc_event in exceptions.items():
                    r_key = None
                    raw_val = {}
                    for k_raw, v_raw in raw_exceptions.items():
                        match = False
                        if k_raw == exc_key:
                            match = True
                        else:
                            try:
                                if isinstance(exc_key, datetime):
                                    if datetime.fromisoformat(k_raw.replace('Z', '+00:00')) == exc_key:
                                        match = True
                                elif isinstance(exc_key, date):
                                    if date.fromisoformat(k_raw) == exc_key:
                                        match = True
                            except Exception:
                                pass
                        if match:
                            r_key = k_raw
                            raw_val = v_raw if isinstance(v_raw, dict) else {}
                            break

                    if r_key is None:
                        r_key = exc_key.isoformat() if hasattr(exc_key, "isoformat") else str(exc_key)

                    try:
                        exc_dt = None
                        if isinstance(exc_key, datetime):
                            exc_dt = exc_key.astimezone(timezone.utc)
                        elif isinstance(exc_key, date):
                            exc_dt = datetime.combine(exc_key, datetime.min.time(), tzinfo=timezone.utc)
                        elif isinstance(exc_key, str):
                            clean_key = re.sub(r'\[.*?\]$', '', exc_key)
                            exc_dt = datetime.fromisoformat(clean_key.replace('Z', '+00:00')).astimezone(timezone.utc)

                        if exc_dt and exc_dt < cutoff_date:
                            continue
                    except Exception:
                        pass

                    if exc_event is None:
                        r_key_str = r_key
                        if isinstance(r_key_str, (datetime, date)):
                            r_key_str = r_key_str.isoformat()

                        if tz_name and "T" in r_key_str and "[" not in r_key_str:
                            r_key_str = re.sub(r'(Z|[+-]\d{2}:\d{2})$', '', r_key_str)
                            r_key_str = f"{r_key_str}[{tz_name}]"

                        end_key_str = r_key_str
                        try:
                            if isinstance(r_key, datetime):
                                end_key_dt = r_key + timedelta(minutes=30)
                                end_key_str = end_key_dt.isoformat()
                            elif isinstance(r_key, date):
                                end_key_dt = r_key + timedelta(days=1)
                                end_key_str = end_key_dt.isoformat()
                            elif isinstance(r_key, str):
                                rk_dt = datetime.fromisoformat(re.sub(r'\[.*?\]$', '', r_key).replace('Z', '+00:00'))
                                end_key_dt = rk_dt + timedelta(minutes=30)
                                end_key_str = end_key_dt.isoformat()
                        except Exception:
                            pass

                        if tz_name and "T" in end_key_str and "[" not in end_key_str:
                            end_key_str = re.sub(r'(Z|[+-]\d{2}:\d{2})$', '', end_key_str)
                            end_key_str = f"{end_key_str}[{tz_name}]"

                        cancel_raw = {
                            "uid": uid,
                            "recurrence-id": r_key_str,
                            "dtstart": r_key_str,
                            "dtend": end_key_str,
                            "status": "CANCELLED"
                        }
                        try:
                            cancel_event = Event.model_validate(cancel_raw)
                            exceptions_data.append((cancel_event, cancel_raw, uid))
                        except Exception as e:
                            _LOGGER.error("Validation failed for cancellation exception: %s", e)
                    else:
                        exceptions_data.append((exc_event, raw_val, uid))

        async def execute_pass(events_data, is_exception_pass: bool):
            nonlocal newly_created_masters
            search_results = {}

            def search_callback(request_id, response, exception):
                parent_uid = request_id
                if exception is not None:
                    code, retryable, msg, ra = _classify_google_error(exception)
                    uid_failures[parent_uid] = {
                        "code": code,
                        "retryable": retryable,
                        "message": msg,
                        "retry_after": ra,
                    }
                else:
                    search_results[parent_uid] = response.get("items", [])

            # Phase 1: Search for existing events by iCalUID
            search_reqs = []
            seen_uids = set()
            for ev, _, u in events_data:
                if u in seen_uids:
                    continue
                seen_uids.add(u)
                req = service.events().list(calendarId=calendar_id, iCalUID=u, showDeleted=True)
                search_reqs.append((req, u, search_callback))

            if search_reqs:
                chunk_size = 50
                for i in range(0, len(search_reqs), chunk_size):
                    chunk = search_reqs[i:i + chunk_size]
                    try:
                        await self._execute_batch_chunk(service, chunk)
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        _LOGGER.error("Search chunk execution failed: %s", e)

            # Phase 2: Mutate events
            def mutate_callback(request_id, response, exception):
                parent_uid = req_id_to_uid.get(request_id)
                if not parent_uid:
                    parent_uid = request_id.rsplit('_', 1)[0] if '_' in request_id else request_id

                if exception is not None:
                    error_msg = str(exception)
                    if "404" in error_msg and is_exception_pass:
                        # Log but do not fail if Google has not spawned the virtual instance yet
                        _LOGGER.debug("404 on instance mutation for UID %s, ignoring: %s", parent_uid, error_msg)
                    else:
                        code, retryable, msg, ra = _classify_google_error(exception)
                        uid_failures[parent_uid] = {
                            "code": code,
                            "retryable": retryable,
                            "message": msg,
                            "retry_after": ra,
                        }
                else:
                    if not is_exception_pass and response and "id" in response:
                        newly_created_masters[parent_uid] = response["id"]

            uid_operations = {}
            for index, (ev, r_ev, u) in enumerate(events_data):
                if u in uid_failures:
                    # Skip if search failed
                    continue

                items = search_results.get(u, [])
                body = _convert_ical_to_google(ev, r_ev) if ev else {}

                master_item_id = None
                master_item = None
                for item in items:
                    if "originalStartTime" not in item:
                        base_id = re.sub(r'_\d{8}T\d{6}Z$', '', item["id"])
                        master_item_id = base_id
                        master_item = item
                        break

                if not master_item_id and u in newly_created_masters:
                    master_item_id = newly_created_masters[u]
                    base_id = re.sub(r'_\d{8}T\d{6}Z$', '', master_item_id)
                    master_item_id = base_id

                target_event_id = None

                if is_exception_pass:
                    if master_item and "start" in master_item:
                        m_start = master_item["start"].get("dateTime")
                        m_tz = master_item["start"].get("timeZone")
                        if m_start and "originalStartTime" in body and "dateTime" in body["originalStartTime"]:
                            try:
                                o_dt_str = body["originalStartTime"]["dateTime"]
                                o_dt = datetime.fromisoformat(o_dt_str.replace('Z', '+00:00'))
                                m_dt = datetime.fromisoformat(m_start.replace('Z', '+00:00'))
                                perfect_dt = datetime.combine(o_dt.date(), m_dt.time(), tzinfo=m_dt.tzinfo)
                                body["originalStartTime"]["dateTime"] = _format_google_datetime(perfect_dt, m_tz)
                                if m_tz:
                                    body["originalStartTime"]["timeZone"] = m_tz
                            except Exception as e:
                                _LOGGER.error("Failed to sync originalStartTime: %s", e)

                    for item in items:
                        if "originalStartTime" in item:
                            in_start = body.get("originalStartTime", {})
                            go_start = item.get("originalStartTime", {})
                            in_dt = in_start.get("dateTime")
                            go_dt = go_start.get("dateTime")
                            if in_dt and go_dt:
                                try:
                                    dt1_str = re.sub(r'\[.*?\]$', '', in_dt)
                                    dt1 = datetime.fromisoformat(dt1_str.replace('Z', '+00:00'))
                                    if dt1.tzinfo is None:
                                        in_tz = in_start.get("timeZone")
                                        if in_tz:
                                            tz = dt_util.get_time_zone(in_tz)
                                            if tz: dt1 = dt1.replace(tzinfo=tz)
                                    if dt1.tzinfo: dt1 = dt1.astimezone(timezone.utc)

                                    dt2_str = re.sub(r'\[.*?\]$', '', go_dt)
                                    dt2 = datetime.fromisoformat(dt2_str.replace('Z', '+00:00'))
                                    if dt2.tzinfo is None:
                                        go_tz = go_start.get("timeZone")
                                        if go_tz:
                                            tz = dt_util.get_time_zone(go_tz)
                                            if tz: dt2 = dt2.replace(tzinfo=tz)
                                    if dt2.tzinfo: dt2 = dt2.astimezone(timezone.utc)

                                    if dt1 == dt2:
                                        target_event_id = item["id"]
                                        break
                                except (ValueError, TypeError):
                                    if in_dt == go_dt:
                                        target_event_id = item["id"]
                                        break
                            elif in_start.get("date") and go_start.get("date"):
                                if in_start.get("date") == go_start.get("date"):
                                    target_event_id = item["id"]
                                    break

                    if not target_event_id and master_item_id:
                        orig_time = body.get("originalStartTime", {})
                        try:
                            if 'dateTime' in orig_time:
                                dt_str = orig_time['dateTime']
                                tz_name = orig_time.get('timeZone')
                                dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
                                if dt.tzinfo is None:
                                    tz = dt_util.get_time_zone(tz_name) if tz_name else dt_util.DEFAULT_TIME_ZONE
                                    dt = dt.replace(tzinfo=tz or dt_util.DEFAULT_TIME_ZONE)
                                dt_utc = dt.astimezone(timezone.utc)
                                time_str = dt_utc.strftime('%Y%m%dT%H%M%SZ')
                                target_event_id = f"{master_item_id}_{time_str}"
                            elif 'date' in orig_time:
                                d = date.fromisoformat(orig_time['date'])
                                time_str = d.strftime('%Y%m%d')
                                target_event_id = f"{master_item_id}_{time_str}"
                        except Exception as e:
                            _LOGGER.error("Instance ID computation failed for UID %s: %s", u, e)

                    if not target_event_id:
                        continue

                    body.pop("iCalUID", None)
                    body.pop("recurrence", None)
                    body.pop("originalStartTime", None)

                    if body.get("status") == "cancelled" and master_item and "start" in master_item and "end" in master_item:
                        body["start"] = master_item["start"]
                        body["end"] = master_item["end"]
                else:
                    target_event_id = master_item_id

                unique_req_id = f"{u}_{index}_{'exc' if is_exception_pass else 'mst'}"
                req_id_to_uid[unique_req_id] = u
                req_id_to_body[unique_req_id] = body
                uid_subtasks[u].add(unique_req_id)

                if target_event_id:
                    resource_key = target_event_id
                else:
                    orig_time = body.get("originalStartTime", {})
                    time_str = orig_time.get("dateTime", orig_time.get("date", "master"))
                    resource_key = f"insert_{u}_{time_str}"

                mutation_op = None
                try:
                    tracker_op = uid_trackers[u]["operation"]
                    if tracker_op == OPERATION_REMOVE:
                        if target_event_id:
                            target_item = next((i for i in items if i["id"] == target_event_id), {})
                            if target_item.get("status") != "cancelled":
                                mutation_op = service.events().delete(calendarId=calendar_id, eventId=target_event_id)
                    else:
                        # Upsert operation (add or update)
                        if target_event_id:
                            target_item = next((i for i in items if i["id"] == target_event_id), {})
                            if target_item.get("status") == "cancelled":
                                body.setdefault("status", "confirmed")
                            mutation_op = service.events().update(calendarId=calendar_id, eventId=target_event_id, body=body)
                        else:
                            mutation_op = service.events().insert(calendarId=calendar_id, body=body)

                    if mutation_op:
                        if u not in uid_operations:
                            uid_operations[u] = {}
                        uid_operations[u][resource_key] = (mutation_op, unique_req_id)
                except Exception as e:
                    _LOGGER.error("Mutation prep error for UID %s: %s", u, e)
                    code, retryable, msg, ra = _classify_google_error(e)
                    uid_failures[u] = {
                        "code": code,
                        "retryable": retryable,
                        "message": msg,
                        "retry_after": ra,
                    }

            uid_op_lists = {uid_k: list(ops.values()) for uid_k, ops in uid_operations.items()}
            max_ops = max([len(ops) for ops in uid_op_lists.values()]) if uid_op_lists else 0

            for i in range(max_ops):
                mutate_reqs = []
                for uid_k, ops in uid_op_lists.items():
                    if i < len(ops):
                        mut_op, req_id = ops[i]
                        mutate_reqs.append((mut_op, req_id, mutate_callback))

                if mutate_reqs:
                    chunk_size = 50
                    for j in range(0, len(mutate_reqs), chunk_size):
                        chunk = mutate_reqs[j:j + chunk_size]
                        try:
                            await self._execute_batch_chunk(service, chunk)
                            await asyncio.sleep(0.1)
                        except Exception as e:
                            _LOGGER.error("Mutation chunk execution failed: %s", e)

        # Handle removals: build data entries for active removal UIDs
        removals_data = []
        for uid in active_uids:
            if uid_trackers[uid]["is_removal"]:
                removals_data.append((None, {}, uid))

        if masters_data or removals_data:
            await execute_pass(masters_data + removals_data, is_exception_pass=False)
            if exceptions_data:
                await asyncio.sleep(1.0)

        if exceptions_data:
            await execute_pass(exceptions_data, is_exception_pass=True)

        # Determine terminal status for each active UID
        for uid in active_uids:
            tracker = uid_trackers[uid]
            if uid in uid_failures:
                fail_info = uid_failures[uid]
                tracker["status"] = STATUS_RETRYABLE if fail_info["retryable"] else STATUS_REJECTED
                tracker["code"] = fail_info["code"]
                tracker["retryable"] = fail_info["retryable"]
                tracker["message"] = fail_info["message"]
                tracker["retry_after"] = fail_info.get("retry_after")
            else:
                # If it was a removal and not found in Google Calendar, it was already applied
                tracker["status"] = STATUS_APPLIED
                # Persist successful mutation in storage
                if self.storage:
                    status_type = "tombstone" if tracker["is_removal"] else "active"
                    google_id = newly_created_masters.get(uid)
                    await self.storage.record_event_mutation(
                        alias=calendar_alias,
                        uid=uid,
                        status=status_type,
                        source_revision=tracker["source_revision"],
                        google_event_id=google_id,
                    )

    async def post(self, request, calendar_alias: str):
        start_time = time.monotonic()
        calendar_id = self.calendar_aliases.get(calendar_alias)

        if not calendar_id:
            return web.Response(
                status=404,
                text=f"Endpoint alias '{calendar_alias}' is not configured."
            )

        header_request_id = request.headers.get(HEADER_REQUEST_ID)
        header_idemp_key = request.headers.get(HEADER_IDEMPOTENCY_KEY)

        try:
            data = await request.json()
        except Exception:
            req_id = header_request_id or str(uuid.uuid4())
            return web.json_response({
                "schema_version": SCHEMA_VERSION,
                "request_id": req_id,
                "target_alias": calendar_alias,
                "overall_status": OVERALL_ERROR,
                "events_processed": 0,
                "results": [],
                "errors": [{
                    "code": CODE_INVALID_REQUEST,
                    "message": "Invalid JSON payload",
                    "retryable": False
                }]
            }, status=400)

        # Determine request ID and idempotency key
        request_id = data.get("request_id") or header_request_id or str(uuid.uuid4())
        idempotency_key = data.get("idempotency_key") or header_idemp_key

        # Validate target alias if provided in body
        payload_target = data.get("target_alias")
        if payload_target and payload_target != calendar_alias:
            return web.json_response({
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "target_alias": calendar_alias,
                "overall_status": OVERALL_ERROR,
                "events_processed": 0,
                "results": [],
                "errors": [{
                    "code": CODE_INVALID_REQUEST,
                    "message": f"Target alias '{payload_target}' does not match endpoint alias '{calendar_alias}'.",
                    "retryable": False
                }]
            }, status=400)

        # Compute payload hash for idempotency checking
        try:
            canonical_payload = json.dumps(data, sort_keys=True, default=str)
            payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        except Exception:
            payload_hash = None

        # Check durable idempotency receipt
        if idempotency_key and self.storage:
            cached = await self.storage.get_idempotency(idempotency_key)
            if cached:
                cached_hash = cached.get("payload_hash")
                if cached_hash == payload_hash:
                    _LOGGER.info(
                        "Idempotent replay detected | key=%s request_id=%s target=%s",
                        idempotency_key, request_id, calendar_alias
                    )
                    cached_resp = dict(cached.get("response_data", {}))
                    cached_resp["request_id"] = request_id
                    return web.json_response(cached_resp, status=cached.get("status_code", 200))
                else:
                    _LOGGER.warning(
                        "Idempotency conflict detected | key=%s target=%s",
                        idempotency_key, calendar_alias
                    )
                    return web.json_response({
                        "schema_version": SCHEMA_VERSION,
                        "request_id": request_id,
                        "idempotency_key": idempotency_key,
                        "target_alias": calendar_alias,
                        "overall_status": OVERALL_ERROR,
                        "events_processed": 0,
                        "results": [],
                        "errors": [{
                            "code": CODE_IDEMPOTENCY_CONFLICT,
                            "message": f"Idempotency key '{idempotency_key}' was previously used with a different payload.",
                            "retryable": False
                        }]
                    }, status=409)

        # Normalize envelope items (supports 'items' or 'events')
        raw_items = data.get("items")
        if raw_items is None:
            raw_items = data.get("events")

        if not isinstance(raw_items, list):
            return web.json_response({
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "target_alias": calendar_alias,
                "overall_status": OVERALL_ERROR,
                "events_processed": 0,
                "results": [],
                "errors": [{
                    "code": CODE_INVALID_REQUEST,
                    "message": "Payload must contain an 'items' or 'events' list.",
                    "retryable": False
                }]
            }, status=400)

        top_level_op = str(data.get("operation", "")).lower()

        # Build UID trackers maintaining submission order
        uid_trackers: Dict[str, dict] = {}
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue

            # Extract item event dict and fields
            nested_event = item.get("event") if isinstance(item.get("event"), dict) else None
            effective_event_dict = nested_event if nested_event is not None else item

            # Determine UID
            uid = (
                item.get("uid")
                or effective_event_dict.get("uid")
                or item.get("iCalUID")
                or effective_event_dict.get("iCalUID")
                or item.get("icaluid")
                or effective_event_dict.get("icaluid")
            )
            if not uid:
                uid = f"UNKNOWN_UID_{index}"

            uid_str = str(uid)

            # Prevent duplicate UIDs within single request from creating duplicate results
            if uid_str in uid_trackers:
                continue

            # Determine item operation
            raw_op = item.get("operation") or top_level_op
            raw_op_lower = str(raw_op).lower().strip()
            if raw_op_lower in ("add", "update", "upsert"):
                op = OPERATION_UPSERT
            elif raw_op_lower in ("remove", "delete"):
                op = OPERATION_REMOVE
            else:
                op = raw_op_lower or "unknown"

            # Determine source revision
            source_rev = (
                item.get("source_revision")
                or item.get("source-revision")
                or effective_event_dict.get("source_revision")
                or effective_event_dict.get("source-revision")
            )
            source_rev_str = str(source_rev).strip() if source_rev is not None else None

            is_removal = op == OPERATION_REMOVE

            uid_trackers[uid_str] = {
                "uid": uid_str,
                "operation": op,
                "source_revision": source_rev_str,
                "status": None,
                "code": None,
                "retryable": None,
                "message": None,
                "valid_event": None,
                "processed_raw_event": effective_event_dict,
                "is_removal": is_removal,
            }

            # Validate operation
            if op not in (OPERATION_UPSERT, OPERATION_REMOVE):
                uid_trackers[uid_str]["status"] = STATUS_REJECTED
                uid_trackers[uid_str]["code"] = CODE_INVALID_REQUEST
                uid_trackers[uid_str]["retryable"] = False
                uid_trackers[uid_str]["message"] = f"Unsupported operation '{raw_op}'."
                continue

            # Validate event structure for upserts
            if not is_removal:
                try:
                    val_ev, proc_ev = self._parse_event_item(effective_event_dict)
                    uid_trackers[uid_str]["valid_event"] = val_ev
                    uid_trackers[uid_str]["processed_raw_event"] = proc_ev
                except Exception as e:
                    _LOGGER.error("Event validation failed for UID %s: %s", uid_str, e)
                    err_msg = str(e)
                    code = CODE_UNSUPPORTED_RECURRENCE if "rrule" in err_msg.lower() or "recurrence" in err_msg.lower() else CODE_INVALID_EVENT
                    uid_trackers[uid_str]["status"] = STATUS_REJECTED
                    uid_trackers[uid_str]["code"] = code
                    uid_trackers[uid_str]["retryable"] = False
                    uid_trackers[uid_str]["message"] = f"Validation failed: {err_msg}"

        # Evaluate revision state and tombstones before Google API mutation
        for uid_str, tracker in uid_trackers.items():
            if tracker["status"] is not None:
                # Already failed validation
                continue

            if self.storage:
                should_exec, rev_status, rev_msg = await self.storage.check_revision_and_status(
                    alias=calendar_alias,
                    uid=uid_str,
                    incoming_rev=tracker["source_revision"],
                    incoming_op=tracker["operation"],
                )
                if not should_exec:
                    tracker["status"] = rev_status
                    tracker["message"] = rev_msg

        # Filter UIDs requiring downstream Google Calendar execution
        active_uids = [
            uid_str for uid_str, tracker in uid_trackers.items()
            if tracker["status"] is None
        ]

        max_retry_after: Optional[int] = None

        if active_uids:
            try:
                if not self.session.valid_token:
                    await self.session.async_ensure_token_valid()
                service = await self.hass.async_add_executor_job(self._get_google_service)
                await self._process_operation(
                    service=service,
                    calendar_id=calendar_id,
                    calendar_alias=calendar_alias,
                    active_uids=active_uids,
                    uid_trackers=uid_trackers,
                )
            except Exception as e:
                _LOGGER.error("Downstream execution error: %s", e)
                code, retryable, msg, ra = _classify_google_error(e)
                if ra and (max_retry_after is None or ra > max_retry_after):
                    max_retry_after = ra
                for uid_str in active_uids:
                    if uid_trackers[uid_str]["status"] is None:
                        uid_trackers[uid_str]["status"] = STATUS_RETRYABLE if retryable else STATUS_REJECTED
                        uid_trackers[uid_str]["code"] = code
                        uid_trackers[uid_str]["retryable"] = retryable
                        uid_trackers[uid_str]["message"] = msg

        # Assemble per-UID results
        results = []
        success_count = 0
        failure_count = 0
        has_rejected = False
        has_retryable = False

        for uid_str, tracker in uid_trackers.items():
            st = tracker["status"] or STATUS_APPLIED
            res = {
                "uid": uid_str,
                "operation": tracker["operation"],
                "status": st,
            }
            if tracker.get("source_revision"):
                res["source_revision"] = tracker["source_revision"]
            if tracker.get("code"):
                res["code"] = tracker["code"]
            if tracker.get("retryable") is not None:
                res["retryable"] = tracker["retryable"]
            if tracker.get("message"):
                res["message"] = tracker["message"]

            results.append(res)

            if st in (STATUS_APPLIED, STATUS_ALREADY_APPLIED, STATUS_STALE_IGNORED):
                success_count += 1
            else:
                failure_count += 1
                if st == STATUS_REJECTED:
                    has_rejected = True
                elif st == STATUS_RETRYABLE:
                    has_retryable = True

            ra = tracker.get("retry_after")
            if ra and (max_retry_after is None or ra > max_retry_after):
                max_retry_after = ra

        # Compute overall status
        if failure_count == 0:
            overall_status = OVERALL_SUCCESS
            status_code = 200
        elif success_count > 0:
            overall_status = OVERALL_PARTIAL
            status_code = 207  # Multi-Status
        else:
            overall_status = OVERALL_ERROR
            # Check predominant error code
            first_err = next((t for t in uid_trackers.values() if t.get("code")), {})
            err_code = first_err.get("code")
            if err_code == CODE_AUTHENTICATION_FAILED:
                status_code = 401
            elif err_code == CODE_AUTHORIZATION_FAILED:
                status_code = 403
            elif err_code == CODE_RATE_LIMITED:
                status_code = 429
            elif err_code == CODE_DOWNSTREAM_TIMEOUT:
                status_code = 503
            elif err_code == CODE_INVALID_REQUEST:
                status_code = 400
            else:
                status_code = 422 if has_rejected else 503

        response_data = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "target_alias": calendar_alias,
            "overall_status": overall_status,
            "events_processed": success_count,
            "results": results,
        }
        if idempotency_key:
            response_data["idempotency_key"] = idempotency_key

        # Legacy backward-compatibility fields
        if top_level_op:
            response_data["operation"] = top_level_op
        legacy_errors = [
            {"uid": r["uid"], "error": r.get("message") or r.get("code")}
            for r in results
            if r["status"] in (STATUS_REJECTED, STATUS_RETRYABLE, STATUS_CONFLICT)
        ]
        if legacy_errors:
            response_data["errors"] = legacy_errors

        # Store idempotency receipt
        if idempotency_key and self.storage:
            await self.storage.record_idempotency(
                idempotency_key=idempotency_key,
                payload_hash=payload_hash or "",
                target_alias=calendar_alias,
                request_id=request_id,
                status_code=status_code,
                response_data=response_data,
            )

        # Notify Home Assistant sensor
        if success_count > 0:
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_UPDATE_ENDPOINT}_{calendar_alias}",
                {
                    "operation": top_level_op or "push",
                    "processed_count": success_count,
                    "request_id": request_id,
                    "overall_status": overall_status,
                    "results": results,
                }
            )

        duration_ms = (time.monotonic() - start_time) * 1000.0
        _LOGGER.info(
            "Push API request completed | request_id=%s target=%s overall=%s processed=%d/%d duration_ms=%.1f",
            request_id, calendar_alias, overall_status, success_count, len(uid_trackers), duration_ms
        )

        headers = {}
        if max_retry_after:
            headers[HEADER_RETRY_AFTER] = str(max_retry_after)

        return web.json_response(response_data, status=status_code, headers=headers)