import logging
import re
import json
from datetime import datetime, date, timedelta, timezone
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.dispatcher import async_dispatcher_send
import homeassistant.util.dt as dt_util
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

from .ical_patch import Event
from .const import SIGNAL_UPDATE_ENDPOINT

_LOGGER = logging.getLogger(__name__)

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

def _convert_ical_to_google(event: Event, raw_event: dict):
    """Strictly map to Google Calendar API format."""
    body = {}
    
    # 1. UID
    uid = getattr(event, "uid", None) or getattr(event, "icaluid", None)
    if uid:
        body["iCalUID"] = str(uid)
        
    # 2. Basic strings
    if getattr(event, "summary", None): body["summary"] = event.summary
    if getattr(event, "description", None): body["description"] = event.description
    if getattr(event, "location", None): body["location"] = event.location

    # 3. Dates
    dtstart = getattr(event, "dtstart", None)
    dtend = getattr(event, "dtend", None)

    # --- FIX: Prevent "Missing End Time" Errors ---
    if dtstart and not dtend:
        if isinstance(dtstart, datetime):
            dtend = dtstart
        elif isinstance(dtstart, date):
            dtend = dtstart + timedelta(days=1)

    if dtstart:
        if isinstance(dtstart, datetime):
            tz_name, safe_dtstart = _get_tz_name_and_dt(dtstart)
            body["start"] = {
                "dateTime": safe_dtstart.isoformat(),
                "timeZone": tz_name
            }
        elif isinstance(dtstart, date):
            body["start"] = {"date": dtstart.isoformat()}

    if dtend:
        if isinstance(dtend, datetime):
            tz_name, safe_dtend = _get_tz_name_and_dt(dtend)
            body["end"] = {
                "dateTime": safe_dtend.isoformat(),
                "timeZone": tz_name
            }
        elif isinstance(dtend, date):
            body["end"] = {"date": dtend.isoformat()}

    # 3b. Exceptions to Recurring Events
    recurrence_id = getattr(event, "recurrence_id", None)
    if recurrence_id:
        if isinstance(recurrence_id, datetime):
            tz_name, safe_rec_id = _get_tz_name_and_dt(recurrence_id)
            body["originalStartTime"] = {
                "dateTime": safe_rec_id.isoformat(),
                "timeZone": tz_name
            }
        elif isinstance(recurrence_id, date):
            body["originalStartTime"] = {"date": recurrence_id.isoformat()}

    # 4. Enums
    status = getattr(event, "status", None)
    if status:
        body["status"] = str(status.value).lower() if hasattr(status, 'value') else str(status).lower()
    
    transparency = getattr(event, "transparency", None)
    if transparency:
        body["transparency"] = str(transparency.value).lower() if hasattr(transparency, 'value') else str(transparency).lower()
        
    classification = getattr(event, "classification", None)
    if classification:
        body["visibility"] = str(classification.value).lower() if hasattr(classification, 'value') else str(classification).lower()

    # 5. RRULE
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

    # 6. Alarms
    alarms_list = raw_event.get("valarm") or raw_event.get("alarms") or []
    if alarms_list and isinstance(alarms_list, list):
        overrides = []
        for alarm_dict in alarms_list:
            if not isinstance(alarm_dict, dict): continue
            
            action = str(alarm_dict.get("action", "")).upper()
            method = "email" if "EMAIL" in action else "popup"
            
            trigger = str(alarm_dict.get("trigger", ""))
            clean_trigger = trigger.lstrip('+-')
            mins = 10 
            
            match = re.match(r'^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$', clean_trigger)
            if match:
                weeks = int(match.group(1) or 0)
                days = int(match.group(2) or 0)
                hours = int(match.group(3) or 0)
                minutes = int(match.group(4) or 0)
                mins = (weeks * 10080) + (days * 1440) + (hours * 60) + minutes
            elif trigger:
                try:
                    trigger_dt = datetime.fromisoformat(trigger.replace('Z', '+00:00'))
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

    # 7. Attendees
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

    # 8. Organizer
    organizer = getattr(event, "organizer", None)
    if organizer:
        email = getattr(organizer, "cal_address", str(organizer))
        if email.lower().startswith("mailto:"):
            email = email[7:]
        if "@" in email:
            body["organizer"] = {"email": email}

    # 9. URL
    url = getattr(event, "url", None)
    if url:
        body["source"] = {"url": str(url), "title": "Original Event Link"}

    # 10. Categories
    categories = getattr(event, "categories", None)
    if categories:
        cat_str = f"\n\nCategories: {', '.join(categories)}"
        body["description"] = body.get("description", "") + cat_str

    return body

