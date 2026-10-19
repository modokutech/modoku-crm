"""Per-day training times (Fix204).

A class (course_sessions) or a quotation line (quotation_items) normally has
one time for every day: start_date..end_date, each day training_time. When
the days differ (1.5 days: day 1 9am-5pm, day 2 9am-1pm) or aren't back to
back (10 Nov and 17 Nov), the exact days are stored in `day_times`, a JSON
list of {"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM"}. start_date,
end_date and training_time are still kept (first date, last date, first
day's time) so everything that only reads those keeps working.

Pure helpers, no Flask/db imports, so any module can use them.
"""
import json
import re
from datetime import date, datetime, timedelta


def _d(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _hhmm(value):
    value = (value or "").strip()
    return value if re.fullmatch(r"\d{2}:\d{2}", value) else ""


def fmt_time_12h(hhmm):
    """'09:00' -> '9:00 AM'."""
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
    except (ValueError, TypeError):
        return hhmm or ""


def _split_time(training_time):
    # Local import: sessions imports a lot; this module must stay import-light.
    from .sessions import split_training_time
    return split_training_time(training_time) if training_time else ("", "")


def parse_day_times(value):
    """day_times JSON (or a list) -> clean, date-sorted list of
    {"date", "start", "end"}; [] when unset or unreadable."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    out, seen = [], set()
    for day in value if isinstance(value, list) else []:
        if not isinstance(day, dict):
            continue
        d = _d(day.get("date"))
        if d is None or d in seen:
            continue
        seen.add(d)
        out.append({"date": d.isoformat(), "start": _hhmm(day.get("start")), "end": _hhmm(day.get("end"))})
    out.sort(key=lambda x: x["date"])
    return out


def _get(row, key):
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def days(start_date, end_date, training_time, day_times=None):
    """Every training day as (date, start "HH:MM", end "HH:MM")."""
    parsed = parse_day_times(day_times)
    if parsed:
        return [(_d(x["date"]), x["start"], x["end"]) for x in parsed]
    start, end = _d(start_date), _d(end_date)
    if start is None:
        return []
    if end is None or end < start:
        end = start
    s, e = _split_time(training_time)
    return [(start + timedelta(days=i), s, e) for i in range((end - start).days + 1)]


def row_days(row, date_key="start_date", end_key="end_date", time_key="training_time", days_key="day_times"):
    return days(_get(row, date_key), _get(row, end_key), _get(row, time_key), _get(row, days_key))


def is_varied(day_list):
    """True when the days don't share one time, or aren't back to back."""
    if len(day_list) < 2:
        return False
    if len({(s, e) for _d_, s, e in day_list}) > 1:
        return True
    return any((b[0] - a[0]).days != 1 for a, b in zip(day_list, day_list[1:]))


def _short(d, with_year):
    return d.strftime("%-d %b %Y") if with_year else d.strftime("%-d %b")


def dates_text(day_list):
    """'10 & 17 Nov 2026' / '10, 12 & 17 Nov 2026' / '28 Nov & 5 Dec 2026'
    for days that aren't back to back. Back-to-back days: None (callers use
    the normal fmtdaterange)."""
    if len(day_list) < 2 or not any((b[0] - a[0]).days != 1 for a, b in zip(day_list, day_list[1:])):
        return None
    ds = [d for d, _s, _e in day_list]
    same_year = len({d.year for d in ds}) == 1
    same_month = same_year and len({d.month for d in ds}) == 1
    if same_month:
        parts = [str(d.day) for d in ds[:-1]] + [ds[-1].strftime("%-d %b %Y")]
    else:
        parts = [_short(d, not same_year) for d in ds[:-1]] + [ds[-1].strftime("%-d %b %Y")]
    return ", ".join(parts[:-1]) + " & " + parts[-1]


def times_differ(day_list):
    return len({(s, e) for _d_, s, e in day_list}) > 1


def time_text(day_list, fallback=None, sep=" · "):
    """The time to show: one time when every day is the same ('9:00 AM -
    5:00 PM', i.e. fallback = the stored training_time), or each day with its
    date when they differ: '10 Nov: 9:00 AM - 5:00 PM · 11 Nov: 9:00 AM - 1:00 PM'."""
    if not times_differ(day_list):
        if day_list and day_list[0][1] and day_list[0][2] and not fallback:
            return f"{fmt_time_12h(day_list[0][1])} - {fmt_time_12h(day_list[0][2])}"
        return fallback
    parts = []
    for d, s, e in day_list:
        t = f"{fmt_time_12h(s)} - {fmt_time_12h(e)}" if s and e else "time to be confirmed"
        parts.append(f"{d.strftime('%-d %b')}: {t}")
    return sep.join(parts)


def row_time_text(row, sep=" · ", **keys):
    return time_text(row_days(row, **keys), _get(row, keys.get("time_key", "training_time")), sep=sep)


def duration_label(day_list):
    """'1.5 days' from the hours: a day of 6 hours or more counts as a full
    day, a shorter one as half a day. '' when there are no times."""
    total = 0.0
    for _d_, s, e in day_list:
        if not (s and e):
            return ""
        hours = (datetime.strptime(e, "%H:%M") - datetime.strptime(s, "%H:%M")).seconds / 3600
        total += 1 if hours >= 6 else 0.5 if hours > 0 else 0
    if not total:
        return ""
    total = int(total) if total.is_integer() else total
    return f"{total} day" if total <= 1 else f"{total} days"


def to_json(day_list):
    return json.dumps([{"date": d.isoformat(), "start": s, "end": e} for d, s, e in day_list])


def from_form(value, start_date=None):
    """The per-day editor's hidden JSON -> (day_times JSON or None, first date,
    last date, first day's (start, end)). None when the editor wasn't used or
    holds fewer than 2 days."""
    parsed = parse_day_times(value)
    if len(parsed) < 2:
        return None, None, None, None
    return (json.dumps(parsed), parsed[0]["date"], parsed[-1]["date"], (parsed[0]["start"], parsed[0]["end"]))


def _keys(row):
    try:
        keys = row.keys()
    except AttributeError:
        keys = []
    if "item_date" in keys:
        return {"date_key": "item_date", "end_key": "item_date_end", "time_key": "item_time"}
    return {}


def jinja_time(row, sep=" · "):
    """{{ row|sched_time }}: the time to show for a class or quotation line."""
    keys = _keys(row)
    return row_time_text(row, sep=sep, **keys)


def jinja_dates(row):
    """{{ row|sched_dates }}: '10 & 17 Nov 2026' for days that aren't back to
    back, else the normal date range."""
    from . import fmtdaterange
    keys = _keys(row)
    text = dates_text(row_days(row, **keys))
    if text:
        return text
    return fmtdaterange(_get(row, keys.get("date_key", "start_date")), _get(row, keys.get("end_key", "end_date")))
