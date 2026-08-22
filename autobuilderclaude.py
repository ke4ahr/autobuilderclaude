#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# autobuilderclaude v1.10.0
# Copyright (C) 2026 Kris Kirby
# https://github.com/ke4ahr/autobuilderclaude
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# ---------------------------------------------------------------------------
# autobuilderclaude -- Document-driven Claude task runner (format v1).
#
# Parses an implementation plan (autobuilderclaude format v1) and executes tasks
# via the claude CLI. Per-task model is read from the plan document.
# All I/O to and from claude is captured to timestamped log files.
# Output token counts are included in the per-task output line.
# On a 429/rate-limit response that contains a
# reset time, the script sleeps until (reset_time + 1 minute) and retries
# the failing task automatically. Sleeps longer than 24 hours are supported;
# a progress line is printed every 2 hours during the wait.
#
# Tasks may also be restricted to specific day-of-week and/or time-of-day
# execution windows via a plan "ExecWindow:" field or a config "exec_window:" key.
# Format per entry: "[DAY[,DAY...] ]HH:MM-HH:MM [TZ]"; multiple entries separated by ";".
# Day abbreviations: Mo Tu We Th Fr Sa Su (case-insensitive). Omit days for all days.
# Example: "Tu,Th 18:00-06:00 America/Chicago" -- Tue+Thu nights 18:00-06:00.
# Outside the window, the script sleeps until the window opens before each attempt,
# including attempts after a rate-limit retry wake-up. Per-task ExecWindow:
# overrides the config default; omit both for unrestricted execution.
#
# Ctrl-C (SIGINT) is caught cleanly at all blocking points (communicate, sleep).
# The current subprocess is terminated, partial output is written to a log file,
# and the run exits with code 130 (POSIX convention: 128 + signal 2). All waits,
# retries, abortions, and terminations are logged to run_events.txt in the log dir.
#
# The common context (working directory, preamble, license header) is the same for
# every task in a run. It is written once to common_context.txt and referenced by
# each task's prompt log, which contains only the task-specific body. In dry-run
# mode the common context is printed once, not once per task.
#
# Task elapsed time reports and totals count only subprocess execution time and
# exclude all sleep and wait time (rate-limit, exec-window, retry waits).
#
# Usage:
#   autobuilderclaude --input PLAN [--template TEMPLATE] [--config CONFIG] [OPTIONS]
#
# Options:
#   --input PLAN          Implementation plan .md file (required)
#   --template TEMPLATE   YAML base defaults (overridden by plan Build Config)
#   --config CONFIG       YAML config file (overrides plan Build Config and --template)
#   --task N              Run only task N (integer) or "verify"
#   --start-task N        Start at task N and run through the remaining tasks
#   --stop-after N        Stop after task N completes (inclusive)
#   --model MODEL         Override per-task model (haiku|sonnet|opus or full ID)
#   --effort LEVEL        Global effort override (low|medium|high|xhigh|max);
#                         overrides per-task Effort: field and config effort key
#   --parallel N          Number of tasks to run concurrently (default: 1)
#   --dry-run             Print prompts without calling claude
#   --list                List tasks and models, then exit
#   --help
#   [any other flags]     Passed through to the claude CLI unchanged
#
# Plan format: see autobuilderclaude_plan_template_v1.md
# Config format: see autobuilderclaude_config_v1.yaml
# ---------------------------------------------------------------------------

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError as _ZINotFoundError
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False
    ZoneInfo = None
    _ZINotFoundError = Exception

try:
    import yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

# ---------------------------------------------------------------------------
# Default model IDs -- overridden by config models dict
# ---------------------------------------------------------------------------
DEFAULT_MODEL_IDS = {
    'haiku':  'claude-haiku-4-5-20251001',
    'sonnet': 'claude-sonnet-4-6',
    'opus':   'claude-opus-4-7',
}

# Exit code for Ctrl-C / SIGINT interruption (POSIX: 128 + signal 2).
RC_INTERRUPTED = 130

# Set when Ctrl-C is received; workers check this between retry attempts.
_interrupt_event = threading.Event()

# ---------------------------------------------------------------------------
# Rate-limit detection and retry
# ---------------------------------------------------------------------------

_RATE_LIMIT_RE = re.compile(
    r'hit your (?:\w+\s+){0,3}limit|usage limit|rate.?limit|exceeded.*limit|limit.*exceeded',
    re.IGNORECASE,
)

_SPEND_LIMIT_RE = re.compile(
    r'monthly\s+spend\s+limit|spend\s+limit.*claude\.ai/settings',
    re.IGNORECASE,
)

# Patterns for extracting the reset datetime from rate-limit messages.
_RESET_ISO_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?)',
)
_RESET_12H_RE = re.compile(
    r'(\d{1,2}:\d{2}(?::\d{2})?)\s*([AP]M)\s*([A-Z]{2,4})?'
    r'(?:\s+on\s+\w+,?\s+(\w+)\s+(\d{1,2}),?\s+(\d{4}))?',
    re.IGNORECASE,
)
_RESET_RELATIVE_RE = re.compile(
    r'(?:reset|retry|wait)\s+(?:in|after)\s+(\d+)\s*(second|minute|hour)s?',
    re.IGNORECASE,
)
# "7:20am (America/Chicago)" or "12pm (America/Chicago)" -- time-only with IANA zone
_RESET_TIME_IANA_RE = re.compile(
    r'(\d{1,2}(?::\d{2})?)\s*([ap]m)\s*\(([A-Za-z_/]+)\)',
    re.IGNORECASE,
)
# "May 26, 12pm (America/Chicago)" -- month + day + time with IANA zone
_RESET_DATE_IANA_RE = re.compile(
    r'(\w+)\s+(\d{1,2}),?\s*(\d{1,2}(?::\d{2})?)\s*([ap]m)\s*\(([A-Za-z_/]+)\)',
    re.IGNORECASE,
)

# Progress interval for long rate-limit sleeps (seconds).
_SLEEP_REPORT_INTERVAL = 7200

# Maximum number of rate-limit retries before giving up.
_MAX_RATE_LIMIT_RETRIES = 3

_TZ_OFFSETS = {
    # UTC / GMT
    'UTC': 0,   'GMT': 0,   'UT': 0,
    # North America -- Standard / Daylight (ambiguous: CST also China +8; use IANA Asia/Shanghai)
    'NST': -3.5,  'NDT': -2.5,   # Newfoundland
    'AST': -4,    'ADT': -3,     # Atlantic
    'EST': -5,    'EDT': -4,     # Eastern
    'CST': -6,    'CDT': -5,     # Central
    'MST': -7,    'MDT': -6,     # Mountain
    'PST': -8,    'PDT': -7,     # Pacific
    'AKST': -9,   'AKDT': -8,    # Alaska
    'HST': -10,   'HDT': -9,     # Hawaii
    'HAST': -10,  'HADT': -9,    # Hawaii-Aleutian
    'SST': -11,                   # Samoa
    # Europe
    'WET': 0,                     # Western European
    'WEST': 1,   'BST': 1,       # W. European Summer / British Summer
    'CET': 1,    'CEST': 2,      # Central European / Summer
    'EET': 2,    'EEST': 3,      # Eastern European / Summer
    # Middle East / Russia / Africa
    'MSK': 3,                     # Moscow
    'TRT': 3,                     # Turkey
    'GST': 4,                     # Gulf Standard (UAE, Oman)
    'AFT': 4.5,                   # Afghanistan
    'PKT': 5,                     # Pakistan
    'NPT': 5.75,                  # Nepal
    'MMT': 6.5,                   # Myanmar
    # Asia / Pacific (ambiguous: IST = Ireland +1 OR India +5.5 OR Israel +2; use IANA)
    'ICT': 7,                     # Indochina
    'WIB': 7,                     # Western Indonesia
    'HKT': 8,                     # Hong Kong
    'SGT': 8,                     # Singapore
    'MYT': 8,                     # Malaysia
    'PHT': 8,                     # Philippines
    'AWST': 8,                    # Australia Western
    'JST': 9,                     # Japan
    'KST': 9,                     # Korea
    'WIT': 9,                     # Eastern Indonesia
    'ACST': 9.5,  'ACDT': 10.5,  # Australia Central Std / Daylight
    'AEST': 10,   'AEDT': 11,    # Australia Eastern Std / Daylight
    'NZST': 12,   'NZDT': 13,    # New Zealand Std / Daylight
}
_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


