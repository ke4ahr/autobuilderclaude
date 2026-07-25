# autobuilderclaude

Document-driven Claude task runner (autobuilderclaude format v1).

https://github.com/ke4ahr/autobuilderclaude

Reads an implementation plan written in Markdown, extracts tasks, and
executes each one by piping the task prompt to `claude` via the CLI.
Tasks may run sequentially or concurrently. All prompts and responses
are captured to timestamped log files. Token usage is reported per task
and as a run total. Each task is timed; elapsed time is printed in both
seconds and minutes forms (e.g. `426.7s, 7m6.700s`). On a 429/rate-limit
response that contains a reset time, the script sleeps until (reset_time
+ 10 minutes) and retries the failing task automatically. Sleeps longer
than 24 hours are supported; a progress line is printed every 2 hours
during the wait. When an invocation runs longer than 29 minutes, exits
non-zero, and produces no output, it is treated as a hung or crashed run;
a second such occurrence within the same task's retry loop is fatal.

## Requirements

- Python 3.10+
- `claude` CLI on PATH
- `pyyaml` (only required when using YAML config files or plan Build Config blocks)

## Installation

### Using a virtual environment (recommended)

```
python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml
```

To activate the venv in future sessions:

```
source .venv/bin/activate
```

To deactivate:

```
deactivate
```

### Without a virtual environment

```
pip install pyyaml
```

## Usage

```
autobuilderclaude.py --input PLAN [--template TEMPLATE] [--config CONFIG] [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--input PLAN` | Implementation plan .md file (required) |
| `--template TEMPLATE` | YAML file providing base defaults; overridden by the plan's Build Config |
| `--config CONFIG` | YAML file overriding both the template and the plan's Build Config |
| `--task N` | Run only task N (integer) or `verify` |
| `--start-task N` | Start at task N and run through all remaining tasks |
| `--stop-after N` | Stop after task N completes (inclusive); skips verification |
| `--model MODEL` | Override per-task model for all tasks (`haiku`, `sonnet`, `opus`, or full model ID) |
| `--effort LEVEL` | Global effort override: `low`, `medium`, `high`, `xhigh`, `max`; overrides per-task `Effort:` field and config `effort` key |
| `--parallel N` | Number of tasks to run concurrently (default: 1) |
| `--dry-run` | Print resolved prompts without calling claude |
| `--list` | List all tasks with resolved models, then exit |

Any flag not recognized by autobuilderclaude is passed through to the claude CLI unchanged.

### Config precedence (lowest to highest)

```
--template  <  plan ## Build Config  <  --config
```

Any key present in a higher-priority source overrides the same key from
a lower-priority source. Use `--template` for shared defaults across
multiple plans; use `--config` for per-run overrides.

## Plan format

Plans are Markdown files with an optional `## Build Config` YAML block
and one or more `### Task N -- title` sections.

```markdown
## Build Config

` ``yaml
repo:          /absolute/path/to/repo
log_dir:       /absolute/path/to/logs
license_file:  /absolute/path/to/LICENSE_HEADER.txt
default_model: sonnet
models:
  haiku:  claude-haiku-4-5-20251001
  sonnet: claude-sonnet-4-6
  opus:   claude-opus-4-6
` ``

### Task 1 -- short title
Model: haiku
Files: lib/db.py

Prompt text here. Describe exactly what claude should create or modify.

### Task 2 -- complex analysis
Model: sonnet
Effort: high

Prompt text for a task that needs high effort. Omit Effort: to use the
config effort key (or claude's default if neither is set).
```

Tasks are executed in numeric order when sequential. With `--parallel`,
tasks are dispatched concurrently and may complete out of order; each
task's output is buffered and printed as a complete block when it
finishes. The optional `## Verification` section always runs after all
tasks complete.

### Task fields

| Field | Required | Description |
|-------|----------|-------------|
| `Model:` | no | `haiku`, `sonnet`, `opus`, or full model ID. Falls back to `default_model`. |
| `Files:` | no | Comma-separated list of target files (informational; shown in header). |
| `Effort:` | no | Per-task effort level: `low`, `medium`, `high`, `xhigh`, `max`. Overrides config `effort`; overridden by CLI `--effort`. |

## Config file format

Config files are YAML. All keys are optional -- include only what you
want to override.

```yaml
repo:          /absolute/path/to/repo
add_dirs:
  - /absolute/path/to/extra/dir    # optional; repeat for multiple
log_dir:       /absolute/path/to/logs
license_file:  /absolute/path/to/LICENSE_HEADER.txt
preamble: |
  Complete ALL steps without asking for confirmation.
  Do not stop mid-task. Apply all changes as directed.
default_model: sonnet
models:
  haiku:  claude-haiku-4-5-20251001
  sonnet: claude-sonnet-4-6
  opus:   claude-opus-4-6
```

`docs/autobuilderclaude_config_v1.yaml` is a template with placeholder values.
Copy it, fill in real paths, and pass it via `--template` or `--config`.

### Config keys

