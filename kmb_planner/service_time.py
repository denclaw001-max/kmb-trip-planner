"""Service-day resolution for the time-aware planner (Phase 4).

GTFS convention: a service day starts at 03:00; times past midnight
belong to the previous service day and are counted beyond 24:00
(01:00 Tuesday = Monday's service day, clock 1500). Bundle windows are
keyed by weekday-flag patterns (Mon..Sun, '1' = runs that weekday),
with per-date exceptions computed at sync time from calendar_dates
(public holidays etc.).
"""
from datetime import timedelta

SERVICE_DAY_START_HOUR = 3


def all_flag_patterns(graph):
    """Every weekday-flag pattern any graph line's windows use."""
    out = set()
    for line in graph.lines.values():
        if line.get("windows"):
            out.update(line["windows"].keys())
    return out


def active_flags(all_patterns, exceptions, service_date):
    """Patterns active on a service date: weekday bit, adjusted by the
    date's exceptions (patterns added / removed for that date)."""
    wd = service_date.weekday()  # Mon=0 .. Sun=6
    active = {p for p in all_patterns if p[wd] == "1"}
    exc = exceptions.get(service_date.strftime("%Y%m%d"))
    if exc:
        active -= set(exc.get("remove", []))
        active |= set(exc.get("add", []))
    return active


def service_context(now, all_patterns, exceptions):
    """now: aware datetime (HKT). Returns (clock_min, active_patterns,
    service_date). clock_min is minutes since service-day midnight and
    may exceed 1440 in the early hours."""
    mins = now.hour * 60 + now.minute + now.second / 60.0
    if now.hour < SERVICE_DAY_START_HOUR:
        service_date = (now - timedelta(days=1)).date()
        clock = mins + 1440.0
    else:
        service_date = now.date()
        clock = mins
    return clock, active_flags(all_patterns, exceptions, service_date), service_date
