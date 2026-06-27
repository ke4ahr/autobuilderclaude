#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# autobuilderclaude v1.6.3
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
# Output token count is printed as a dedicated line per task.
# On a 429/rate-limit response that contains a
# reset time, the script sleeps until (reset_time + 10 minutes) and retries
# the failing task automatically. Sleeps longer than 24 hours are supported;
# a progress line is printed every 2 hours during the wait.
#
# Tasks may also be restricted to specific time-of-day execution windows via
# a plan "ExecWindow:" field or a config "exec_window:" key
# (format: "HH:MM-HH:MM [TZ]", multiple windows separated by ";"). Outside
# the window, the script sleeps until the window opens before each attempt,
# including attempts after a rate-limit retry wake-up. Per-task ExecWindow:
# overrides the config default; omit both for unrestricted execution.
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

# ---------------------------------------------------------------------------
# Rate-limit detection and retry
# ---------------------------------------------------------------------------

_RATE_LIMIT_RE = re.compile(
    r'hit your (?:\w+\s+)?limit|usage limit|rate.?limit|exceeded.*limit|limit.*exceeded',
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

# Maximum number of rate-limit retries before giving up and raising
# RateLimitError.
_MAX_RATE_LIMIT_RETRIES = 3

_TZ_OFFSETS = {
    'UTC': 0, 'GMT': 0,
    'EST': -5, 'EDT': -4,
    'CST': -6, 'CDT': -5,
    'MST': -7, 'MDT': -6,
    'PST': -8, 'PDT': -7,
}
_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


class RateLimitError(RuntimeError):
    """Raised when the claude CLI output indicates a usage-rate limit."""
    pass


# Tools granted to claude when neither config nor --allowedTools sets a list.
# Single source of truth for run_claude()'s default and main()'s fallback.
DEFAULT_ALLOWED_TOOLS = ['Bash', 'Edit', 'Read', 'Write']


def parse_reset_time(message):
    """
    Extract the rate-limit reset datetime from a message string.
    Returns a UTC-aware datetime, or None if no parseable time is found.
    Handles ISO 8601, 12-hour clock with TZ abbreviation, and relative offsets.
    """
    # ISO 8601: 2026-05-22T14:00:00Z or 2026-05-22T14:00:00-05:00
    m = _RESET_ISO_RE.search(message)
    if m:
        s = m.group(1)
        base = s[:19].replace('T', ' ')
        try:
            dt = datetime.strptime(base, '%Y-%m-%d %H:%M:%S')
            if s.endswith('Z'):
                return dt.replace(tzinfo=timezone.utc)
            off_m = re.search(r'([+-])(\d{2}):?(\d{2})$', s)
            if off_m:
                sign = 1 if off_m.group(1) == '+' else -1
                h, mn = int(off_m.group(2)), int(off_m.group(3))
                tz = timezone(timedelta(hours=sign * h, minutes=sign * mn))
                return dt.replace(tzinfo=tz).astimezone(timezone.utc)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
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
            # "9:00 PM CDT" -- TZ abbreviation but no date. Assume today (or
            # tomorrow if that time has already passed) in the abbreviation's
            # fixed UTC offset, the same fallback logic as the IANA-zone,
            # date-less branch below.
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
                if reset_local <= now_local:
                    reset_local += timedelta(days=1)
                return reset_local.astimezone(timezone.utc)
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

    # "May 26, 12pm (America/Chicago)" -- month+day + time + IANA zone name
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
                    return reset_dt.astimezone(timezone.utc)
                except (_ZINotFoundError, ValueError):
                    pass

    # "7:20am (America/Chicago)" or "12pm (America/Chicago)" -- time-only with IANA zone
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
                if reset_local <= now_local:
                    reset_local += timedelta(days=1)
                return reset_local.astimezone(timezone.utc)
            except _ZINotFoundError:
                pass

    return None


# ---------------------------------------------------------------------------
# Execution time windows
# ---------------------------------------------------------------------------

_EXEC_WINDOW_RE = re.compile(
    r'^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*(?:([A-Za-z_]+/[A-Za-z_]+)|([A-Za-z]{2,4}))?\s*$'
)


def parse_exec_windows(spec):
    """
    Parse an exec-window spec string into a list of window dicts:
      { start: (h, m), end: (h, m), tz: tzinfo }
    Multiple windows are separated by ';'. Each window:
      HH:MM-HH:MM [IANA/Zone | TZABBR]
    A window where start > end wraps past midnight (e.g. 22:00-06:00).
    tz defaults to UTC if omitted. Returns [] for an empty/None spec,
    meaning no restriction -- always allowed.
    """
    windows = []
    if not spec:
        return windows
    for part in str(spec).split(';'):
        part = part.strip()
        if not part:
            continue
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
        windows.append({'start': (sh, sm), 'end': (eh, em), 'tz': tz})
    return windows


def _in_exec_window(now_utc, window):
    local = now_utc.astimezone(window['tz'])
    start_h, start_m = window['start']
    end_h, end_m = window['end']
    start_minutes = start_h * 60 + start_m
    end_minutes   = end_h * 60 + end_m
    cur_minutes   = local.hour * 60 + local.minute
    if start_minutes == end_minutes:
        return True  # full-day window
    if start_minutes < end_minutes:
        return start_minutes <= cur_minutes < end_minutes
    return cur_minutes >= start_minutes or cur_minutes < end_minutes  # wraps midnight


def _next_exec_window_start(now_utc, window):
    """UTC datetime of the next time this window opens, given now_utc is
    currently outside it."""
    local = now_utc.astimezone(window['tz'])
    start_h, start_m = window['start']
    candidate = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def wait_for_exec_window(windows, _out):
    """
    Block until current time falls within one of the given windows.
    No-op if windows is empty (unrestricted). Prints progress every
    _SLEEP_REPORT_INTERVAL seconds, matching the rate-limit sleep style.
    """
    if not windows:
        return
    now = datetime.now(timezone.utc)
    if any(_in_exec_window(now, w) for w in windows):
        return
    wake_dt = min(_next_exec_window_start(now, w) for w in windows)
    sleep_secs = max(0.0, (wake_dt - now).total_seconds())
    _out(
        f'  Exec window closed -- next window opens '
        f'{wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")}; sleeping {sleep_secs:.0f}s'
    )
    remaining = sleep_secs
    while remaining > 0:
        chunk = min(remaining, _SLEEP_REPORT_INTERVAL)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            still_left = max(0.0, (wake_dt - datetime.now(timezone.utc)).total_seconds())
            _out(
                f'  Exec window sleep -- {still_left:.0f}s remaining '
                f'(wake {wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")})'
            )
    _out('  Exec window open -- proceeding.')


# ---------------------------------------------------------------------------
# Config loading and merging
# ---------------------------------------------------------------------------

_BUILD_CONFIG_RE = re.compile(
    r'^## Build Config\s*\n+```yaml\n(.*?)```',
    re.DOTALL | re.MULTILINE,
)


def _require_yaml():
    if not _HAVE_YAML:
        print(
            'ERROR: PyYAML is required to parse YAML config. '
            'Install with: pip install pyyaml',
            file=sys.stderr,
        )
        sys.exit(1)


def load_plan_config(plan_text):
    """Extract the Build Config YAML block from the plan, if present."""
    m = _BUILD_CONFIG_RE.search(plan_text)
    if not m:
        return {}
    _require_yaml()
    return yaml.safe_load(m.group(1)) or {}


def load_config_file(path):
    _require_yaml()
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_models_file(path):
    """
    Read a models list file (one model ID per line).
    Blank lines and lines starting with '#' are ignored.
    Returns a dict mapping each model ID to itself (self-mapping aliases).
    These entries can be used in plan Model: fields as full IDs and are
    treated the same as any explicit alias in the models dict.
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
    """file_cfg values override plan_cfg values."""
    merged = dict(plan_cfg)
    merged.update(file_cfg)
    # Deep-merge the models dict so partial overrides work.
    plan_models = plan_cfg.get('models', {})
    file_models = file_cfg.get('models', {})
    if plan_models or file_models:
        merged_models = dict(plan_models)
        merged_models.update(file_models)
        merged['models'] = merged_models
    return merged


def resolve_model(alias, config):
    """
    Resolve 'haiku', 'sonnet', 'opus' (or a full model ID) to a full ID.
    Lookup order: config models dict -> DEFAULT_MODEL_IDS -> pass through as-is.
    """
    aliases = config.get('models') or {}
    return aliases.get(alias) or DEFAULT_MODEL_IDS.get(alias) or alias


def resolve_task_effort(task, cli_effort, config):
    """
    Return the effective effort string for a task (or verification dict).
    Precedence: CLI --effort (global override) > task Effort: field > config effort key.
    Returns empty string when no effort is set at any level.
    """
    if cli_effort:
        return cli_effort
    task_effort = (task.get('effort') or '').strip().lower()
    if task_effort:
        return task_effort
    return str(config.get('effort') or '').strip().lower()


def resolve_task_exec_window(task, config):
    """
    Return the effective exec-window spec string for a task (or verification
    dict). Precedence: task ExecWindow: field > config exec_window key.
    Returns '' (unrestricted) when neither is set.
    """
    task_window = (task.get('exec_window') or '').strip()
    if task_window:
        return task_window
    return str(config.get('exec_window') or '').strip()


def validate_config_types(config):
    """
    Hard error (exit 1) when a config key expected to hold a specific type
    holds an incompatible one. Catches YAML authoring mistakes -- e.g.
    add_dirs: /some/path instead of add_dirs: [/some/path] -- before they
    reach a crash site (models as a non-dict breaks resolve_model()) or
    silently misbehave (add_dirs as a bare string iterates per-character
    into bogus --add-dir flags; allowed_tools as a bare string raises an
    unhandled TypeError when concatenated with a list in run_claude()).
    A null/missing value for any of these keys is valid (treated as empty
    by its caller) and is not an error.
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
    Given the text after a task heading line, return (model, files, prompt_body).

    Per-task fields (Model:, Files:) appear immediately after the heading with
    no blank lines between them. A blank line ends the fields block; everything
    after that is the prompt body.
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
            # Comment line inside the fields block -- skip, do not advance field_end.
            continue
        elif stripped == '':
            # Blank line ends the fields block; body starts after it.
            field_end = i + 1
            break
        else:
            # First non-field, non-comment, non-blank line starts the prompt body
            # right here -- no skip, no distance heuristic.
            field_end = i
            break

    prompt_lines = lines[field_end:]
    # Strip leading and trailing blank lines from prompt body.
    while prompt_lines and prompt_lines[0].strip() == '':
        prompt_lines.pop(0)
    while prompt_lines and prompt_lines[-1].strip() == '':
        prompt_lines.pop()

    return model, files, effort, exec_window, '\n'.join(prompt_lines)


def _section_end(text, start):
    """
    Return the index (in text) where the current section ends.
    A section ends at the next --- rule or ## heading.
    """
    tail = text[start:]
    m = _SECTION_END_RE.search(tail)
    return start + m.start() if m else len(text)


def parse_tasks(plan_text):
    """
    Return a list of task dicts, sorted by task number:
      { num, title, model, files, prompt_body }
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
    Validate parsed tasks for collisions and numbering gaps.

    Hard error (exit 1) on duplicate Task N numbers -- running the same task
    twice can double-edit files. Warning only (stderr) on a non-gap-free or
    non-1-start numbering sequence -- may be intentional during plan editing.
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
    overlapping Files: entries -- parallel workers writing the same file can
    race and clobber each other's edits.
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
    Return { model, prompt_body } for the ## Verification section, or None.
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

def build_prompt(task_dict, config):
    """
    Prepend repo path, optional preamble, and optional license header to the
    task's prompt body. Kept minimal to avoid unnecessary context overhead.
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
        try:
            header = Path(license_file).read_text(encoding='utf-8').rstrip()
            parts.append(
                'Use this exact license header for all new Python files:\n\n' + header
            )
        except OSError as e:
            print(f'WARNING: cannot read license_file {license_file}: {e}', file=sys.stderr)

    if parts:
        parts.append('')  # blank line before prompt body
    parts.append(task_dict['prompt_body'])
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def make_run_log_dir(config, plan_path, run_ts):
    base = str(config.get('log_dir') or '').strip()
    if not base:
        base = str(Path(plan_path).resolve().parent.parent / 'tmp_build_logs')
    plan_stem = Path(plan_path).stem
    log_dir = Path(base) / f'{plan_stem}_{run_ts}'
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def write_log(log_dir, filename, content):
    p = log_dir / filename
    p.write_text(content, encoding='utf-8')
    return p


def write_completion_marker(log_dir, task_num, task_title, exit_code):
    """Write a marker file for a finished task.

    Filename ends with _completed.txt on exit 0, _failed.txt otherwise.
    """
    ts       = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    date_str = ts[:10]
    status   = 'completed' if exit_code == 0 else 'failed'
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

def run_claude(prompt, model, dry_run, log_dir, label, add_dirs=None, allowed_tools=None, extra_claude_args=None, _lines=None, exec_windows=None):
    """
    Pipe prompt to claude on stdin. Capture stdout+stderr to log files
    and echo to the terminal. Return (exit_code, usage_dict).
    usage_dict keys: input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens. All zero on failure or dry-run.
    add_dirs: list of directory paths to pass via --add-dir.
    _lines: if a list, append output lines to it instead of printing (for
            parallel execution -- caller prints the buffer atomically).
    exec_windows: parsed list from parse_exec_windows(), or None/[] for no
            restriction. Checked before every attempt, including attempts
            after a rate-limit retry wake-up.
    Output token count is printed as a dedicated line. On a 429/rate-limit
    response that includes a reset time, sleeps until (reset_time + 10
    minutes) and retries, up to _MAX_RATE_LIMIT_RETRIES times. Raises
    RateLimitError if rate-limited with no parseable reset time or if all
    retries are exhausted.
    """
    def _out(s=''):
        if _lines is not None:
            _lines.append(s)
        else:
            print(s)

    ts = datetime.now(timezone.utc).strftime('%H:%M:%S+00:00')
    prompt_log = write_log(log_dir, f'{label}_{ts}_prompt.txt', prompt)
    _out(f'  prompt  -> {prompt_log}')

    _zero_usage = {
        'input_tokens': 0, 'output_tokens': 0,
        'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
    }

    if dry_run:
        _out('-- DRY RUN: prompt follows --')
        _out(prompt)
        _out('-- END prompt --')
        return 0, _zero_usage

    tools = allowed_tools or DEFAULT_ALLOWED_TOOLS
    claude_cmd = ['claude', '--model', model, '-p', '--output-format', 'json',
                  '--allowedTools'] + tools
    for d in (add_dirs or []):
        claude_cmd += ['--add-dir', d]
    claude_cmd += (extra_claude_args or [])

    cmd = claude_cmd

    attempt = 0
    while True:
        attempt += 1
        wait_for_exec_window(exec_windows, _out)
        run_ts = datetime.now(timezone.utc).strftime('%H:%M:%S+00:00')
        t0 = time.monotonic()

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
        )
        raw, err = proc.communicate(input=prompt)
        elapsed = time.monotonic() - t0

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
        _out(f'  output tokens: {tok_out}')
        _out(text_output)

        # text_output has the decoded human-readable result (JSON unescaped);
        # search it first so natural-language time patterns resolve correctly.
        combined = text_output + '\n' + raw + '\n' + err
        # Require the rate-limit keyword regardless of signal source, so a
        # successful response that merely echoes a time+timezone string
        # cannot false-positive. Exit code != 0 OR a specific, low-false-
        # positive reset-time pattern (IANA time/date, ISO 8601, or a
        # relative "retry in N seconds/minutes/hours" phrase) is accepted as
        # the secondary signal, catching limits that report exit 0 in any of
        # parse_reset_time()'s supported formats. The bare 12-hour-clock
        # pattern (_RESET_12H_RE, e.g. a lone "9:00 PM") is deliberately
        # excluded here -- it is too generic to use as a standalone signal
        # on a successful exit.
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
                wake_dt    = reset_dt + timedelta(minutes=10)
                now_dt     = datetime.now(timezone.utc)
                sleep_secs = max(0.0, (wake_dt - now_dt).total_seconds())
                _out(
                    f'  Rate limit -- resets '
                    f'{reset_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")}; '
                    f'sleeping {sleep_secs:.0f}s '
                    f'(wake {wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")})'
                )
                remaining = sleep_secs
                while remaining > 0:
                    chunk = min(remaining, _SLEEP_REPORT_INTERVAL)
                    time.sleep(chunk)
                    remaining -= chunk
                    if remaining > 0:
                        still_left = max(0.0, (wake_dt - datetime.now(timezone.utc)).total_seconds())
                        _out(
                            f'  Rate limit sleep -- {still_left:.0f}s remaining '
                            f'(wake {wake_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")})'
                        )
                _out('  Retrying ...')
                continue
            raise RateLimitError(text_output.strip()[:300])

        break

    return proc.returncode, usage