class RateLimitError(RuntimeError):
    """
    Raised when the claude CLI output indicates a usage-rate limit has been hit.
    """
    pass


class SpendLimitError(RateLimitError):
    """
    Raised when the monthly account spend limit is reached.
    Unlike RateLimitError, this is not retryable.
    """
    pass


class FatalInvocationError(RuntimeError):
    """
    Raised when a claude invocation ran longer than _FATAL_TIMEOUT_SECS,
    exited non-zero, and produced no output on two consecutive occurrences.
    """
    pass


_FATAL_TIMEOUT_SECS = 29 * 60

DEFAULT_ALLOWED_TOOLS = ['Bash', 'Edit', 'Read', 'Write']

# Cache for license file content; populated on first build_common_context() call.
_license_header_cache: dict = {}
_license_header_cache_lock = threading.Lock()

# Cached open file handle for run_events.txt; one handle per run, reset in main().
_run_events_handle = None
_run_events_lock   = threading.Lock()


# ---------------------------------------------------------------------------
# Run event logging
# ---------------------------------------------------------------------------

def _log_run_event(log_dir, message):
    """Append a timestamped event line to run_events.txt in the log directory."""
    global _run_events_handle
    if log_dir is None:
        return
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    try:
        with _run_events_lock:
            if _run_events_handle is None:
                _run_events_handle = open(log_dir / 'run_events.txt', 'a', encoding='utf-8')
            _run_events_handle.write(f'{ts}  {message}\n')
            _run_events_handle.flush()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Rate-limit time parsing
# ---------------------------------------------------------------------------

def _roll_time_if_past(reset_local, now_local):
    """Roll reset_local forward one day if it passed more than 1 hour ago."""
    if reset_local < now_local - timedelta(hours=1):
        return reset_local + timedelta(days=1)
    return reset_local


def parse_reset_time(message):
    """
    Extract the rate-limit reset datetime from a Claude CLI error message.
    Returns a UTC-aware datetime object, or None if no parseable time is found.

    Supported formats (in precedence order):
    1. ISO 8601: "2026-05-22T14:00:00Z" or "2026-05-22T14:00:00-05:00"
    2. Month + day + 12H + IANA zone: "May 26, 12pm (America/Chicago)"
    3. Time-only + IANA zone: "7:20am (America/Chicago)"
    4. 12H with TZ abbreviation and optional date: "9:00 PM CDT on Thursday, May 22, 2026"
    5. Relative offset: "retry after 60 seconds" / "reset in 5 minutes"
    """
    # ISO 8601
    m = _RESET_ISO_RE.search(message)
    if m:
        s = m.group(1)
        base = s[:19].replace('T', ' ')
        try:
            dt = datetime.strptime(base, '%Y-%m-%d %H:%M:%S')
            now_utc = datetime.now(timezone.utc)
            if s.endswith('Z'):
                reset_utc = dt.replace(tzinfo=timezone.utc)
            else:
                off_m = re.search(r'([+-])(\d{2}):?(\d{2})$', s)
                if off_m:
                    sign = 1 if off_m.group(1) == '+' else -1
                    h, mn = int(off_m.group(2)), int(off_m.group(3))
                    tz = timezone(timedelta(hours=sign * h, minutes=sign * mn))
                    reset_utc = dt.replace(tzinfo=tz).astimezone(timezone.utc)
                else:
                    reset_utc = dt.replace(tzinfo=timezone.utc)
            return max(reset_utc, now_utc + timedelta(minutes=1))
        except ValueError:
            pass

    # IANA-zone patterns checked before bare 12H to avoid misreading
    # "4:40am (America/Chicago)" as 4:40 AM UTC.

    # "May 26, 12pm (America/Chicago)"
    if _HAVE_ZONEINFO:
        m = _RESET_DATE_IANA_RE.search(message)
        if m:
            month_str = m.group(1)
            day_str   = m.group(2)
            time_str  = m.group(3)
            ampm      = m.group(4).upper()
            iana_zone = m.group(5)
            month = _MONTH_NAMES.get(month_str.lower())
            if month:
                try:
                    tz = ZoneInfo(iana_zone)
                    parts = time_str.split(':')
                    h  = int(parts[0])
                    mn = int(parts[1]) if len(parts) > 1 else 0
                    if ampm == 'PM' and h != 12:
                        h += 12
                    elif ampm == 'AM' and h == 12:
                        h = 0
                    now_utc  = datetime.now(timezone.utc)
                    year     = now_utc.astimezone(tz).year
                    reset_dt = datetime(year, month, int(day_str), h, mn, 0, tzinfo=tz)
                    if reset_dt.astimezone(timezone.utc) <= now_utc:
                        reset_dt = datetime(year + 1, month, int(day_str), h, mn, 0, tzinfo=tz)
                    return max(reset_dt.astimezone(timezone.utc), now_utc + timedelta(minutes=1))
                except (_ZINotFoundError, ValueError):
                    pass

    # "7:20am (America/Chicago)"
    if _HAVE_ZONEINFO:
        m = _RESET_TIME_IANA_RE.search(message)
        if m:
            time_str  = m.group(1)
            ampm      = m.group(2).upper()
            iana_zone = m.group(3)
            try:
                tz = ZoneInfo(iana_zone)
                parts = time_str.split(':')
                h  = int(parts[0])
                mn = int(parts[1]) if len(parts) > 1 else 0
                if ampm == 'PM' and h != 12:
                    h += 12
                elif ampm == 'AM' and h == 12:
                    h = 0
                now_local   = datetime.now(tz)
                reset_local = now_local.replace(hour=h, minute=mn, second=0, microsecond=0)
                reset_local = _roll_time_if_past(reset_local, now_local)
                now_utc = now_local.astimezone(timezone.utc)
                return max(reset_local.astimezone(timezone.utc), now_utc + timedelta(minutes=1))
            except _ZINotFoundError:
                pass

    # "9:00 PM CDT on Thursday, May 22, 2026"
    m = _RESET_12H_RE.search(message)
    if m:
        time_str   = m.group(1)
        ampm       = m.group(2).upper()
        tz_abbr    = (m.group(3) or 'UTC').upper()
        month_str  = m.group(4)
        day_str    = m.group(5)
        year_str   = m.group(6)
        if month_str and day_str and year_str:
            month = _MONTH_NAMES.get(month_str.lower())
            if month:
                try:
                    parts = time_str.split(':')
                    h  = int(parts[0])
                    mn = int(parts[1]) if len(parts) > 1 else 0
                    sc = int(parts[2]) if len(parts) > 2 else 0
                    if ampm == 'PM' and h != 12:
                        h += 12
                    elif ampm == 'AM' and h == 12:
                        h = 0
                    dt = datetime(int(year_str), month, int(day_str), h, mn, sc)
                    offset_h = _TZ_OFFSETS.get(tz_abbr, 0)
                    tz = timezone(timedelta(hours=offset_h))
                    return dt.replace(tzinfo=tz).astimezone(timezone.utc)
                except (ValueError, KeyError):
                    pass
        else:
            try:
                parts = time_str.split(':')
                h  = int(parts[0])
                mn = int(parts[1]) if len(parts) > 1 else 0
                sc = int(parts[2]) if len(parts) > 2 else 0
                if ampm == 'PM' and h != 12:
                    h += 12
                elif ampm == 'AM' and h == 12:
                    h = 0
                offset_h = _TZ_OFFSETS.get(tz_abbr, 0)
                tz = timezone(timedelta(hours=offset_h))
                now_local = datetime.now(tz)
                reset_local = now_local.replace(hour=h, minute=mn, second=sc, microsecond=0)
                reset_local = _roll_time_if_past(reset_local, now_local)
                now_utc = now_local.astimezone(timezone.utc)
                return max(reset_local.astimezone(timezone.utc), now_utc + timedelta(minutes=1))
            except (ValueError, KeyError):
                pass

    # "retry after 60 seconds" / "reset in 5 minutes"
    m = _RESET_RELATIVE_RE.search(message)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        if unit.startswith('second'):
            delta = timedelta(seconds=amount)
        elif unit.startswith('minute'):
            delta = timedelta(minutes=amount)
        else:
            delta = timedelta(hours=amount)
        return datetime.now(timezone.utc) + delta

    return None


