"""Activity Heatmap — a hot/cold visual read of how busy the year has been,
in two views:

- Classes: a GitHub-contributions-style calendar for the selected year,
  one cell per day, shaded by how many classes (any non-Cancelled status)
  were running that day (i.e. the day falls within the class's
  start_date..end_date span).
- Trainers: a trainer-by-month grid for the same year, shaded by training
  days delivered — reusing the exact same "Day" figure already used on the
  Trainer Log's Yearly View (Confirmed POs), so the two pages never
  disagree with each other over what a "day delivered" means.

Both are read-only, computed live from existing data — nothing new is
stored.
"""
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request

from . import db
from .auth import login_required
from .trainer_utilization import MONTH_NAMES, _yearly_pivot

bp = Blueprint("heatmap", __name__, url_prefix="/heatmap")


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _available_years():
    rows = db.query(
        "SELECT DISTINCT substr(start_date, 1, 4) AS yr FROM course_sessions WHERE start_date IS NOT NULL"
    )
    years = {int(r["yr"]) for r in rows if r["yr"] and r["yr"].isdigit()}
    years.add(date.today().year)
    return sorted(years, reverse=True)


def _classes_calendar(year):
    """One entry per calendar day of `year`, with the count of classes
    (any status except Cancelled) running that day, grouped into weeks
    (lists of 7 days, Monday-first) for a GitHub-style grid."""
    rows = db.query(
        "SELECT start_date, end_date FROM course_sessions WHERE status != 'Cancelled' "
        "AND start_date IS NOT NULL AND (start_date <= ? AND COALESCE(end_date, start_date) >= ?)",
        (f"{year}-12-31", f"{year}-01-01"),
    )
    counts = {}
    for row in rows:
        start = _parse_date(row["start_date"])
        end = _parse_date(row["end_date"]) or start
        if not start:
            continue
        if end < start:
            end = start
        day = start
        # Cap the span so a bad/garbage end_date can never spin this loop
        # into effectively-infinite work.
        for _ in range(366):
            if day > end:
                break
            if day.year == year:
                counts[day] = counts.get(day, 0) + 1
            day += timedelta(days=1)

    jan1 = date(year, 1, 1)
    dec31 = date(year, 12, 31)
    grid_start = jan1 - timedelta(days=jan1.weekday())  # back up to Monday
    grid_end = dec31 + timedelta(days=(6 - dec31.weekday()))

    weeks = []
    day = grid_start
    while day <= grid_end:
        week = []
        for _ in range(7):
            week.append({
                "date": day,
                "in_year": day.year == year,
                "count": counts.get(day, 0),
            })
            day += timedelta(days=1)
        weeks.append(week)

    max_count = max(counts.values()) if counts else 0
    return weeks, max_count


def _level(count, max_count):
    """0-4 shading bucket for a heatmap cell, same idea as GitHub's
    contribution graph."""
    if count <= 0 or max_count <= 0:
        return 0
    ratio = count / max_count
    if ratio >= 0.75:
        return 4
    if ratio >= 0.5:
        return 3
    if ratio >= 0.25:
        return 2
    return 1


@bp.route("/")
@login_required
def index():
    year = request.args.get("year", type=int) or date.today().year
    weeks, max_count = _classes_calendar(year)
    for week in weeks:
        for cell in week:
            cell["level"] = _level(cell["count"], max_count)

    trainer_pivot = _yearly_pivot(year)
    trainer_max_days = max((max(m["days"] for m in t["months"]) for t in trainer_pivot), default=0)
    for t in trainer_pivot:
        for m in t["months"]:
            m["level"] = _level(m["days"], trainer_max_days)

    return render_template(
        "heatmap/index.html", weeks=weeks, year=year, available_years=_available_years(),
        trainer_pivot=trainer_pivot, month_names=MONTH_NAMES,
        month_labels=["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    )