def _task_worker(task, model, prompt, dry_run, log_dir, label, add_dirs, allowed_tools=None, extra_claude_args=None, exec_windows=None):
    """
    Worker for parallel execution. Buffers all output, returns
    (task_num, rc, usage, lines) where lines is a list of strings to print.
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

    rc, usage = run_claude(prompt, model, dry_run, log_dir, label, add_dirs, allowed_tools, extra_claude_args, _lines=lines, exec_windows=exec_windows)
    marker = write_completion_marker(log_dir, task['num'], task['title'], rc)
    lines.append(f'  marker  -> {marker}')
    return task['num'], rc, usage, lines


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog='autobuilderclaude',
        description='autobuilderclaude v1.6.3 -- Document-driven Claude task runner (autobuilderclaude format v1).',
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
                   help='Override per-task model (haiku|sonnet|opus, a full Claude model ID, or a provider/model:tag ID such as nvidia/nemotron-3-super-120b-a12b:free)')
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
# Main
# ---------------------------------------------------------------------------

def main():
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

    # Load models_file if specified: self-map each model ID as an alias.
    # Explicit models: entries in config take precedence over models_file entries.
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

    # Pass-through args go to every claude invocation unchanged.
    # Effort is resolved per-task via resolve_task_effort(); --effort is the global override.
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

    # --stop-after: trim the selected list and suppress verification.
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
    print(f'Log dir: {log_dir}')
    if args.effort:
        print(f'Effort (global override): {args.effort}')
    if pass_through_args:
        print(f'Claude pass-through args: {" ".join(pass_through_args)}')
    if args.stop_after is not None:
        print(f'Stop after: task {args.stop_after}')
    if args.parallel > 1:
        print(f'Parallel: {args.parallel} workers')

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

    if args.parallel > 1 and len(selected) > 1:
        warn_parallel_file_collisions(selected)

        # Build all (task, model, prompt, label, task_claude_args) tuples up front.
        work_items = []
        for task in selected:
            model_key = args.model or task['model'] or default_model
            model     = resolve_model(model_key, config)
            safe_title = re.sub(r'[^a-zA-Z0-9_-]', '_', task['title'])[:40]
            label      = f'task_{task["num"]:03d}_{safe_title}'
            prompt     = build_prompt(task, config)
            effort     = resolve_task_effort(task, args.effort, config)
            task_claude_args = (['--effort', effort] if effort else []) + pass_through_args
            exec_windows = parse_exec_windows(resolve_task_exec_window(task, config))
            work_items.append((task, model, prompt, label, task_claude_args, exec_windows))

        # Explicit try/finally (not `with`) so every exit path -- success,
        # RateLimitError, or any other worker exception -- gets the fast
        # non-blocking shutdown. ThreadPoolExecutor.__exit__ defaults to
        # shutdown(wait=True), which would block on all other in-flight
        # workers (potentially hours, if any are sleeping in an exec-window
        # or rate-limit wait) before an unhandled exception's traceback
        # surfaces.
        executor = ThreadPoolExecutor(max_workers=args.parallel)
        try:
            futures = {
                executor.submit(
                    _task_worker, task, model, prompt, args.dry_run, log_dir, label, add_dirs, allowed_tools_list, task_claude_args, exec_windows
                ): task['num']
                for task, model, prompt, label, task_claude_args, exec_windows in work_items
            }
            for future in as_completed(futures):
                try:
                    task_num, rc, usage, lines = future.result()
                except RateLimitError as e:
                    with print_lock:
                        print(f'\nERROR: rate limit reached -- {e}', file=sys.stderr)
                        print('Remaining tasks skipped.', file=sys.stderr)
                    sys.exit(1)
                with print_lock:
                    print('\n'.join(lines))
                _accumulate(rc, usage, task_num)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    else:
        for task in selected:
            model_key = args.model or task['model'] or default_model
            model     = resolve_model(model_key, config)
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

            prompt = build_prompt(task, config)
            try:
                rc, usage = run_claude(prompt, model, args.dry_run, log_dir, label, add_dirs, allowed_tools_list, task_claude_args, exec_windows=exec_windows)
            except RateLimitError as e:
                print(f'\nERROR: rate limit reached -- {e}', file=sys.stderr)
                print('Remaining tasks skipped.', file=sys.stderr)
                sys.exit(1)
            marker = write_completion_marker(log_dir, task['num'], task['title'], rc)
            print(f'  marker  -> {marker}')
            _accumulate(rc, usage, task['num'])

    if run_verify and verification:
        model_key    = args.model or verification['model']
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

        prompt = build_prompt(verification, config)
        try:
            rc, usage = run_claude(prompt, model, args.dry_run, log_dir, 'verify', add_dirs, allowed_tools_list, verify_claude_args, exec_windows=exec_windows)
        except RateLimitError as e:
            print(f'\nERROR: rate limit reached -- {e}', file=sys.stderr)
            sys.exit(1)
        _accumulate(rc, usage)

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
