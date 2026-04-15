import re
import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Union, List, Optional, Annotated

from pydantic import field_serializer, FieldSerializationInfo, BeforeValidator, Field

from ical.parsing.property import ParsedProperty, ParsedPropertyParameter
from ical.types.data_types import serialize_field, DATA_TYPE
from ical.types.date import DATE_REGEX
from ical.types.date_time import DATETIME_REGEX, TZID, ATTR_VALUE
from ical.util import dtstamp_factory
from ical.event import Event as iCalEvent

def contains_regex(regex: re.Pattern, value: str) -> re.Match | None:
    return re.search(regex.pattern.removeprefix('^').removesuffix('$'), value)

def parse_date_and_datetime(value: str | datetime.date | Dict[str,str] | None) -> datetime.date | None:
    """Coerce str or ICS formatted JSON dict into date and datetime value."""
    if isinstance(value, dict):        
        value = f"DATETIME;{TZID}={value.get(TZID)}:{value.get(ATTR_VALUE)}"

    if isinstance(value, str):
        iana = None
        iso_part = value

        # 1. Extract RFC 9775 bracketed timezone (e.g., "[America/Los_Angeles]")
        bracket_match = re.search(r'\[(.*?)\]$', value)
        if bracket_match:
            iana = bracket_match.group(1)
            iso_part = value[:bracket_match.start()] # Strip the bracketed part

        # 2. Try native Python parsing for ISO 8601 
        # (This flawlessly handles Z, spaces, offsets, and 6-digit microseconds)
        try:
            # .replace('Z', '+00:00') guarantees compatibility across all Python 3.x minor versions
            dt = datetime.datetime.fromisoformat(iso_part.replace('Z', '+00:00'))
            if iana:
                dt = dt.replace(tzinfo=ZoneInfo(iana))
            return dt
        except ValueError:
            pass

        try:
            dt = datetime.date.fromisoformat(iso_part)
            return dt
        except ValueError:
            pass

        # 3. Fallback to ICS format parsing (e.g. 20260408T093500Z)
        if ics_match := contains_regex(DATETIME_REGEX, iso_part):
            dt_type = datetime.datetime
        elif ics_match := contains_regex(DATE_REGEX, iso_part):
            dt_type = datetime.date
        else:
            # Unparsable, let Pydantic throw a clean error
            return value 
        
        parsed_value = ics_match.group()
        
        params: List[ParsedPropertyParameter] = []
        if iana:
            params.append(ParsedPropertyParameter(TZID, [iana]))

        prop = ParsedProperty(name=dt_type.__name__, value=parsed_value, params=params)
        return DATA_TYPE.parse_property(dt_type, prop)

    return value

class Event(iCalEvent):
    dtstamp: Annotated[
        Union[datetime.date, datetime.datetime],
        BeforeValidator(parse_date_and_datetime),
    ] = Field(default_factory=lambda: dtstamp_factory())

    dtstart: Annotated[
        Union[datetime.date, datetime.datetime, None],
        BeforeValidator(parse_date_and_datetime),
    ] = Field(default=None)

    dtend: Annotated[
        Union[datetime.date, datetime.datetime, None],
        BeforeValidator(parse_date_and_datetime),
    ] = None

    created: Annotated[ 
        Optional[datetime.datetime], 
        BeforeValidator(parse_date_and_datetime),
    ] = None
    
    last_modified: Annotated[ 
        Optional[datetime.datetime], 
        BeforeValidator(parse_date_and_datetime),
    ] = Field(
        alias="last-modified",
        default=None,
    )

    # FIX: Explicitly patching recurrence_id so Pydantic doesn't crash on it!
    recurrence_id: Annotated[
        Union[datetime.datetime, datetime.date, None],
        BeforeValidator(parse_date_and_datetime),
    ] = Field(
        alias="recurrence-id",
        default=None,
    )

    @field_serializer('*')
    def serialize_fields(self, value: Any, info: FieldSerializationInfo) -> Any:
        if not (info.context and info.context.get("ics")):
            if isinstance(value, datetime.datetime):
                tz_name = value.tzname()
                if isinstance(value.tzinfo, ZoneInfo):
                    tz_name = value.tzinfo.key or "UTC"
                return f"{value.isoformat()}[{tz_name}]"
            return value
        return serialize_field(self, value, info)