# ---------------------------------------------------------------------------
# Execution time windows
# ---------------------------------------------------------------------------

_EXEC_WINDOW_RE = re.compile(
    r'^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*(?:([A-Za-z_]+(?:/[A-Za-z_]+)+)|([A-Za-z]{2,4}))?\s*$'
)

_DAY_NAMES = {
    'mo': 0, 'mon': 0,
    'tu': 1, 'tue': 1,
    'we': 2, 'wed': 2,
    'th': 3, 'thu': 3,
    'fr': 4, 'fri': 4,
    'sa': 5, 'sat': 5,
    'su': 6, 'sun': 6,
}

_DAY_PREFIX_RE = re.compile(r'^([A-Za-z]{2,3}(?:,[A-Za-z]{2,3})*)\s+')


def parse_exec_windows(spec):
    """
    Parse an exec-window spec string into a list of window dicts:
      { start: (h, m), end: (h, m), tz: tzinfo, days: frozenset|None }
    Multiple windows are separated by ';'. Each window:
      [DAY[,DAY...] ]HH:MM-HH:MM [IANA/Zone | TZABBR]
    Returns [] for an empty/None spec (no restriction).
    """
    windows = []
    if not spec:
        return windows
    for part in str(spec).split(';'):
        part = part.strip()
        if not part:
            continue
        days = None
        dm = _DAY_PREFIX_RE.match(part)
        if dm:
            day_set = set()
            bad = False
            for d in dm.group(1).split(','):
                n = _DAY_NAMES.get(d.lower())
                if n is None:
                    print(f'WARNING: unrecognized day abbreviation "{d}" in exec-window "{part}" -- ignoring window', file=sys.stderr)
                    bad = True
                    break
                day_set.add(n)
            if bad:
                continue
            days = frozenset(day_set)
            part = part[dm.end():]
        m = _EXEC_WINDOW_RE.match(part)
        if not m:
            print(f'WARNING: cannot parse exec-window "{part}" -- ignoring', file=sys.stderr)
            continue
        sh, sm = (int(x) for x in m.group(1).split(':'))
        eh, em = (int(x) for x in m.group(2).split(':'))
        tz_name = m.group(3) or m.group(4)
        tz = None
        if m.group(3) and _HAVE_ZONEINFO:
            try:
                tz = ZoneInfo(tz_name)
            except _ZINotFoundError:
                tz = None
        if tz is None:
            offset_h = _TZ_OFFSETS.get((tz_name or 'UTC').upper(), 0)
            tz = timezone(timedelta(hours=offset_h))
        windows.append({'start': (sh, sm), 'end': (eh, em), 'tz': tz, 'days': days})
    return windows


def _in_exec_window(now_utc, window):
    """Return True if now_utc falls within the given window dict."""
    local = now_utc.astimezone(window['tz'])
    days = window.get('days')
    start_h, start_m = window['start']
    end_h, end_m = window['end']
    start_minutes = start_h * 60 + start_m
    end_minutes   = end_h * 60 + end_m
    cur_minutes   = local.hour * 60 + local.minute
    cur_weekday   = local.weekday()

    if start_minutes == end_minutes:
        return days is None or cur_weekday in days

    if start_minutes < end_minutes:
        if days is not None and cur_weekday not in days:
            return False
        return start_minutes <= cur_minutes < end_minutes

    if cur_minutes >= start_minutes:
        return days is None or cur_weekday in days
    if cur_minutes < end_minutes:
        prev_weekday = (cur_weekday - 1) % 7
        return days is None or prev_weekday in days
    return False


def _next_exec_window_start(now_utc, window):
    """Return the UTC datetime of the next time this window opens."""
    local = now_utc.astimezone(window['tz'])
    start_h, start_m = window['start']
    candidate = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    days = window.get('days')
    if days is not None:
        for _ in range(7):
            if candidate.weekday() in days:
                break
            candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _chunked_sleep(sleep_secs, wake_dt, label, _out):
    """
    Sleep for sleep_secs seconds in chunks, printing a progress line every
    _SLEEP_REPORT_INTERVAL seconds. Returns actual seconds elapsed (monotonic).
    """
    t_start = time.monotonic()
    remaining = sleep_secs
    while remaining > 0:
        chunk = min(remaining, _SLEEP_REPORT_INTERVAL)
        try:
            time.sleep(chunk)
        except KeyboardInterrupt:
            _interrupt_event.set()
            raise
        remaining -= chunk
        if remaining > 0:
            still_left = max(0.0, (wake_dt - datetime.now(timezone.utc)).total_seconds())
            _out(
                f'  {label} -- {still_left:.0f}s remaining '
                f'(wake {wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")})'
            )
    return time.monotonic() - t_start


def wait_for_exec_window(windows, _out, log_dir=None):
    """
    Block until the current time falls within one of the given windows.
    No-op (returns 0.0) if windows is empty. Logs wait events to run_events.txt.
    Returns total seconds waited (excluding time already in-window).
    """
    if not windows:
        return 0.0
    now = datetime.now(timezone.utc)
    if any(_in_exec_window(now, w) for w in windows):
        return 0.0
    total_waited = 0.0
    while True:
        now = datetime.now(timezone.utc)
        wake_dt = min(_next_exec_window_start(now, w) for w in windows)
        sleep_secs = max(0.0, (wake_dt - now).total_seconds())
        msg = (
            f'Exec window closed -- next window opens '
            f'{wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")}; sleeping {sleep_secs:.0f}s'
        )
        _out(f'  {msg}')
        _log_run_event(log_dir, msg)
        actual = _chunked_sleep(sleep_secs, wake_dt, 'Exec window sleep', _out)
        total_waited += actual
        now = datetime.now(timezone.utc)
        if any(_in_exec_window(now, w) for w in windows):
            break
    resume_msg = f'Exec window open -- proceeded (waited {total_waited:.0f}s total).'
    _out(f'  {resume_msg}')
    _log_run_event(log_dir, resume_msg)
    return total_waited


# ---------------------------------------------------------------------------
# Config loading and merging
# ---------------------------------------------------------------------------

_BUILD_CONFIG_RE = re.compile(
    r'^## Build Config\s*\n+```yaml\n(.*?)```',
    re.DOTALL | re.MULTILINE,
)


def _require_yaml():
    """Exit with an error message if PyYAML is not installed."""
    if not _HAVE_YAML:
        print(
            'ERROR: PyYAML is required to parse YAML config. '
            'Install with: pip install pyyaml',
            file=sys.stderr,
        )
        sys.exit(1)


