import logging
import re
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
    if dtstart:
        if isinstance(dtstart, datetime):
            tz_name, safe_dtstart = _get_tz_name_and_dt(dtstart)
            body["start"] = {
                "dateTime": safe_dtstart.isoformat(),
                "timeZone": tz_name
            }
        elif isinstance(dtstart, date):
            body["start"] = {"date": dtstart.isoformat()}

    dtend = getattr(event, "dtend", None)
    if dtend:
        if isinstance(dtend, datetime):
            tz_name, safe_dtend = _get_tz_name_and_dt(dtend)
            body["end"] = {
                "dateTime": safe_dtend.isoformat(),
                "timeZone": tz_name
            }
        elif isinstance(dtend, date):
            body["end"] = {"date": dtend.isoformat()}

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

    # 6. Alarms - Bypassing Pydantic's silent drop by reading the raw dictionary directly
    alarms_list = raw_event.get("valarm") or raw_event.get("alarms") or []
    if alarms_list and isinstance(alarms_list, list):
        overrides = []
        for alarm_dict in alarms_list:
            if not isinstance(alarm_dict, dict): continue
            
            action = str(alarm_dict.get("action", "")).upper()
            method = "email" if "EMAIL" in action else "popup"
            
            trigger = str(alarm_dict.get("trigger", ""))
            clean_trigger = trigger.lstrip('+-')
            mins = 10 # Fallback default
            
            # Regex parse the ISO duration string (e.g., PT15M -> 15 mins)
            match = re.match(r'^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$', clean_trigger)
            if match:
                weeks = int(match.group(1) or 0)
                days = int(match.group(2) or 0)
                hours = int(match.group(3) or 0)
                minutes = int(match.group(4) or 0)
                mins = (weeks * 10080) + (days * 1440) + (hours * 60) + minutes
            elif trigger:
                # Fallback: Absolute datetime strings. Do the math against dtstart.
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

            # Apply strict Google constraints (Min 0, Max 40320 minutes)
            mins = max(0, min(mins, 40320))
            
            override_entry = {"method": method, "minutes": mins}
            if override_entry not in overrides:
                overrides.append(override_entry)
                
        if overrides:
            body["reminders"] = {
                "useDefault": False,
                "overrides": overrides[:5] # Google caps at 5 overrides
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
        processed_count = 0
        api_errors = []
        search_results = {}

        # --- BATCH 1: Search for all existing events by UID ---
        def search_callback(request_id, response, exception):
            if exception is not None:
                api_errors.append({"uid": request_id, "error": str(exception)})
            else:
                search_results[request_id] = response.get("items", [])

        search_batch = service.new_batch_http_request()
        searches_added = 0
        
        for event, raw_event in valid_events_data:
            uid = getattr(event, "uid", None) or getattr(event, "icaluid", None)
            if not uid:
                api_errors.append({"error": "Missing UID"})
                continue
                
            search_batch.add(
                service.events().list(calendarId=calendar_id, iCalUID=str(uid), showDeleted=True),
                request_id=str(uid),
                callback=search_callback
            )
            searches_added += 1

        if searches_added > 0:
            try:
                search_batch.execute()
            except Exception as e:
                _LOGGER.error("Batch search failed: %s", e)
                return processed_count, [{"error": f"Batch search failed: {e}"}]

        # --- BATCH 2: Process Add, Update, and Remove Operations ---
        def mutate_callback(request_id, response, exception):
            nonlocal processed_count
            if exception is not None:
                api_errors.append({"uid": request_id, "error": str(exception)})
            else:
                processed_count += 1

        mutate_batch = service.new_batch_http_request()
        mutations_added = 0

        for event, raw_event in valid_events_data:
            uid = str(getattr(event, "uid", None) or getattr(event, "icaluid", None))
            if uid not in search_results:
                continue # Skip if the search phase encountered an error for this UID
                
            items = search_results[uid]
            body = _convert_ical_to_google(event, raw_event)
            
            try:
                if operation == "add":
                    if items:
                        _LOGGER.info("Event %s already exists. Updating instead.", uid)
                        event_id = items[0]["id"]
                        if items[0].get("status") == "cancelled":
                            body.setdefault("status", "confirmed")
                        mutate_batch.add(
                            service.events().update(calendarId=calendar_id, eventId=event_id, body=body),
                            request_id=uid, callback=mutate_callback
                        )
                    else:
                        mutate_batch.add(
                            service.events().insert(calendarId=calendar_id, body=body),
                            request_id=uid, callback=mutate_callback
                        )
                    mutations_added += 1

                elif operation == "update":
                    if not items:
                        _LOGGER.warning("Could not find event with UID %s to update.", uid)
                        api_errors.append({"uid": uid, "error": "Event not found for update"})
                        continue
                    
                    event_id = items[0]["id"]
                    if items[0].get("status") == "cancelled":
                        body.setdefault("status", "confirmed")
                    mutate_batch.add(
                        service.events().update(calendarId=calendar_id, eventId=event_id, body=body),
                        request_id=uid, callback=mutate_callback
                    )
                    mutations_added += 1

                elif operation == "remove":
                    if not items:
                        _LOGGER.info("Event %s not found. Gracefully ignoring remove.", uid)
                        processed_count += 1
                    else:
                        event_id = items[0]["id"]
                        if items[0].get("status") == "cancelled":
                            _LOGGER.info("Event %s already deleted. Gracefully ignoring.", uid)
                            processed_count += 1
                        else:
                            mutate_batch.add(
                                service.events().delete(calendarId=calendar_id, eventId=event_id),
                                request_id=uid, callback=mutate_callback
                            )
                            mutations_added += 1
                            
            except Exception as e:
                _LOGGER.error("Error building mutation for event %s: %s", uid, str(e))
                api_errors.append({"uid": uid, "error": str(e)})
                
        if mutations_added > 0:
            try:
                mutate_batch.execute()
            except Exception as e:
                _LOGGER.error("Batch execution failed: %s", e)
                api_errors.append({"error": f"Batch execution failed: {e}"})
                
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
                # Pack the validated event and raw dictionary into a tuple
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