class GoogleCalendarPushView(HomeAssistantView):
    """REST API endpoint for pushing events to Google Calendar."""
    
    url = "/api/google_calendar_push/{calendar_alias}"
    name = "api:google_calendar_push"
    requires_auth = True

    def __init__(self, hass, session, calendar_aliases):
        self.hass = hass
        self.session = session
        self.calendar_aliases = calendar_aliases

    def _get_google_service(self):
        credentials = Credentials(
            token=self.session.token["access_token"],
            refresh_token=self.session.token.get("refresh_token"),
            token_uri=self.session.token.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=self.session.token.get("client_id"),
            client_secret=self.session.token.get("client_secret"),
        )
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def _process_operation(self, service, calendar_id, operation, valid_events_data):
        from datetime import datetime, date, timedelta, timezone
        import time
        processed_count = 0
        api_errors = []

        masters_data = []
        exceptions_data = []
        
        # --- PROTECTION 1: Ignore ancient exceptions (older than 60 days)
        # Prevents Google 404 errors on deeply historical virtual instances.
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=60)
        
        for event, raw_event in valid_events_data:
            if getattr(event, "recurrence_id", None):
                exceptions_data.append((event, raw_event))
            else:
                masters_data.append((event, raw_event))

            exceptions = getattr(event, "exceptions", None)
            if exceptions:
                raw_exceptions = raw_event.get("exceptions", {})
                
                if len(exceptions) == len(raw_exceptions):
                    for (exc_key, exc_event), (raw_key, raw_val) in zip(exceptions.items(), raw_exceptions.items()):
                        
                        try:
                            exc_dt = None
                            if isinstance(exc_key, datetime):
                                exc_dt = exc_key.astimezone(timezone.utc)
                            elif isinstance(exc_key, date):
                                exc_dt = datetime.combine(exc_key, datetime.min.time(), tzinfo=timezone.utc)
                            elif isinstance(exc_key, str):
                                exc_dt = datetime.fromisoformat(exc_key.replace('Z', '+00:00')).astimezone(timezone.utc)
                                
                            if exc_dt and exc_dt < cutoff_date:
                                continue # Safely skip pushing ancient history to Google
                        except Exception:
                            pass 
                            
                        if exc_event is None:
                            uid = str(getattr(event, "uid", None) or getattr(event, "icaluid", None))
                            cancel_raw = {
                                "uid": uid,
                                "recurrence-id": raw_key,
                                "dtstart": raw_key, 
                                "dtend": raw_key,   
                                "status": "CANCELLED"
                            }
                            try:
                                cancel_event = Event.model_validate(cancel_raw)
                                exceptions_data.append((cancel_event, cancel_raw))
                            except Exception as e:
                                _LOGGER.error("Validation failed for cancellation exception: %s", e)
                        else:
                            exceptions_data.append((exc_event, raw_val if isinstance(raw_val, dict) else {}))
                else:
                    _LOGGER.warning("Mismatch in exceptions dict length for UID %s", getattr(event, "uid", None))

        def execute_pass(events_data, is_exception_pass):
            nonlocal processed_count
            search_results = {}

            # --- In-Memory Map to prevent Race Conditions ---
            # Keys are UIDs, Values are the newly assigned Google Event IDs
            newly_created_masters = {}
            
            # --- Dictionary to map Request IDs to their JSON bodies for debugging ---
            req_id_to_body = {}
            
            def search_callback(request_id, response, exception):
                if exception is not None:
                    api_errors.append({"uid": request_id, "error": str(exception)})
                else:
                    search_results[request_id] = response.get("items", [])

            # --- PROTECTION 2: Chunk Execution to honor Google API Rate Limits
            def execute_in_chunks(batch_reqs, chunk_size=50):
                for i in range(0, len(batch_reqs), chunk_size):
                    chunk = batch_reqs[i:i + chunk_size]
                    batch = service.new_batch_http_request()
                    for req, req_id, cb in chunk:
                        batch.add(req, request_id=req_id, callback=cb)
                    try:
                        batch.execute()
                        time.sleep(0.3) # Micro-backoff to stay under Burst Limits
                    except Exception as e:
                        _LOGGER.error("Chunk execution failed: %s", e)
                        api_errors.append({"error": f"Batch chunk failed: {e}"})

            search_reqs = []
            seen_uids = set()
            
            for ev, _ in events_data:
                uid = str(getattr(ev, "uid", None) or getattr(ev, "icaluid", None))
                if not uid or uid in seen_uids:
                    continue
                seen_uids.add(uid)
                req = service.events().list(calendarId=calendar_id, iCalUID=uid, showDeleted=True)
                search_reqs.append((req, uid, search_callback))

            if search_reqs:
                execute_in_chunks(search_reqs, chunk_size=50)

            def mutate_callback(request_id, response, exception):
                nonlocal processed_count
                original_uid = request_id.rsplit('_', 1)[0] if '_' in request_id else request_id
                
                if exception is not None:
                    error_msg = str(exception)
                    sent_body = req_id_to_body.get(request_id, {})
                    _LOGGER.error("Google API Mutation Error for UID %s: %s | Payload sent: %s", original_uid, error_msg, json.dumps(sent_body))
                    api_errors.append({"uid": original_uid, "error": error_msg})
                else:
                    processed_count += 1
                    if not is_exception_pass and response and "id" in response:
                         newly_created_masters[original_uid] = response["id"]

            uid_operations = {}

            for index, (ev, r_ev) in enumerate(events_data):
                uid = str(getattr(ev, "uid", None) or getattr(ev, "icaluid", None))
                
                # Fetch search results. If missing, check our newly created masters map!
                items = search_results.get(uid, [])
                body = _convert_ical_to_google(ev, r_ev)
                
                master_item_id = None
                master_item = None
                for item in items:
                    if "originalStartTime" not in item:
                        master_item_id = item["id"]
                        master_item = item
                        break
                        
                # Race Condition Fix: If search didn't find the master, pull it from memory
                if not master_item_id and uid in newly_created_masters:
                     master_item_id = newly_created_masters[uid]
                        
                target_event_id = None
                
                if is_exception_pass:
                    # Help computation by converting offset to master offset
                    if master_item and "start" in master_item:
                        master_tz = master_item["start"].get("timeZone")
                        if master_tz and "originalStartTime" in body and "dateTime" in body["originalStartTime"]:
                            try:
                                orig_dt_str = body["originalStartTime"]["dateTime"]
                                dt = datetime.fromisoformat(orig_dt_str.replace('Z', '+00:00'))
                                tz_obj = dt_util.get_time_zone(master_tz)
                                if tz_obj:
                                    dt_local = dt.astimezone(tz_obj)
                                    body["originalStartTime"]["dateTime"] = dt_local.isoformat()
                                    body["originalStartTime"]["timeZone"] = master_tz
                            except Exception as e:
                                pass

                    # 1. Look for explicitly overridden exception
                    for item in items:
                        if "originalStartTime" in item:
                            in_start = body.get("originalStartTime", {})
                            go_start = item.get("originalStartTime", {})
                            in_dt = in_start.get("dateTime")
                            go_dt = go_start.get("dateTime")
                            
                            if in_dt and go_dt:
                                try:
                                    dt1 = datetime.fromisoformat(in_dt.replace('Z', '+00:00'))
                                    dt2 = datetime.fromisoformat(go_dt.replace('Z', '+00:00'))
                                    if dt1 == dt2:
                                        target_event_id = item["id"]
                                        break
                                except ValueError:
                                    if in_dt == go_dt:
                                        target_event_id = item["id"]
                                        break
                            elif in_start.get("date") and go_start.get("date"):
                                if in_start.get("date") == go_start.get("date"):
                                    target_event_id = item["id"]
                                    break
                    
                    # 2. Compute Virtual Instance ID
                    if not target_event_id and master_item_id:
                        orig_time = body.get("originalStartTime", {})
                        try:
                            if 'dateTime' in orig_time:
                                dt = datetime.fromisoformat(orig_time['dateTime'].replace('Z', '+00:00'))
                                dt_utc = dt.astimezone(timezone.utc)
                                time_str = dt_utc.strftime('%Y%m%dT%H%M%SZ')
                                target_event_id = f"{master_item_id}_{time_str}"
                            elif 'date' in orig_time:
                                d = date.fromisoformat(orig_time['date'])
                                time_str = d.strftime('%Y%m%d')
                                target_event_id = f"{master_item_id}_{time_str}"
                        except Exception as e:
                            _LOGGER.error("Instance ID computation failed for UID %s: %s", uid, e)
                            
                    if not target_event_id:
                         # Still missing? That means the Master wasn't created. Drop the orphan.
                        _LOGGER.warning("Master event %s not found in search or memory. Dropping orphaned exception.", uid)
                        continue 
                    
                    # --- FIX: Prevent 400 Bad Request on Exceptions ---
                    # Virtual instances cannot contain master properties like iCalUID or recurrence arrays
                    body.pop("iCalUID", None)
                    body.pop("recurrence", None) 
                    
                    # --- FIX: Prevent Strict originalStartTime Validation mismatches ---
                    # Because we are identifying the instance perfectly via the target_event_id in the URL, 
                    # omitting originalStartTime from the body completely bypasses Google's formatting constraints.
                    body.pop("originalStartTime", None)
                    
                else:
                    target_event_id = master_item_id

                # --- FIX: Prevent Zero-Duration Errors on Cancellations ---
                # Google API strictly rejects events where start == end. 
                # Cancellations do not require dates, so we pop them completely.
                if body.get("status") == "cancelled":
                    body.pop("start", None)
                    body.pop("end", None)

                unique_req_id = f"{uid}_{index}"
                req_id_to_body[unique_req_id] = body # Store for error logging
                
                if target_event_id:
                    resource_key = target_event_id
                else:
                    orig_time = body.get("originalStartTime", {})
                    time_str = orig_time.get("dateTime", orig_time.get("date", "master"))
                    resource_key = f"insert_{uid}_{time_str}"

                mutation_op = None
                try:
                    if operation == "add":
                        if target_event_id:
                            target_item = next((i for i in items if i["id"] == target_event_id), {})
                            if target_item.get("status") == "cancelled":
                                body.setdefault("status", "confirmed")
                            mutation_op = service.events().update(calendarId=calendar_id, eventId=target_event_id, body=body)
                        else:
                            mutation_op = service.events().insert(calendarId=calendar_id, body=body)

                    elif operation == "update":
                        if not target_event_id:
                            api_errors.append({"uid": uid, "error": "Event not found for update"})
                            continue
                        
                        target_item = next((i for i in items if i["id"] == target_event_id), {})
                        if target_item.get("status") == "cancelled":
                            body.setdefault("status", "confirmed")
                        mutation_op = service.events().update(calendarId=calendar_id, eventId=target_event_id, body=body)

                    elif operation == "remove":
                        if target_event_id:
                            target_item = next((i for i in items if i["id"] == target_event_id), {})
                            if target_item.get("status") != "cancelled":
                                mutation_op = service.events().delete(calendarId=calendar_id, eventId=target_event_id)
                            else:
                                processed_count += 1
                                continue
                        else:
                            processed_count += 1
                            continue
                    
                    if mutation_op:
                        if uid not in uid_operations:
                            uid_operations[uid] = {}
                        uid_operations[uid][resource_key] = (mutation_op, unique_req_id)
                                
                except Exception as e:
                    _LOGGER.error("Mutation preparation error for UID %s: %s", uid, e)
                    api_errors.append({"uid": uid, "error": str(e)})

            # Safely extract ops without conflicts and chunk them into batches of 50
            uid_op_lists = {u: list(ops.values()) for u, ops in uid_operations.items()}
            max_ops = max([len(ops) for ops in uid_op_lists.values()]) if uid_op_lists else 0

            for i in range(max_ops):
                mutate_reqs = []
                for u, ops in uid_op_lists.items():
                    if i < len(ops):
                        mut_op, req_id = ops[i]
                        mutate_reqs.append((mut_op, req_id, mutate_callback))
                        
                if mutate_reqs:
                    execute_in_chunks(mutate_reqs, chunk_size=50)

        # --- Execution Block ---
        if masters_data:
            execute_pass(masters_data, is_exception_pass=False)
            
            # Force a micro-sleep to ensure the API has a moment to settle before exceptions fire
            if exceptions_data:
                 import time
                 time.sleep(0.5) 
                 
        if exceptions_data:
            execute_pass(exceptions_data, is_exception_pass=True)

        return processed_count, api_errors
    
    async def post(self, request, calendar_alias):
        calendar_id = self.calendar_aliases.get(calendar_alias)
        
        if not calendar_id:
            return web.Response(status=404, text=f"Endpoint alias '{calendar_alias}' is not configured.")

        try:
            data = await request.json()
        except ValueError:
            return web.Response(status=400, text="Invalid JSON payload")

        operation = data.get("operation", "").lower()
        raw_events = data.get("events", [])

        if operation not in ["add", "update", "remove"]:
            return web.Response(status=400, text="Invalid operation.")
        if not isinstance(raw_events, list):
            return web.Response(status=400, text="'events' must be a list.")

        valid_events_data = []
        validation_errors = []
        
        for raw_event in raw_events:
            try:
                validated_event = Event.model_validate(raw_event)
                valid_events_data.append((validated_event, raw_event))
            except Exception as e:
                uid = raw_event.get("uid", "UNKNOWN_UID")
                _LOGGER.error("Pydantic validation failed for event %s: %s", uid, str(e))
                validation_errors.append({"uid": uid, "error": f"Validation failed: {str(e)}"})

        if not valid_events_data:
            return web.json_response({
                "status": "error",
                "operation": operation,
                "events_processed": 0,
                "target_alias": calendar_alias,
                "errors": validation_errors
            }, status=400)

        if not self.session.valid_token:
            await self.session.async_ensure_token_valid()

        service = await self.hass.async_add_executor_job(self._get_google_service)
        
        processed_count, api_errors = await self.hass.async_add_executor_job(
            self._process_operation, service, calendar_id, operation, valid_events_data
        )

        all_errors = validation_errors + api_errors

        if processed_count > 0:
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_UPDATE_ENDPOINT}_{calendar_alias}",
                operation,
                processed_count
            )

        if all_errors:
            status_code = 207 if processed_count > 0 else 400
            return web.json_response({
                "status": "partial_success" if processed_count > 0 else "error",
                "operation": operation,
                "events_processed": processed_count,
                "target_alias": calendar_alias,
                "errors": all_errors
            }, status=status_code)

        return web.json_response({
            "status": "success", 
            "operation": operation, 
            "events_processed": processed_count,
            "target_alias": calendar_alias
        })