| Key | Description |
|-----|-------------|
| `repo` | Absolute path to the project root. Passed to claude as `Working directory:` and via `--add-dir`. |
| `add_dirs` | List of additional absolute paths to pass via `--add-dir`. Use when tasks must read or write files outside `repo` (e.g. a shared memory directory). |
| `log_dir` | Directory for log files. Created if absent. Default: `../tmp_build_logs` relative to the plan file. |
| `license_file` | Path to a plain-text license header. Injected verbatim into every prompt. Set to `null` to skip. |
| `preamble` | Text injected into every task prompt after `Working directory:` and before the task body. Use to set agent behavior (e.g. "complete all steps without asking for confirmation"). Omit or set to `""` to skip. |
| `allowed_tools` | List of tools passed via `--allowedTools`. Set to `null` or omit to use the built-in default: `['Bash', 'Edit', 'Read', 'Write']`. Include `mcp__GhidraMCP__*` (or other `mcp__*` entries) explicitly if a task needs MCP tool access -- it is not granted by the default. |
| `default_model` | Model alias used when a task has no `Model:` field. Default: `sonnet`. |
| `effort` | Default effort level for claude (`low`/`medium`/`high`/`xhigh`/`max`). Overridden by a task's `Effort:` field or by CLI `--effort`. Omit for claude's default. |
| `exec_window` | Restrict execution to specific days and/or times. Format per entry: `[DAY[,DAY...] ]HH:MM-HH:MM [TZ]`; entries separated by `;`. Day abbreviations (case-insensitive): `Mo Tu We Th Fr Sa Su`; omit for all days. `start>end` wraps past midnight. TZ is an IANA zone or abbreviation; omitted TZ defaults to UTC. `null`/omit for unrestricted. Overridden per task by `ExecWindow:` field. See [Execution windows](#execution-windows). |
| `models` | Dict mapping `haiku`/`sonnet`/`opus` aliases to full model IDs. |

`models`, `add_dirs`, and `allowed_tools` are type-checked at startup: `null`/omitted
is always valid, but a value of the wrong type (e.g. `add_dirs: /some/path` instead of
a list) exits 1 with a clear error naming the bad key and the type found, instead of
crashing later with a raw traceback.

## Claude invocation

Each task runs:

```
claude --model MODEL -p --output-format json --allowedTools ... --add-dir REPO [--add-dir DIR ...] [EXTRA_ARGS] < prompt
```

`--allowedTools` permits claude to write files without interactive
permission prompts. `--add-dir REPO` grants file access to the repo
directory. Each entry in `add_dirs` adds another `--add-dir` flag.
JSON output format is used to capture token usage.

`EXTRA_ARGS` are any flags not recognized by autobuilderclaude (passed
through unchanged), plus `--effort LEVEL` resolved per task. Effort
precedence: CLI `--effort` (global) > task `Effort:` field > config
`effort` key.

## Parallel execution

`--parallel N` runs up to N tasks concurrently using a thread pool.
Each task gets its own log files. Output is buffered per task and
printed as a complete block when the task finishes, so blocks do not
interleave.

The verification step always runs sequentially after all tasks complete,
regardless of `--parallel`.

Use `--parallel` for independent tasks (e.g. separate library files).
Avoid it for tasks with ordering dependencies.

If two or more concurrently-selected tasks declare the same `Files:` entry,
a non-fatal warning is printed before the run starts, listing the file and
the colliding task numbers. The run still proceeds -- review the warning and
re-run sequentially (or split `--task`) if the collision is unintentional.

If any worker raises an exception -- a rate limit or otherwise -- the thread
pool aborts promptly without waiting for other in-flight workers (including
any sleeping through an exec-window or rate-limit wait) to finish first.

## Token usage

Token counts are printed after each task on the output line, followed by a
dedicated output-token line:

```
  output  -> /path/to/1997-07-16T19:20:30+00:00_output.txt  (4.2s, exit 0)  tokens: in=1234 out=567 cache_read=890 cache_write=0
  output tokens: 567
```

A cumulative total is printed at the end of the run:

```
Done.  total tokens: in=5432 out=2109 cache_read=1780 cache_write=0
```

Fields: `in` = input tokens, `out` = output tokens, `cache_read` = tokens
read from prompt cache, `cache_write` = tokens written to prompt cache.

## Execution windows

The `exec_window` config key (and per-task `ExecWindow:` field) restrict when tasks
may run. Outside a window the script sleeps until the next window opens, then resumes.
This sleep loop re-runs after any rate-limit retry wake-up as well.

### Format

```
[DAY[,DAY...] ]HH:MM-HH:MM [TZ] [; [DAY[,DAY...] ]HH:MM-HH:MM [TZ] ...]
```

- **Day prefix** (optional): comma-separated 2-3 letter abbreviations (case-insensitive).
  `Mo Tu We Th Fr Sa Su` (also `Mon Tue Wed Thu Fri Sat Sun`).
  Omit to allow all days (backward-compatible with existing specs).
- **Time range**: `HH:MM-HH:MM`. `start > end` wraps past midnight (e.g. `18:00-06:00`
  is open from 18:00 until 06:00 the following morning).
- **TZ**: IANA zone (`America/Chicago`) or abbreviation (`CDT`/`CST`/`UTC`).
  Defaults to UTC if omitted.
- **Multiple entries**: separated by `;`.

### Wrapping windows with day filter

When `start > end` and a day filter is set, the window is open when:
- The current local day is in the day filter AND the time is >= start, OR
- The current local day is the day AFTER a day in the filter AND the time is < end.

Example: `Tu,Th 18:00-06:00 America/Chicago` -- open Tuesday 18:00 through Wednesday
06:00 AND Thursday 18:00 through Friday 06:00.

### Examples

| Spec | Meaning |
|------|---------|
| `17:00-08:00 America/Chicago` | Overnight every day (existing format) |
| `Sa,Su 00:00-00:00 America/Chicago` | All day Saturday + Sunday |
| `Tu,Th 18:00-06:00 America/Chicago` | Tuesday and Thursday nights, wrapping to Wed/Fri 06:00 |
| `Mo,Tu,We,Th,Fr 17:00-08:00 CDT; Sa,Su 00:00-00:00 CDT` | Weeknights + all weekend |

## Rate-limit handling

When claude's output contains a usage-rate-limit message, the script attempts to
extract a reset time from the message. The message must first match one of the
recognized limit-keyword phrases: `hit your limit`, `hit your <word> limit` (e.g.
"hit your session limit", "hit your weekly limit" -- any single word between
"your" and "limit"), `usage limit`, `rate limit`/`rate-limit`, `exceeded ... limit`,
or `limit ... exceeded`. If the keyword matches, it is treated as a real rate
limit if claude exited non-zero, OR -- on exit 0 -- the message matches one of the
low-false-positive reset-time patterns below (ISO 8601, an IANA-zone date or time,
or a relative offset phrase). A bare 12-hour clock with no date and no IANA zone
(e.g. a lone "9:00 PM") is too generic to trust as a standalone signal on a
successful exit, so it only triggers retry handling when claude also exited
non-zero. Five reset-time formats are recognized:

- ISO 8601 (e.g. `2026-05-22T14:00:00Z`)
- 12-hour clock with TZ abbreviation, with or without a date (e.g.
  `9:00 PM CDT on Thursday, May 22, 2026` or just `9:00 PM CDT`). When no date is
  given, today is assumed; tomorrow is assumed only if the time passed more than
  1 hour ago. A reset time that passed within the last hour is treated as the
  current reset boundary (resulting in an immediate retry rather than a 24-hour
  wait), guarding against repeated day-rollover when the script wakes slightly
  late from an earlier rate-limit sleep.
- Relative offsets (e.g. `retry after 60 seconds`)
- Date + time + IANA zone (e.g. `May 26, 12pm (America/Chicago)`)
- Time-only + IANA zone (e.g. `7:20am (America/Chicago)`). When no date is given,
  today is assumed; tomorrow is assumed only if the time passed more than 1 hour
  ago (same 1-hour-grace logic as the TZ-abbreviation format above).

If a reset time is found, the script sleeps until (reset_time + 10 minutes)
and then retries the failing task automatically, up to 3 times:

```
  Rate limit -- resets 2026-05-22T14:00:00+00:00; sleeping 3612s (wake 2026-05-22T15:00:12+00:00)
  Retrying ...
```

For sleeps longer than 2 hours, a progress line is printed every 2 hours:

```
  Rate limit sleep -- 86392s remaining (wake 2026-05-23T15:00:12+00:00)
```

If the retry succeeds, processing continues with the next task normally.

If no reset time is found in the message, or if all 3 retries also hit a
rate limit, the run aborts immediately:

```
ERROR: rate limit reached -- <message excerpt>
Remaining tasks skipped.
```

## Fatal invocation detection

An invocation is flagged when all three of these conditions hold simultaneously:

- It ran for longer than 29 minutes (measured from `Popen` to `communicate()` return,
  excluding any rate-limit sleep time between retries)
- It exited with a non-zero return code
- It produced no output (stdout is empty after JSON decoding)

The first such occurrence within a task's retry loop emits a warning and allows
processing to continue:

```
  WARNING: invocation ran 1758s with no output (strike 1 of 2 before fatal; exit 1)
```

The second occurrence is fatal: the script prints an error to stderr and exits 1,
skipping all remaining tasks:

```
FATAL: invocation ran 1812s (>1740s), exited 1, produced no output (second occurrence -- aborting)
Remaining tasks skipped.
```

An additional rule applies when the previous retry was a rate-limit hit: if the
next attempt then meets the timeout and no-output criteria, it is immediately
fatal without waiting for a second occurrence:

```
FATAL: invocation ran 1810s (>1740s), exited 1, produced no output (previous attempt was a rate-limit hit -- aborting)
Remaining tasks skipped.
```

This catches the pattern where a task never gets to run (first attempt hits a rate
limit; second attempt hangs until the session-limit compaction hook fires and claude
exits non-zero with no output).

## Log files

Each run creates a timestamped subdirectory under `log_dir`:

```
{log_dir}/{plan_stem}_{YYYY-MM-DDThh:mm:ss+00:00}/
  task_001_{title}_{hh:mm:ss+00:00}_prompt.txt
  task_001_{title}_{hh:mm:ss+00:00}_output.txt
  task_001_{title}_{hh:mm:ss+00:00}_stderr.txt  (only if claude wrote to stderr)
  task_1_YYYY-MM-DD_completed.txt   (exit 0)
  task_1_YYYY-MM-DD_failed.txt      (exit non-zero)
  task_002_...
  verify_{hh:mm:ss+00:00}_prompt.txt
  verify_{hh:mm:ss+00:00}_output.txt
```

Output files contain the text response only (JSON envelope stripped).
Stderr files capture any output written to stderr by the claude subprocess.

Each completed task drops a marker file named
`task_N_YYYY-MM-DD_completed.txt` (exit 0) or `task_N_YYYY-MM-DD_failed.txt`
(non-zero exit). The marker contains the task number, title, date, and exit
code. Tasks aborted by a rate-limit error do not produce a marker.

## Examples

Run a single task using only the plan's embedded config:

```
autobuilderclaude --input docs/plan.md --task 1
```

Run all tasks with a shared template for defaults:

```
autobuilderclaude --input docs/plan.md --template docs/autobuilderclaude_config_v1.yaml
```

Run tasks 1-5 concurrently (3 at a time):

```
autobuilderclaude --input docs/plan.md --parallel 3
```

Dry-run to preview all prompts:

```
autobuilderclaude --input docs/plan.md --dry-run
```

List tasks and resolved models without running anything:

```
autobuilderclaude --input docs/plan.md --list
```

Run verification only:

```
autobuilderclaude --input docs/plan.md --task verify
```

Override model for a one-off test:

```
autobuilderclaude --input docs/plan.md --task 3 --model sonnet
```

Run with high effort globally (overrides any per-task `Effort:` field):

```
autobuilderclaude --input docs/plan.md --effort high
```

Per-task effort is set in the plan (`Effort: high` under a task heading);
`--effort` on the command line overrides all per-task fields for the run.

Run tasks 1 through 5 only (skip the rest and skip verification):

```
autobuilderclaude --input docs/plan.md --stop-after 5
```

Pass an extra claude flag through (e.g. `--verbose`):

```
autobuilderclaude --input docs/plan.md --verbose
```

Copyright (C) 2026 Kris Kirby

SPDX-License-Identifier: GPL-3.0-or-later

[2026-04-15] created -- auto-builder toolchain (precursor to autobuilderclaude)
  Origin project: openscraper
  Files created at that time:
    tmp_os/auto-builder.py               -- Python driver; parses plan,
                                            runs claude per-task, captures I/O
    docs/autobuilder_plan_template_v1.md -- format spec + template (format v1)
    docs/autobuilder_config_v1.yaml      -- runtime config template
    openscraper/LICENSE_HEADER.txt       -- extracted from existing .py files
  CLI: auto-builder.py --input PLAN.md [--config CONFIG.yaml]
                       [--task N|verify] [--model MODEL] [--dry-run] [--list]
  Model per task read from plan document (Model: haiku|sonnet|opus).
  All claude I/O captured to tmp_build_logs/{plan_stem}_{timestamp}/.
  Verification section runs last via Sonnet.

[2026-04-16T17:36:00+00:00] run -- first execution against Syntor X9000 RE plan
  task_implementation_plan.md, config x9000_autobuilder_config.yaml
  Task 1 ran 433s, exited 1 (rate limit hit mid-task)
  Tasks 2-9 + verification exited 1 immediately (rate limit, 0 tokens each)
  Root cause: Sonnet daily usage limit exhausted; resets 2026-04-17 14:00 America/Chicago

[2026-04-16T19:45:00+00:00] patch v1.1.1 -> v1.2.0
  autobuilderclaude.py:
    - Added _RATE_LIMIT_RE and RateLimitError class
    - run_claude: raises RateLimitError when exit != 0 and "hit your limit" in output
    - Sequential loop: catches RateLimitError, aborts with message, skips remaining tasks
    - Parallel loop: catches RateLimitError from future.result(), aborts
    - Verification block: catches RateLimitError
    - write_completion_marker: _completed.txt on exit 0, _failed.txt on non-zero exit
    - Removed unused import os
    - Parallel abort: executor.shutdown(wait=False, cancel_futures=True) before sys.exit
  README.md:
    - Added "Rate-limit handling" section
    - Updated log files section with marker file naming and semantics

[2026-04-16T20:12:48+00:00] patch v1.2.0 -- timestamp format update
  autobuilderclaude.py:
    - run_ts strftime: '%Y-%m-%dT%H%MZ' -> '%Y-%m-%dT%H:%M:%S+00:00'
    - run_claude ts strftime: '%H%M%SZ' -> '%H:%M:%S+00:00'
    - write_completion_marker: '%Y-%m-%d' -> '%Y-%m-%dT%H:%M:%S+00:00'; field renamed date: -> timestamp:
  README.md:
    - Log file path examples updated to YYYY-MM-DDThh:mm:ss+00:00 format
  activity.log, openscraper/activity.log:
    - Format descriptor comments updated to YYYY-MM-DDThh:mm:ss+00:00

[2026-04-23T06:03:38+00:00] patch v1.2.1 -- change autobuilder_ to autobuilderclaude_
  - matches script filename, clues LLMs into the relevance of the files.
  - Updated README.md.

[2026-05-02T06:05:49+00:00] feature -- additional model support (OpenRouter / models_file)
  autobuilderclaude.py:
    - Removed stale commented-out example in DEFAULT_MODEL_IDS
    - Added load_models_file(path): reads one model ID per line (skips # and blank),
      returns self-mapping dict so each ID is usable verbatim in plan Model: fields
    - main(): if config key models_file is set, loads file and merges into models dict;
      explicit models: entries take precedence over models_file entries
    - --model help text updated to mention provider/model:tag format
  autobuilderclaude_config_v1.yaml:
    - Added models_file option (null by default) with OpenRouter env var docs
    - Added example short-alias comments in models: section
    - Added note that full provider IDs work directly in plan Model: fields
  autobuilderclaude_plan_template_v1.md:
    - PER-TASK FIELDS: Model: now shows <full-model-id> as accepted value
    - OpenRouter env var requirements documented (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN)
    - Build Config block: added models_file example and short-alias example
  OpenRouter env vars: ANTHROPIC_BASE_URL=https://openrouter.ai/api,
    ANTHROPIC_AUTH_TOKEN=<key>, OPENROUTER_API_KEY=<same key>. ANTHROPIC_API_KEY empty.
  syntorx9000 copy synced: autobuilderclaude.py (already identical), config, plan template.

# line 375
[2026-05-21T08:02:12+00:00 / 2026-05-21T03:02:12-0500] patch v1.2.x -> v1.3.1
  autobuilderclaude.py:
    - Version bump to 1.3.1
    - Added timedelta to datetime imports
    - Expanded _RATE_LIMIT_RE to catch "usage limit", "rate limit", "exceeded.*limit"
    - Added parse_reset_time(message): extracts reset UTC datetime from rate-limit
      messages; handles ISO 8601, 12-hour clock + TZ abbreviation, relative offsets
    - Added _RESET_ISO_RE, _RESET_12H_RE, _RESET_RELATIVE_RE, _TZ_OFFSETS, _MONTH_NAMES
    - run_claude: wraps claude_cmd with bash -c 'time "$@"' -- to use bash time builtin;
      stderr captured separately (time_raw) instead of merged into stdout
    - run_claude: prints dedicated "output tokens: N" line after each invocation
    - run_claude: prints bash time output (real/user/sys) as "time: <line>" entries
    - run_claude: retry loop on rate-limit: if reset time parseable and attempt==1,
      sleeps until (reset_dt + 10 min) and retries; raises RateLimitError on second
      failure or when no reset time is found
    - run_claude: attempt suffix (_attempt2) appended to output log filename on retry
  README.md:
    - Updated description to mention bash time, output tokens, and auto-retry
    - Added "Bash time output" section
    - Updated "Rate-limit handling" section to describe sleep-and-retry behavior

# line 397
[2026-05-21T15:51:32+00:00 / 2026-05-21T10:51:32-0500] fix v1.3.1 -- rate-limit reset time parse failure
  Root cause: Claude CLI outputs "resets 7:20am (America/Chicago)" -- a time-only
  string with an IANA timezone name in parens. _RESET_12H_RE requires a full date
  (month/day/year) and did not match. _RESET_ISO_RE and _RESET_RELATIVE_RE also
  did not match. parse_reset_time returned None -> RateLimitError raised -> abort.
  autobuilderclaude.py:
    - Added zoneinfo import (with ImportError fallback) and _HAVE_ZONEINFO flag
    - Added _RESET_TIME_IANA_RE: r'(\d{1,2}:\d{2})\s*([ap]m)\s*\(([A-Za-z_/]+)\)'
    - parse_reset_time: new branch for IANA time-only format; uses ZoneInfo to
      resolve the named timezone; assumes same day if time is still future, else
      next day; returns UTC-aware datetime
    - run_claude: changed combined to text_output + raw + time_raw so the decoded
      human-readable message (JSON-unescaped) is searched first by parse_reset_time

# line 416
[2026-05-21T16:05:05+00:00 / 2026-05-21T11:05:05-0500] fix -- limit detection: IANA time pattern is definitive signal
  Root cause: limit errors are not always 429s; the IANA time pattern
  ("7:20am (America/Chicago)") is the reliable signal regardless of exit code.
  autobuilderclaude.py:
    - run_claude: replaced single `if proc.returncode != 0 and _RATE_LIMIT_RE`
      check with `is_limit` flag: _RESET_TIME_IANA_RE match triggers regardless
      of exit code; _RATE_LIMIT_RE remains fallback requiring non-zero exit

# line 415
[2026-05-21T16:12:46+00:00 / 2026-05-21T11:12:46-0500] Remove bash time wrapper
  autobuilderclaude.py:
    - Removed bash time wrapper (`bash -c 'time "$@"'`); cmd set directly to claude_cmd
    - stderr still captured separately (renamed time_raw -> err); included in combined
      for rate-limit pattern detection
    - Removed time printing block (real/user/sys lines)
    - Updated header comment and run_claude docstring
  README.md:
    - Removed "Bash time output" section
    - Added this activity log entry

# line 427
[2026-05-21T16:23:30+00:00 / 2026-05-21T11:23:30-0500] Elapsed time format change
  autobuilderclaude.py:
    - run_claude output line now shows both formats: (426.7s, 7m6.700s, exit 0)
    - Seconds form retained for quick scan; minutes form added for readability

# line 432
[2026-05-21T16:26:33+00:00 / 2026-05-21T11:26:33-0500] Add --start-task option
  autobuilderclaude.py:
    - Added --start-task N argument: runs tasks N through end of plan
    - Mutually exclusive with --task; errors if no tasks >= N exist
    - Header usage comment updated
  README.md:
    - Added this activity log entry

# line 442
[2026-05-21T16:28:04+00:00 / 2026-05-21T11:28:04-0500] v1.3.1 -> v1.4.1
  autobuilderclaude.py:
    - Version string updated to v1.4.1 (header comment and argparse description)
  README.md:
    - Version in intro description corrected (bash time ref removed; elapsed time description updated)
    - --start-task row added to Options table
    - Added this activity log entry

# line 451
[2026-05-21T16:31:10+00:00 / 2026-05-21T11:31:10-0500] Rename legacy autobuilder -> autobuilderclaude format refs
  autobuilderclaude.py:
    - Header: "autobuilder format v1" -> "autobuilderclaude format v1"
    - Header: plan/config file refs updated to autobuilderclaude_plan_template_v1.md / autobuilderclaude_config_v1.yaml
    - argparse description and epilog: same format name and file ref updates
    - Default log dir (tmp_build_logs) left unchanged per user instruction
  README.md:
    - Subtitle: "autobuilder format v1" -> "autobuilderclaude format v1"
    - Historical activity log entries (2026-04-15) left unchanged -- accurately reflect legacy tool names
    - Added this activity log entry

# line 147
[2026-05-22T07:03:39+00:00 / 2026-05-22T02:03:39-0500] feature v1.4.1 -> v1.5.0 -- --effort, --stop-after, CLI pass-through
  autobuilderclaude.py:
    - Version bump to v1.5.0
    - build_arg_parser: added --effort LEVEL (choices: low|medium|high|xhigh|max)
    - build_arg_parser: added --stop-after N (stops after task N; suppresses verification)
    - main: parse_known_args() instead of parse_args(); unrecognized flags collected as extra_args
    - main: builds extra_claude_args = [--effort LEVEL if set] + extra_args (pass-through)
    - main: effort resolved from --effort CLI flag first, then config effort key
    - main: --task and --stop-after declared mutually exclusive
    - main: stop-after filters selected list and sets run_verify=False
    - run_claude: extra_claude_args parameter appended to claude_cmd
    - _task_worker: extra_claude_args parameter threaded through; shown in task header if set
    - All run_claude and _task_worker call sites updated to pass extra_claude_args
    - Header usage comment updated: --effort, --stop-after, pass-through note
  autobuilderclaude_config_v1.yaml:
    - Added effort: null optional key with description
  autobuilderclaude_plan_template_v1.md:
    - Build Config YAML block: added # effort: high commented example
    - PARSER RULES keys list: added effort
  README.md:
    - Options table: added --effort LEVEL and --stop-after N rows; added pass-through note
    - Config keys table: added effort row
    - Claude invocation: updated command line to show [EXTRA_ARGS]; documented pass-through
    - Examples: added --effort, --stop-after, and pass-through examples
    - Activity log section: appended this entry

# line 536
[2026-05-30T02:19:44+00:00 / 2026-05-29T21:19:44-0500] patch v1.5.6 -> v1.5.7 -- stderr logging
  autobuilderclaude.py:
    - run_claude: if err is non-empty, writes it to task_NNN_..._stderr.txt alongside output log
    - Version bump to v1.5.7 (header comment and argparse description)
  README.md:
    - Log files section: added _stderr.txt entry with conditional note
    - Activity log section: appended this entry

# line 551
[2026-06-25T21:51:53+00:00 / 2026-06-25T16:51:53-0500] patch v1.6.0 -> v1.6.1 -- 10-item expert review fix pass
  autobuilderclaude.py:
    - Fix 1: is_limit gating changed to keyword AND (exit!=0 OR IANA-zone match), closing
      a false-positive hole while preserving the documented exit-0 catch
    - Fix 2: added DEFAULT_ALLOWED_TOOLS = ['Bash','Edit','Read','Write'] constant; both
      call sites use it. BEHAVIOR CHANGE: the bare default no longer includes
      'mcp__GhidraMCP__*' -- a caller relying on default --allowedTools now needs an
      explicit allowed_tools config key to retain MCP tool access
    - Fix 3: None-safety guard (str(value or '').strip()) applied to repo/preamble/
      log_dir and to main()'s add_dirs comprehension (4 sites total)
    - Fix 4: rewrote _parse_fields_and_body()'s field/body boundary detection to
      deterministic per-line classification, replacing a distance heuristic that could
      silently drop a stray prompt-body line
    - Fix 5: added validate_tasks(tasks) -- hard error (exit 1) on duplicate Task N
      numbers; warning-only on a non-gap-free/non-1-start numbering sequence
    - Fix 6: added a repo-directory-existence check in main() -- exits 1 with an error
      if config repo is set but is not a directory
    - Fix 7: added _MAX_RATE_LIMIT_RETRIES = 3; retry gate changed from a single retry
      (attempt == 1) to attempt <= _MAX_RATE_LIMIT_RETRIES
    - Fix 10: added warn_parallel_file_collisions(selected), called in main() at the
      start of the --parallel branch -- non-fatal warning listing any Files: entries
      declared by more than one concurrently-selected task
    - Version bump to v1.6.1 (header comment and argparse description)
  autobuilderclaude_config_v1.yaml:
    - Fix 8: added a comment above log_dir documenting the ../tmp_build_logs fallback
      (doc-only; the fallback behavior itself was unchanged, already correct)
  autobuilderclaude_plan_template_v1.md:
    - Fix 9: moved per-task Model: from a "Required:" heading to "Optional:", with a
      note that it falls back to default_model -- the template had contradicted both
      README.md and the actual code
  README.md:
    - Config keys table: added allowed_tools row documenting the default and the
      mcp__GhidraMCP__* opt-in behavior change from Fix 2
    - Parallel execution section: documented the Fix 10 file-collision warning
    - Activity log section: appended this entry

# line 605
[2026-06-25T23:34:33+00:00 / 2026-06-25T18:34:33-0500] patch v1.6.1 -> v1.6.2 -- 2nd expert review fix pass
  autobuilderclaude.py:
    - Fix 1: is_limit gating now also accepts ISO 8601, relative-offset, and date+IANA-
      zone reset patterns (not just bare time+IANA-zone) as the secondary signal that
      qualifies an exit-0 response as a real rate limit
    - Fix 2: parse_reset_time()'s 12-hour-clock+TZ-abbreviation branch now has a date-
      less fallback ("9:00 PM CDT" with no date assumes today, or tomorrow if already
      passed) -- previously returned None, which raised immediately on the first hit
      instead of retrying
    - Fix 3: added validate_config_types(config) -- hard error (exit 1) when models,
      add_dirs, or allowed_tools holds the wrong YAML type, naming the bad key and the
      type found. Also fixed the two remaining unguarded `config.get('models', {})`
      reads to `config.get('models') or {}`, since `models: null` is valid input
    - Fix 4: parallel-mode `with ThreadPoolExecutor(...) as executor:` replaced with an
      explicit try/finally so `shutdown(wait=False, cancel_futures=True)` runs on every
      exit path (success, rate limit, or any other worker exception), not just rate
      limits -- previously any other exception triggered the default
      shutdown(wait=True), blocking on all other in-flight workers first
    - Fix 5: build_prompt()'s license_file read now uses the same
      `str(x or '').strip()` guard as its repo/preamble siblings (style-only; behavior
      unchanged)
    - Version bump to v1.6.2 (header comment and argparse description)
  README.md:
    - Config keys table: added a note on validate_config_types() (Fix 3)
    - Rate-limit handling section: corrected the exit-0 gating description and
      documented the date-less TZ-abbreviation fallback (Fixes 1, 2)
    - Parallel execution section: documented the Fix 4 prompt-abort behavior for
      non-rate-limit exceptions
    - Activity log section: appended this entry

# line 640
[2026-06-26T11:26:35+00:00 / 2026-06-26T06:26:35-0500] patch v1.6.2 -> v1.6.3 -- fix rate-limit keyword regex miss on "session limit"
  autobuilderclaude.py:
    - Fix: `_RATE_LIMIT_RE`'s first alternative widened from the literal
      `hit your limit` to `hit your (?:\w+\s+)?limit`, so messages like
      "You've hit your session limit" (and similarly-worded variants, e.g.
      "weekly limit") now match the keyword gate. Previously the inserted
      word broke every alternative in the regex, so `is_limit` evaluated
      False and the task failed outright instead of sleeping to the
      already-correctly-parseable reset time.
    - Discovered when a real run of the syntorx9000 MRSS EEPROM plan's
      Task 11 hit a Claude Code session limit and exited 1 instead of
      retrying. Root cause confirmed by reading the regex directly; full
      detail in autobuilderclaude_activity.log.
    - Verified via a standalone python3 reproduction: the failing message,
      the original "hit your limit" message, "weekly limit", "usage limit",
      "rate limit", "rate-limit", "exceeded ... limit", and "limit ...
      exceeded" all match; two benign control strings ("hit your stride
      today", "time limit for this run") still do not match.
    - `python3 -m py_compile autobuilderclaude.py` succeeds after the fix.
    - Version bump to v1.6.3 (header comment and argparse description)
  README.md:
    - Rate-limit handling section: documented the recognized limit-keyword
      phrases, including the widened "hit your <word> limit" pattern
    - Activity log section: appended this entry

[2026-07-02T07:08:45+00:00 / 2026-07-02T02:08:45-0500] patch v1.6.3 -> v1.6.4 -- fix parse_reset_time() date rollover after midnight UTC
  autobuilderclaude.py:
    - Fix: changed both time-only branches in parse_reset_time() from
      `if reset_local <= now_local: reset_local += timedelta(days=1)` to
      `if reset_local < now_local - timedelta(hours=1): reset_local += timedelta(days=1)`.
      Affected branches: the 12h+TZ-abbreviation date-less path (line ~236) and
      the IANA time-only path (line ~307).
    - Root cause: when the script woke up ~10 minutes after a reset time
      (e.g., 1:40 AM CDT) and received a second rate-limit message with the
      same time ("1:30 AM CDT"), the old guard fired because 1:30 AM <= 1:40 AM,
      adding a full day and scheduling a ~24-hour sleep instead of a short
      retry. Manifested as the printed restart time showing one calendar day
      further ahead than expected, visible after midnight UTC (00:00Z) when
      the CDT local date had not yet rolled over.
    - Fix behavior: a reset time that just barely passed (within 1 hour)
      is returned as-is (past UTC time -> sleep_secs = max(0, negative) = 0 ->
      immediate retry). A reset time that passed more than 1 hour ago is
      still rolled to the next calendar day, preserving the original behavior
      for the common first-rate-limit case (e.g., rate-limited at 3:40 PM
      CDT with a "1:30 AM CDT" reset -> correctly schedules 1:30 AM the
      next day).
    - Version bump to v1.6.4 (header comment line 3, argparse description)
  README.md:
    - Activity log section: appended this entry

[2026-07-03T18:20:26+00:00 / 2026-07-03T13:20:26-0500] patch v1.6.4 -> v1.6.5 -- fix parse_reset_time() IANA-before-12H ordering
  autobuilderclaude.py:
    - Fix: moved _RESET_DATE_IANA_RE and _RESET_TIME_IANA_RE checks to run BEFORE
      _RESET_12H_RE in parse_reset_time(). For messages like "resets 4:40am
      (America/Chicago)", the 12H branch had been matching first, defaulting the
      timezone to UTC (no TZ abbreviation in that group) and scheduling a
      next-day wakeup. The IANA branch (which correctly extracts
      America/Chicago = CDT = UTC-5) now runs first; 12H is the fallback for
      messages with no IANA zone name.
    - Verified: "4:40am (America/Chicago)" now yields 9:40 AM UTC (correct);
      old code yielded 4:40 AM UTC next day (wrong).
    - `python3 -m py_compile autobuilderclaude.py` succeeds.
    - Version bump to v1.6.5 (header comment line 3, argparse description)
  README.md:
    - Activity log section: appended this entry

[2026-07-15T11:28:38+00:00 / 2026-07-15T06:28:38-0500] feature v1.6.5 -> v1.6.6 -- fatal invocation detection
  autobuilderclaude.py:
    - Added FatalInvocationError(RuntimeError) class and _FATAL_TIMEOUT_SECS = 29*60
      constant (1740 seconds)
    - run_claude(): added _long_no_output_strikes counter and _prev_was_rate_limit
      flag, both initialized before the retry while loop. After each invocation, if
      all three hold (elapsed > _FATAL_TIMEOUT_SECS, returncode != 0,
      text_output.strip() == ""), the counter increments. Strike 1 prints a WARNING
      and continues; strike 2 raises FatalInvocationError with reason "second
      occurrence". If _prev_was_rate_limit is True at strike 1, FatalInvocationError
      is raised immediately with reason "previous attempt was a rate-limit hit".
      _prev_was_rate_limit is set True in the rate-limit retry branch before continue.
    - Three catch sites updated in main(): serial task loop, parallel executor
      loop, and verification block each catch FatalInvocationError, print the
      message to stderr, and call sys.exit(1).
    - `python3 -m py_compile autobuilderclaude.py` succeeds.
    - Version bump to v1.6.6 (header comment line 3, argparse description)
  README.md:
    - Intro: added sentence describing fatal invocation detection
    - Added "Fatal invocation detection" section (before Log files)
    - Activity log section: appended this entry

[2026-07-15T12:12:30+00:00 / 2026-07-15T07:12:30-0500] patch v1.6.6 -> v1.6.7 -- expert review bug fixes (s374)
  autobuilderclaude.py:
    - FatalInvocationError docstring: corrected ">20 min" to ">29 min" (matched _FATAL_TIMEOUT_SECS)
    - _EXEC_WINDOW_RE: widened IANA zone group from `[A-Za-z_]+/[A-Za-z_]+` to
      `[A-Za-z_]+(?:/[A-Za-z_]+)+` to match 3-component zones (America/Indiana/Indianapolis etc.)
    - Added _chunked_sleep(sleep_secs, wake_dt, label, _out) helper; replaced duplicated
      chunked-sleep loops in wait_for_exec_window and run_claude rate-limit branch with calls to it
    - wait_for_exec_window: restructured to loop and re-verify window is open after each sleep,
      guarding against clock skew and DST transitions
    - load_config_file: added try/except OSError with clean ERROR print + sys.exit(1), matching
      load_models_file behavior
    - merge_configs docstring: documented that list-type keys (add_dirs, allowed_tools) are
      replaced in full; only models is deep-merged
    - _parse_fields_and_body: added warning to stderr when a line matches `\w+:\s*\S` but is
      not a recognized field (Model|Files|Effort|ExecWindow) -- indicates typo in plan header
    - build_prompt: added _license_header_cache module-level dict; license file now read once
      per path per process, not once per task invocation
    - proc.communicate(): added KNOWN ISSUE comment explaining no-timeout is intentional;
      _FATAL_TIMEOUT_SECS only fires post-communicate(); claude CLI manages its own lifecycle
    - executor.shutdown: wrapped cancel_futures=True in try/except TypeError for Python 3.8 compat
    - Version bump to v1.6.7 (header comment, argparse description)
  README.md:
    - Activity log section: appended this entry

# line 797
[2026-07-25T19:40:02+00:00 / 2026-07-25T14:40:02-0500] feature v1.6.7 -> v1.6.8 -- day-of-week exec_window filter
  autobuilderclaude.py:
    - Added _DAY_NAMES dict: maps 2- and 3-letter day abbreviations (Mo/Mon..Su/Sun)
      to Python weekday() ints (Mon=0, Sun=6)
    - Added _DAY_PREFIX_RE: matches optional "DAY[,DAY...] " prefix before HH:MM-HH:MM
    - parse_exec_windows: strips day prefix via _DAY_PREFIX_RE; parses day set into
      frozenset; adds 'days' key to each window dict (None = all days)
    - _in_exec_window: redesigned -- full-day (start==end), non-wrapping, and wrapping
      cases all check day filter; wrapping case checks prev_weekday for closing side
    - _next_exec_window_start: added day-advance loop (max 7 steps) after computing
      initial candidate; guarantees landing on an allowed weekday
    - Header comment: updated exec_window format to "[DAY[,DAY...] ]HH:MM-HH:MM [TZ]"
    - Version bump to v1.6.8 (header comment, argparse description)
  autobuilderclaude_plan_template_v1.md: updated ExecWindow: docs + examples
  autobuilderclaude_config_v1.yaml: added day-of-week exec_window comment examples
  README.md: added "Execution windows" section; added exec_window row to Config keys table
  New spec format: "[DAY[,DAY...] ]HH:MM-HH:MM [TZ]"
  Examples: "Sa,Su 00:00-00:00 America/Chicago"; "Tu,Th 18:00-06:00 America/Chicago"

[2026-07-25T19:54:17+00:00 / 2026-07-25T14:54:17-0500] verify v1.6.8 complete -- 8 unit tests PASSED; py_compile OK; --list dry-run PASSED (plan_0007 all tasks show unrestricted)