def load_plan_config(plan_text):
    """
    Extract the Build Config YAML block from the plan document, if present.
    Returns a dict of config keys, or {} if no block is found.
    """
    m = _BUILD_CONFIG_RE.search(plan_text)
    if not m:
        return {}
    _require_yaml()
    return yaml.safe_load(m.group(1)) or {}


def load_config_file(path):
    """Read a YAML config file from path and return its contents as a dict."""
    _require_yaml()
    try:
        with open(path, encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except OSError as e:
        print(f'ERROR: cannot read config: {e}', file=sys.stderr)
        sys.exit(1)


def load_models_file(path):
    """
    Read a models list file (one model ID per line).
    Blank lines and lines starting with '#' are ignored.
    Returns a dict mapping each model ID to itself.
    """
    result = {}
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as e:
        print(f'WARNING: cannot read models_file {path}: {e}', file=sys.stderr)
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        result[line] = line
    return result


def merge_configs(plan_cfg, file_cfg):
    """
    Merge two config dicts, with file_cfg values taking precedence.
    The models dict is deep-merged; all other keys are replaced in full.
    """
    merged = dict(plan_cfg)
    merged.update(file_cfg)
    plan_models = plan_cfg.get('models', {})
    file_models = file_cfg.get('models', {})
    if plan_models or file_models:
        merged_models = dict(plan_models)
        merged_models.update(file_models)
        merged['models'] = merged_models
    return merged


def resolve_model(alias, config):
    """
    Resolve a model alias ('haiku', 'sonnet', 'opus') or full model ID.
    Lookup order: config models dict -> DEFAULT_MODEL_IDS -> pass through.
    """
    aliases = config.get('models') or {}
    return aliases.get(alias) or DEFAULT_MODEL_IDS.get(alias) or alias


def resolve_task_effort(task, cli_effort, config):
    """
    Return the effective effort string for a task.
    Precedence: CLI --effort > task Effort: field > config effort key.
    """
    if cli_effort:
        return cli_effort
    task_effort = (task.get('effort') or '').strip().lower()
    if task_effort:
        return task_effort
    return str(config.get('effort') or '').strip().lower()


def resolve_task_exec_window(task, config):
    """
    Return the effective exec-window spec string for a task.
    Precedence: task ExecWindow: field > config exec_window key.
    """
    task_window = (task.get('exec_window') or '').strip()
    if task_window:
        return task_window
    return str(config.get('exec_window') or '').strip()


def validate_config_types(config):
    """
    Hard error (exit 1) when a config key holds an incompatible type.
    Catches YAML authoring mistakes before they reach a crash or misbehave silently.
    """
    errors = []
    models = config.get('models')
    if models is not None and not isinstance(models, dict):
        errors.append(f'models: must be a mapping (dict), got {type(models).__name__}')
    for key in ('add_dirs', 'allowed_tools'):
        val = config.get(key)
        if val is not None and not isinstance(val, list):
            errors.append(f'{key}: must be a list, got {type(val).__name__}')
    if errors:
        print('ERROR: invalid config value type(s):', file=sys.stderr)
        for e in errors:
            print(f'  {e}', file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

_TASK_HEADING_RE = re.compile(r'^### Task (\d+) -- (.+)$', re.MULTILINE)
_FIELD_RE        = re.compile(r'^(Model|Files|Effort|ExecWindow):\s*(.+)$')
_VERIFY_HEAD_RE  = re.compile(r'^## Verification[ \t]*$', re.MULTILINE)
_SECTION_END_RE  = re.compile(r'^(?:---|## )', re.MULTILINE)


def _parse_fields_and_body(section_text):
    """
    Given the text after a task heading line, return a tuple of
    (model, files, effort, exec_window, prompt_body).

    Per-task fields appear immediately after the heading with no blank lines
    between them. A blank line ends the fields block; everything after is the
    prompt body. Comment lines (starting with #) within the fields block are
    skipped without ending it.
    """
    lines = section_text.lstrip('\n').split('\n')
    model = None
    files = []
    effort = None
    exec_window = None
    field_end = 0

    for i, line in enumerate(lines):
        fm = _FIELD_RE.match(line)
        stripped = line.strip()
        if fm:
            key, val = fm.group(1), fm.group(2).strip()
            if key == 'Model':
                model = val.lower()
            elif key == 'Files':
                files = [f.strip() for f in val.split(',') if f.strip()]
            elif key == 'Effort':
                effort = val.lower()
            elif key == 'ExecWindow':
                exec_window = val
            field_end = i + 1
        elif stripped.startswith('#'):
            continue
        elif stripped == '':
            field_end = i + 1
            break
        else:
            if re.match(r'^\w+:\s*\S', line):
                print(
                    f'WARNING: unrecognized field in plan -- treated as prompt body: {line!r}',
                    file=sys.stderr,
                )
            field_end = i
            break

    prompt_lines = lines[field_end:]
    while prompt_lines and prompt_lines[0].strip() == '':
        prompt_lines.pop(0)
    while prompt_lines and prompt_lines[-1].strip() == '':
        prompt_lines.pop()

    return model, files, effort, exec_window, '\n'.join(prompt_lines)


def _section_end(text, start):
    """Return the character index where the current section ends (--- or ## heading)."""
    tail = text[start:]
    m = _SECTION_END_RE.search(tail)
    return start + m.start() if m else len(text)


def parse_tasks(plan_text):
    """
    Return a list of task dicts sorted by task number. Each dict contains:
      { num, title, model, files, effort, exec_window, prompt_body }
    Only headings matching "### Task N -- Title" (N a digit) are recognized.
    """
    headings = list(_TASK_HEADING_RE.finditer(plan_text))
    tasks = []

    for i, m in enumerate(headings):
        num   = int(m.group(1))
        title = m.group(2).strip()
        body_start = m.end()

        if i + 1 < len(headings):
            body_end = headings[i + 1].start()
        else:
            body_end = _section_end(plan_text, body_start)

        section = plan_text[body_start:body_end]
        model, files, effort, exec_window, prompt_body = _parse_fields_and_body(section)

        tasks.append({
            'num':         num,
            'title':       title,
            'model':       model,
            'files':       files,
            'effort':      effort,
            'exec_window': exec_window,
            'prompt_body': prompt_body,
        })

    tasks.sort(key=lambda t: t['num'])
    return tasks


def validate_tasks(tasks):
    """
    Validate parsed tasks for duplicate task numbers and non-sequential numbering.
    Duplicate numbers cause a hard exit; non-sequential numbering is a warning only.
    """
    if not tasks:
        return

    by_num = {}
    for t in tasks:
        by_num.setdefault(t['num'], []).append(t['title'])

    dupes = {num: titles for num, titles in by_num.items() if len(titles) > 1}
    if dupes:
        print('ERROR: duplicate Task N numbers in plan:', file=sys.stderr)
        for num, titles in sorted(dupes.items()):
            print(f'  Task {num}: {", ".join(titles)}', file=sys.stderr)
        sys.exit(1)

    nums = sorted(by_num.keys())
    expected = list(range(1, len(nums) + 1))
    if nums != expected:
        print(f'WARNING: task numbers are not a gap-free sequence starting at 1: {nums}', file=sys.stderr)


def warn_parallel_file_collisions(selected):
    """
    Warn (non-fatal) when multiple concurrently-selected tasks declare
    overlapping Files: entries.
    """
    by_file = {}
    for t in selected:
        for f in t['files']:
            by_file.setdefault(f, []).append(t['num'])

    collisions = {f: nums for f, nums in by_file.items() if len(nums) > 1}
    if collisions:
        print('WARNING: multiple parallel tasks declare the same file:', file=sys.stderr)
        for f, nums in sorted(collisions.items()):
            print(f'  {f}: tasks {", ".join(str(n) for n in nums)}', file=sys.stderr)


def parse_verification(plan_text):
    """
    Return a dict { model, effort, exec_window, prompt_body } for the
    ## Verification section of the plan, or None if no such section exists.
    """
    m = _VERIFY_HEAD_RE.search(plan_text)
    if not m:
        return None

    section_start = m.end()
    section_end   = _section_end(plan_text, section_start)
    section       = plan_text[section_start:section_end]

    model, _, effort, exec_window, prompt_body = _parse_fields_and_body(section)
    return {
        'model': model or 'sonnet', 'effort': effort,
        'exec_window': exec_window, 'prompt_body': prompt_body,
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_common_context(config):
    """
    Build the shared context string (working dir, preamble, license header)
    that is identical for every task in a run. Written once to common_context.txt
    in the log directory; not repeated in per-task prompt log files.
    """
    parts = []

    repo = str(config.get('repo') or '').strip()
    if repo:
        parts.append(f'Working directory: {repo}')

    preamble = str(config.get('preamble') or '').strip()
    if preamble:
        parts.append(preamble)

    license_file = str(config.get('license_file') or '').strip()
    if license_file and license_file.lower() not in ('null', 'none'):
        if license_file not in _license_header_cache:
            with _license_header_cache_lock:
                if license_file not in _license_header_cache:
                    try:
                        _license_header_cache[license_file] = (
                            Path(license_file).read_text(encoding='utf-8').rstrip()
                        )
                    except OSError as e:
                        print(f'WARNING: cannot read license_file {license_file}: {e}', file=sys.stderr)
                        _license_header_cache[license_file] = None
        header = _license_header_cache[license_file]
        if header is not None:
            parts.append(
                'Use this exact license header for all new Python files:\n\n' + header
            )

    return '\n'.join(parts) if parts else ''


def build_prompt(task_dict, config, common_context=None):
    """
    Construct the full prompt string to send to Claude for a given task.
    If common_context is provided (pre-built by build_common_context()), it is
    used directly to avoid re-reading the license file. Otherwise it is built
    from config. The task body follows with a blank-line separator.
    """
    if common_context is None:
        common_context = build_common_context(config)

    parts = []
    if common_context:
        parts.append(common_context)
        parts.append('')  # blank line before prompt body
    parts.append(task_dict['prompt_body'])
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def make_run_log_dir(config, plan_path, run_ts):
    """
    Create and return the Path of the per-run log directory.
    Location: config log_dir (or sibling tmp_build_logs/ of the plan file's
    parent) / <plan_stem>_<run_ts>.
    """
    base = str(config.get('log_dir') or '').strip()
    if not base:
        base = str(Path(plan_path).resolve().parent.parent / 'tmp_build_logs')
    plan_stem = Path(plan_path).stem
    log_dir = Path(base) / f'{plan_stem}_{run_ts}'
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def write_log(log_dir, filename, content):
    """Write content to log_dir/filename (UTF-8). Returns the Path written."""
    p = log_dir / filename
    p.write_text(content, encoding='utf-8')
    return p


def write_completion_marker(log_dir, task_num, task_title, exit_code):
    """
    Write a small marker file indicating a task finished.
    Filename ends with _completed.txt (exit 0), _interrupted.txt (RC_INTERRUPTED),
    or _failed.txt (any other non-zero exit).
    """
    ts       = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    date_str = ts[:10]
    if exit_code == 0:
        status = 'completed'
    elif exit_code == RC_INTERRUPTED:
        status = 'interrupted'
    else:
        status = 'failed'
    filename = f'task_{task_num}_{date_str}_{status}.txt'
    content = (
        f'task: {task_num}\n'
        f'title: {task_title}\n'
        f'timestamp: {ts}\n'
        f'exit_code: {exit_code}\n'
    )
    return write_log(log_dir, filename, content)


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def run_claude(prompt, model, dry_run, log_dir, label, add_dirs=None, allowed_tools=None, extra_claude_args=None, _lines=None, exec_windows=None, task_body=None):
    """
    Pipe prompt to the claude CLI on stdin and capture stdout + stderr to
    timestamped log files, echoing output to the terminal (or buffering it
    in _lines for parallel execution). Returns (exit_code, usage_dict).

    usage_dict keys: input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens. All zero on failure or dry-run.

    task_body: if provided, this string is written to the per-task prompt log
               file instead of the full prompt. The full prompt (common context
               + task body) is still sent to Claude. Pass task['prompt_body']
               to avoid repeating the common context across per-task log files.

    add_dirs: list of directory paths to pass via --add-dir.
    allowed_tools: list of tool names; defaults to DEFAULT_ALLOWED_TOOLS.
    extra_claude_args: additional CLI flags passed through verbatim.
    _lines: if a list, append output lines to it instead of printing.
    exec_windows: parsed result of parse_exec_windows(). Checked before every
                  attempt, including after a rate-limit wake-up. Wait time is
                  NOT counted in the reported elapsed time.

    Ctrl-C (KeyboardInterrupt) is caught during proc.communicate() and during
    sleep. The subprocess is terminated, an interrupted log file is written,
    and KeyboardInterrupt is re-raised for the caller to handle.

    Retry behavior: on a rate-limit response with a parseable reset time, sleeps
    until (reset_time + 1 min) and retries, up to _MAX_RATE_LIMIT_RETRIES times.
    Sleep time is NOT counted in the reported elapsed time.

    Raises RateLimitError, SpendLimitError, FatalInvocationError, or
    KeyboardInterrupt.
    """
    def _out(s=''):
        if _lines is not None:
            _lines.append(s)
        else:
            print(s)

    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    log_content = task_body if task_body is not None else prompt
    prompt_log = write_log(log_dir, f'{label}_{ts}_prompt.txt', log_content)
    _out(f'  prompt  -> {prompt_log}')

    _zero_usage = {
        'input_tokens': 0, 'output_tokens': 0,
        'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
    }

    if dry_run:
        _out('-- DRY RUN: task body follows --')
        _out(log_content)
        _out('-- END task body --')
        return 0, _zero_usage

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    claude_cmd = ['claude', '--model', model, '-p', '--output-format', 'json',
                  '--allowedTools'] + tools
    for d in (add_dirs or []):
        claude_cmd += ['--add-dir', d]
    claude_cmd += (extra_claude_args or [])

    _long_no_output_strikes = 0
    _prev_was_rate_limit = False
    attempt = 0
    total_run_secs = 0.0  # subprocess CPU time only; excludes all sleep/wait

    while True:
        # Check for a pending interrupt before starting each attempt.
        if _interrupt_event.is_set():
            raise KeyboardInterrupt('Interrupted by user')

        attempt += 1
        try:
            wait_for_exec_window(exec_windows, _out, log_dir)
        except KeyboardInterrupt:
            _interrupt_event.set()
            _log_run_event(
                log_dir,
                f'{label}: INTERRUPTED during exec-window wait (attempt {attempt})',
            )
            raise
        run_ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        t0 = time.monotonic()

        proc = subprocess.Popen(
            claude_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
        )
        # KNOWN ISSUE: proc.communicate() has no timeout. A subprocess that
        # never exits blocks this thread indefinitely. _FATAL_TIMEOUT_SECS
        # only fires AFTER communicate() returns.
        try:
            raw, err = proc.communicate(input=prompt)
        except KeyboardInterrupt:
            _interrupt_event.set()
            elapsed = time.monotonic() - t0
            total_run_secs += elapsed
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except Exception:
                pass
            interrupted_note = (
                f'[INTERRUPTED by Ctrl-C after {elapsed:.1f}s '
                f'(attempt {attempt}, total process time {total_run_secs:.1f}s)]'
            )
            _out(interrupted_note)
            _log_run_event(
                log_dir,
                f'{label}: INTERRUPTED after {elapsed:.1f}s process time '
                f'(attempt {attempt}); total process time {total_run_secs:.1f}s',
            )
            write_log(
                log_dir,
                f'{label}_{run_ts}_attempt{attempt}_interrupted.txt',
                interrupted_note,
            )
            raise

        elapsed = time.monotonic() - t0
        total_run_secs += elapsed

        # Parse JSON response; fall back to raw text on failure.
        usage = dict(_zero_usage)
        text_output = raw
        try:
            data = json.loads(raw)
            text_output = data.get('result', raw)
            usage.update(data.get('usage') or {})
        except (json.JSONDecodeError, AttributeError):
            pass

        attempt_suffix = f'_attempt{attempt}' if attempt > 1 else ''
        out_log = write_log(log_dir, f'{label}_{run_ts}{attempt_suffix}_output.txt', text_output)
        if err:
            write_log(log_dir, f'{label}_{run_ts}{attempt_suffix}_stderr.txt', err)
        tok_in  = usage.get('input_tokens', 0)
        tok_out = usage.get('output_tokens', 0)
        tok_cr  = usage.get('cache_read_input_tokens', 0)
        tok_cw  = usage.get('cache_creation_input_tokens', 0)
        _out(
            f'  output  -> {out_log}'
            f'  ({elapsed:.1f}s, {int(elapsed) // 60}m{elapsed % 60:.3f}s, exit {proc.returncode})'
            f'  tokens: in={tok_in} out={tok_out} cache_read={tok_cr} cache_write={tok_cw}'
        )
        _out(f'  Return code: claude[{proc.pid}]: {proc.returncode}')
        _out(text_output)

        # text_output has the decoded human-readable result; search it first
        # so natural-language time patterns resolve correctly.
        combined = text_output + '\n' + raw + '\n' + err

        # Check for monthly spend limit before the long-no-output fatal check.
        if _SPEND_LIMIT_RE.search(combined):
            _log_run_event(log_dir, f'{label}: spend limit reached (attempt {attempt})')
            raise SpendLimitError(
                (text_output.strip() or 'monthly spend limit reached')[:300]
            )

        # All three conditions must hold: process ran >29 min, exited non-zero,
        # and produced no output. One such occurrence is tolerated; a second is fatal.
        if elapsed > _FATAL_TIMEOUT_SECS and proc.returncode != 0 and not text_output.strip():
            _long_no_output_strikes += 1
            if _long_no_output_strikes >= 2 or _prev_was_rate_limit:
                reason = (
                    'previous attempt was a rate-limit hit'
                    if _prev_was_rate_limit
                    else 'second occurrence'
                )
                _log_run_event(
                    log_dir,
                    f'{label}: fatal invocation error ({reason}) -- '
                    f'total process time {total_run_secs:.1f}s',
                )
                raise FatalInvocationError(
                    f'invocation ran {elapsed:.0f}s (>{_FATAL_TIMEOUT_SECS}s), '
                    f'exited {proc.returncode}, produced no output '
                    f'({reason} -- aborting)'
                )
            _out(
                f'  WARNING: invocation ran {elapsed:.0f}s with no output '
                f'(strike 1 of 2 before fatal; exit {proc.returncode})'
            )

        # Rate-limit detection: require keyword + secondary signal.
        is_limit = bool(_RATE_LIMIT_RE.search(combined)) and (
            proc.returncode != 0
            or bool(_RESET_TIME_IANA_RE.search(combined))
            or bool(_RESET_DATE_IANA_RE.search(combined))
            or bool(_RESET_ISO_RE.search(combined))
            or bool(_RESET_RELATIVE_RE.search(combined))
        )
        if is_limit:
            reset_dt = parse_reset_time(combined)
            if reset_dt is not None and attempt <= _MAX_RATE_LIMIT_RETRIES:
                wake_dt    = reset_dt + timedelta(minutes=1)
                now_dt     = datetime.now(timezone.utc)
                sleep_secs = max(0.0, (wake_dt - now_dt).total_seconds())
                limit_msg = (
                    f'Rate limit -- resets '
                    f'{reset_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")}; '
                    f'sleeping {sleep_secs:.0f}s '
                    f'(wake {wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")})'
                )
                _out(f'  {limit_msg}')
                _log_run_event(log_dir, f'{label}: {limit_msg}')
                actual_slept = _chunked_sleep(sleep_secs, wake_dt, 'Rate limit sleep', _out)
                resume_msg = (
                    f'Rate limit sleep complete: slept {actual_slept:.0f}s; '
                    f'retrying (attempt {attempt + 1})'
                )
                _out(f'  {resume_msg}')
                _log_run_event(log_dir, f'{label}: {resume_msg}')
                _prev_was_rate_limit = True
                continue
            _log_run_event(
                log_dir,
                f'{label}: rate limit -- no parseable reset time or retries exhausted',
            )
            raise RateLimitError(text_output.strip()[:300])

        break

    _log_run_event(
        log_dir,
        f'{label}: completed (attempt {attempt}, exit {proc.returncode}, '
        f'process time {total_run_secs:.1f}s)',
    )
    return proc.returncode, usage


def _task_worker(task, model, prompt, dry_run, log_dir, label, add_dirs, allowed_tools=None, extra_claude_args=None, exec_windows=None, task_body=None):
    """
    Worker function for parallel task execution via ThreadPoolExecutor.
    Buffers all output into a list and returns it atomically.
    Returns (task_num, rc, usage, lines).
    """
    lines = [
        '',
        '=' * 70,
        f'  Task {task["num"]} -- {task["title"]}',
        f'  model: {model}',
    ]
    if extra_claude_args:
        lines.append(f'  claude args: {" ".join(extra_claude_args)}')
    if task['files']:
        lines.append(f'  files: {", ".join(task["files"])}')
    lines.append('=' * 70)

    try:
        rc, usage = run_claude(
            prompt, model, dry_run, log_dir, label, add_dirs, allowed_tools,
            extra_claude_args, _lines=lines, exec_windows=exec_windows,
            task_body=task_body,
        )
    except KeyboardInterrupt:
        marker = write_completion_marker(log_dir, task['num'], task['title'], RC_INTERRUPTED)
        lines.append(f'  marker  -> {marker}')
        print('\n'.join(lines))
        raise
    marker = write_completion_marker(log_dir, task['num'], task['title'], rc)
    lines.append(f'  marker  -> {marker}')
    return task['num'], rc, usage, lines


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_arg_parser():
    """Construct and return the ArgumentParser for autobuilderclaude."""
    p = argparse.ArgumentParser(
        prog='autobuilderclaude',
        description='autobuilderclaude v1.10.0 -- Document-driven Claude task runner (autobuilderclaude format v1).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Plan format:   autobuilderclaude_plan_template_v1.md\n'
            'Config format: autobuilderclaude_config_v1.yaml\n'
            'https://github.com/ke4ahr/autobuilderclaude\n\n'
            'Any unrecognized flags are passed through to the claude CLI unchanged.'
        ),
    )
    p.add_argument('--input',    metavar='PLAN',     required=True,
                   help='Implementation plan .md file')
    p.add_argument('--template', metavar='TEMPLATE',
                   help='YAML config file providing base defaults (overridden by plan Build Config)')
    p.add_argument('--config',   metavar='CONFIG',
                   help='YAML config file (overrides plan Build Config and --template)')
    p.add_argument('--task',       metavar='N',
                   help='Run only task N (integer) or "verify"')
    p.add_argument('--start-task', metavar='N', type=int,
                   help='Start at task N and run through all remaining tasks')
    p.add_argument('--stop-after', metavar='N', type=int,
                   help='Stop after task N completes (inclusive); verification is skipped')
    p.add_argument('--model',    metavar='MODEL',
                   help='Override per-task model (haiku|sonnet|opus, a full Claude model ID, or a provider/model:tag ID)')
    p.add_argument('--effort',   metavar='LEVEL',
                   choices=['low', 'medium', 'high', 'xhigh', 'max'],
                   help='Global effort level (low|medium|high|xhigh|max); overrides per-task Effort: field and config effort key')
    p.add_argument('--parallel', metavar='N', type=int, default=1,
                   help='Number of tasks to run concurrently (default: 1)')
    p.add_argument('--dry-run',  action='store_true',
                   help='Print prompts without calling claude')
    p.add_argument('--list',     action='store_true',
                   help='List tasks with resolved models, then exit')
    return p


# ---------------------------------------------------------------------------
# Error handling helpers
# ---------------------------------------------------------------------------

def _abort_run(exc, log_dir, event_msg, *, print_lock=None, skip_remaining=True):
    """
    Print the appropriate error message for exc, log the event, and exit 1.
    Handles FatalInvocationError, SpendLimitError, and RateLimitError.
    """
    if isinstance(exc, FatalInvocationError):
        msgs = [f'\nFATAL: {exc}']
    elif isinstance(exc, SpendLimitError):
        msgs = [f'\nERROR: monthly spend limit -- {exc}', 'Raise it at claude.ai/settings/usage']
    else:
        msgs = [f'\nERROR: rate limit reached -- {exc}']
    if skip_remaining:
        msgs.append('Remaining tasks skipped.')
    if print_lock is not None:
        with print_lock:
            for msg in msgs:
                print(msg, file=sys.stderr)
    else:
        for msg in msgs:
            print(msg, file=sys.stderr)
    _log_run_event(log_dir, event_msg)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """
    Entry point. Parse arguments, load and merge configs, select tasks, and
    execute them sequentially or in parallel via ThreadPoolExecutor.

    Execution order:
    1. Parse CLI args; collect unrecognized args as pass-through to claude.
    2. Load and merge configs: template < plan Build Config < --config file.
    3. Parse tasks and optional verification from the plan document.
    4. Apply --task / --start-task / --stop-after task selection filters.
    5. Build common context once; write to common_context.txt.
    6. Run selected tasks (sequential or parallel based on --parallel).
    7. Run verification if present and not suppressed by --stop-after.
    8. Print aggregate token usage and exit with the worst task exit code.

    Exits with RC_INTERRUPTED (130) on Ctrl-C.
    """
    global _run_events_handle
    _interrupt_event.clear()
    if _run_events_handle is not None:
        try:
            _run_events_handle.close()
        except OSError:
            pass
        _run_events_handle = None

    parser = build_arg_parser()
    args, extra_args = parser.parse_known_args()

    if args.parallel < 1:
        print('ERROR: --parallel must be >= 1', file=sys.stderr)
        sys.exit(1)

    plan_path = args.input
    try:
        plan_text = Path(plan_path).read_text(encoding='utf-8')
    except OSError as e:
        print(f'ERROR: cannot read plan: {e}', file=sys.stderr)
        sys.exit(1)

    # Build merged config: template < plan block < config file.
    config = {}
    if args.template:
        config = load_config_file(args.template)
    config = merge_configs(config, load_plan_config(plan_text))
    if args.config:
        config = merge_configs(config, load_config_file(args.config))

    validate_config_types(config)

    models_file_path = str(config.get('models_file', '') or '').strip()
    if models_file_path and models_file_path.lower() not in ('null', 'none', ''):
        file_models = load_models_file(models_file_path)
        explicit_models = config.get('models') or {}
        merged_models = dict(file_models)
        merged_models.update(explicit_models)
        config['models'] = merged_models

    repo_path = str(config.get('repo') or '').strip()
    if repo_path and not Path(repo_path).is_dir():
        print(f'ERROR: repo path does not exist or is not a directory: {repo_path}', file=sys.stderr)
        sys.exit(1)

    tasks        = parse_tasks(plan_text)
    validate_tasks(tasks)
    verification = parse_verification(plan_text)

    if not tasks and not verification:
        print('ERROR: no tasks or verification section found in plan.', file=sys.stderr)
        sys.exit(1)

    pass_through_args = extra_args

    # --list
    if args.list:
        print(f'Plan:  {plan_path}')
        if args.effort:
            print(f'Effort (global override): {args.effort}')
        if pass_through_args:
            print(f'Claude pass-through args: {" ".join(pass_through_args)}')
        default_model = config.get('default_model', 'sonnet')
        for t in tasks:
            model  = resolve_model(t['model'] or default_model, config)
            files  = ', '.join(t['files']) if t['files'] else '(none)'
            effort = resolve_task_effort(t, args.effort, config)
            window = resolve_task_exec_window(t, config)
            print(f'  Task {t["num"]:>3} -- {t["title"]}')
            print(f'           model: {model}')
            print(f'          effort: {effort or "(default)"}')
            print(f'           files: {files}')
            print(f'    exec window: {window or "(unrestricted)"}')
        if verification:
            model  = resolve_model(verification['model'], config)
            effort = resolve_task_effort(verification, args.effort, config)
            window = resolve_task_exec_window(verification, config)
            print(f'  Verify       -- model: {model}  effort: {effort or "(default)"}  exec window: {window or "(unrestricted)"}')
        sys.exit(0)

    # Determine which tasks to run.
    if args.task and args.start_task is not None:
        print('ERROR: --task and --start-task are mutually exclusive', file=sys.stderr)
        sys.exit(1)
    if args.task and args.stop_after is not None:
        print('ERROR: --task and --stop-after are mutually exclusive', file=sys.stderr)
        sys.exit(1)

    run_verify = False
    if args.task:
        if args.task.lower() == 'verify':
            run_verify = True
            selected = []
        else:
            try:
                n = int(args.task)
            except ValueError:
                print('ERROR: --task must be an integer or "verify"', file=sys.stderr)
                sys.exit(1)
            selected = [t for t in tasks if t['num'] == n]
            if not selected:
                nums = [str(t['num']) for t in tasks]
                print(
                    f'ERROR: task {n} not found. Available: {", ".join(nums)}',
                    file=sys.stderr,
                )
                sys.exit(1)
    elif args.start_task is not None:
        n = args.start_task
        selected = [t for t in tasks if t['num'] >= n]
        if not selected:
            nums = [str(t['num']) for t in tasks]
            print(
                f'ERROR: no tasks >= {n}. Available: {", ".join(nums)}',
                file=sys.stderr,
            )
            sys.exit(1)
        run_verify = verification is not None
    else:
        selected   = tasks
        run_verify = verification is not None

    if args.stop_after is not None:
        selected   = [t for t in selected if t['num'] <= args.stop_after]
        run_verify = False
        if not selected:
            nums = [str(t['num']) for t in tasks]
            print(
                f'ERROR: no tasks <= {args.stop_after}. Available: {", ".join(nums)}',
                file=sys.stderr,
            )
            sys.exit(1)

    run_ts   = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    log_dir  = make_run_log_dir(config, plan_path, run_ts)
    _extra_dirs = config.get('add_dirs') or []
    add_dirs = [d for d in ([str(config.get('repo') or '').strip()] + list(_extra_dirs)) if d]
    allowed_tools_list = config.get('allowed_tools') or DEFAULT_ALLOWED_TOOLS

    # Build common context once; write to shared file; print location once.
    common_context = build_common_context(config)
    if common_context:
        cc_path = write_log(log_dir, 'common_context.txt', common_context)
        print(f'Common context -> {cc_path}')

    print(f'Log dir: {log_dir}')
    if args.effort:
        print(f'Effort (global override): {args.effort}')
    if pass_through_args:
        print(f'Claude pass-through args: {" ".join(pass_through_args)}')
    if args.stop_after is not None:
        print(f'Stop after: task {args.stop_after}')
    if args.parallel > 1:
        print(f'Parallel: {args.parallel} workers')

    # In dry-run mode, print the common context once before any task output.
    if args.dry_run and common_context:
        print()
        print('-- DRY RUN: common context (shared by all tasks) --')
        print(common_context)
        print('-- END common context --')

    task_nums = [t['num'] for t in selected]
    _log_run_event(log_dir, f'Run started: plan={plan_path} tasks={task_nums}')

    default_model = config.get('default_model', 'sonnet')
    exit_code  = 0
    total_usage = {
        'input_tokens': 0, 'output_tokens': 0,
        'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
    }
    print_lock = threading.Lock()

    def _accumulate(rc, usage, task_num=None):
        nonlocal exit_code
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)
        if rc != 0:
            label = f'task {task_num}' if task_num is not None else 'verification'
            print(f'WARNING: {label} exited {rc}', file=sys.stderr)
            exit_code = rc

    try:
        if args.parallel > 1 and len(selected) > 1:
            warn_parallel_file_collisions(selected)

            work_items = []
            for task in selected:
                model_key  = args.model or task['model'] or default_model
                model      = resolve_model(model_key, config)
                safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', task['title'])[:40]
                label      = f'task_{task["num"]:03d}_{safe_title}'
                prompt     = build_prompt(task, config, common_context)
                effort     = resolve_task_effort(task, args.effort, config)
                task_claude_args = (['--effort', effort] if effort else []) + pass_through_args
                exec_windows = parse_exec_windows(resolve_task_exec_window(task, config))
                work_items.append((task, model, prompt, label, task_claude_args, exec_windows))

            executor = ThreadPoolExecutor(max_workers=args.parallel)
            try:
                futures = {
                    executor.submit(
                        _task_worker,
                        task, model, prompt, args.dry_run, log_dir, label,
                        add_dirs, allowed_tools_list, task_claude_args,
                        exec_windows, task['prompt_body'],
                    ): task['num']
                    for task, model, prompt, label, task_claude_args, exec_windows in work_items
                }
                for future in as_completed(futures):
                    try:
                        task_num, rc, usage, lines = future.result()
                    except KeyboardInterrupt:
                        _interrupt_event.set()
                        with print_lock:
                            print('\nInterrupted (Ctrl-C).', file=sys.stderr)
                        raise
                    except (FatalInvocationError, SpendLimitError, RateLimitError) as e:
                        if isinstance(e, FatalInvocationError):
                            event = 'Run aborted: fatal invocation error'
                        elif isinstance(e, SpendLimitError):
                            event = 'Run aborted: spend limit reached'
                        else:
                            event = 'Run aborted: rate limit exhausted'
                        _abort_run(e, log_dir, event, print_lock=print_lock)
                    with print_lock:
                        print('\n'.join(lines))
                    _accumulate(rc, usage, task_num)
            except KeyboardInterrupt:
                _log_run_event(log_dir, 'Run INTERRUPTED by user (Ctrl-C) during parallel execution')
                print('Canceling remaining tasks...', file=sys.stderr)
                raise
            finally:
                try:
                    executor.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    executor.shutdown(wait=False)

        else:
            for task in selected:
                model_key  = args.model or task['model'] or default_model
                model      = resolve_model(model_key, config)
                safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', task['title'])[:40]
                label      = f'task_{task["num"]:03d}_{safe_title}'
                effort     = resolve_task_effort(task, args.effort, config)
                task_claude_args = (['--effort', effort] if effort else []) + pass_through_args
                exec_windows = parse_exec_windows(resolve_task_exec_window(task, config))

                print()
                print('=' * 70)
                print(f'  Task {task["num"]} -- {task["title"]}')
                print(f'  model: {model}')
                if task_claude_args:
                    print(f'  claude args: {" ".join(task_claude_args)}')
                if task['files']:
                    print(f'  files: {", ".join(task["files"])}')
                print('=' * 70)

                prompt = build_prompt(task, config, common_context)
                _log_run_event(log_dir, f'Task {task["num"]} ({task["title"]}): starting')
                try:
                    rc, usage = run_claude(
                        prompt, model, args.dry_run, log_dir, label,
                        add_dirs, allowed_tools_list, task_claude_args,
                        exec_windows=exec_windows,
                        task_body=task['prompt_body'],
                    )
                except KeyboardInterrupt:
                    _log_run_event(log_dir, f'Task {task["num"]}: INTERRUPTED by user')
                    write_completion_marker(log_dir, task['num'], task['title'], RC_INTERRUPTED)
                    raise
                except (FatalInvocationError, SpendLimitError, RateLimitError) as e:
                    n = task['num']
                    if isinstance(e, FatalInvocationError):
                        event = f'Task {n}: fatal invocation error -- run aborted'
                    elif isinstance(e, SpendLimitError):
                        event = f'Task {n}: spend limit -- run aborted'
                    else:
                        event = f'Task {n}: rate limit exhausted -- run aborted'
                    _abort_run(e, log_dir, event)
                marker = write_completion_marker(log_dir, task['num'], task['title'], rc)
                print(f'  marker  -> {marker}')
                _accumulate(rc, usage, task['num'])

        if run_verify and verification:
            model_key    = args.model or verification['model'] or default_model
            model        = resolve_model(model_key, config)
            effort       = resolve_task_effort(verification, args.effort, config)
            verify_claude_args = (['--effort', effort] if effort else []) + pass_through_args
            exec_windows = parse_exec_windows(resolve_task_exec_window(verification, config))

            print()
            print('=' * 70)
            print(f'  Verification')
            print(f'  model: {model}')
            if verify_claude_args:
                print(f'  claude args: {" ".join(verify_claude_args)}')
            print('=' * 70)

            prompt = build_prompt(verification, config, common_context)
            _log_run_event(log_dir, 'Verification: starting')
            try:
                rc, usage = run_claude(
                    prompt, model, args.dry_run, log_dir, 'verify',
                    add_dirs, allowed_tools_list, verify_claude_args,
                    exec_windows=exec_windows,
                    task_body=verification['prompt_body'],
                )
            except KeyboardInterrupt:
                _log_run_event(log_dir, 'Verification: INTERRUPTED by user')
                raise
            except (FatalInvocationError, SpendLimitError, RateLimitError) as e:
                if isinstance(e, FatalInvocationError):
                    event = 'Verification: fatal invocation error -- run aborted'
                elif isinstance(e, SpendLimitError):
                    event = 'Verification: spend limit -- run aborted'
                else:
                    event = 'Verification: rate limit exhausted -- run aborted'
                _abort_run(e, log_dir, event, skip_remaining=False)
            _accumulate(rc, usage)

        _log_run_event(log_dir, f'Run completed: exit_code={exit_code}')

    except KeyboardInterrupt:
        print(f'\nInterrupted (Ctrl-C). Exit code {RC_INTERRUPTED}.', file=sys.stderr)
        _log_run_event(log_dir, f'Run terminated by user (Ctrl-C): exit_code={RC_INTERRUPTED}')
        sys.exit(RC_INTERRUPTED)

    print()
    print(
        f'Done.  total tokens: '
        f'in={total_usage["input_tokens"]} '
        f'out={total_usage["output_tokens"]} '
        f'cache_read={total_usage["cache_read_input_tokens"]} '
        f'cache_write={total_usage["cache_creation_input_tokens"]}'
    )
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
