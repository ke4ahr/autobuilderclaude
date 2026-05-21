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
+ 10 minutes) and retries the failing task automatically.

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
autobuilderclaude --input PLAN [--template TEMPLATE] [--config CONFIG] [OPTIONS]
```

### Options

| Option | Description |
|--------|-------------|
| `--input PLAN` | Implementation plan .md file (required) |
| `--template TEMPLATE` | YAML file providing base defaults; overridden by the plan's Build Config |
| `--config CONFIG` | YAML file overriding both the template and the plan's Build Config |
| `--task N` | Run only task N (integer) or `verify` |
| `--start-task N` | Start at task N and run through all remaining tasks |
| `--model MODEL` | Override per-task model for all tasks (`haiku`, `sonnet`, `opus`, or full model ID) |
| `--parallel N` | Number of tasks to run concurrently (default: 1) |
| `--dry-run` | Print resolved prompts without calling claude |
| `--list` | List all tasks with resolved models, then exit |

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
| `default_model` | Model alias used when a task has no `Model:` field. Default: `sonnet`. |
| `models` | Dict mapping `haiku`/`sonnet`/`opus` aliases to full model IDs. |

## Claude invocation

Each task runs:

```
claude --model MODEL -p --output-format json --allowedTools Edit Write --add-dir REPO [--add-dir DIR ...] < prompt
```

`--allowedTools Edit Write` permits claude to write files without
interactive permission prompts. `--add-dir REPO` grants file access to
the repo directory. Each entry in `add_dirs` adds another `--add-dir`
flag. JSON output format is used to capture token usage.

## Parallel execution

`--parallel N` runs up to N tasks concurrently using a thread pool.
Each task gets its own log files. Output is buffered per task and
printed as a complete block when the task finishes, so blocks do not
interleave.

The verification step always runs sequentially after all tasks complete,
regardless of `--parallel`.

Use `--parallel` for independent tasks (e.g. separate library files).
Avoid it for tasks with ordering dependencies.

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

## Rate-limit handling

When claude exits non-zero and its output contains a usage-rate-limit message,
the script attempts to extract a reset time from the message. Three formats are
recognized: ISO 8601 (e.g. `2026-05-22T14:00:00Z`), 12-hour clock with TZ
abbreviation (e.g. `9:00 PM CDT on Thursday, May 22, 2026`), and relative
offsets (e.g. `retry after 60 seconds`).

If a reset time is found, the script sleeps until (reset_time + 10 minutes)
and then retries the failing task automatically:

```
  Rate limit -- resets 2026-05-22T14:00:00+00:00; sleeping 3612s (wake 2026-05-22T15:00:12+00:00)
  Retrying ...
```

If the retry succeeds, processing continues with the next task normally.

If no reset time is found in the message, or if the retry also hits a rate
limit, the run aborts immediately:

```
ERROR: rate limit reached -- <message excerpt>
Remaining tasks skipped.
```

## Log files

Each run creates a timestamped subdirectory under `log_dir`:

```
{log_dir}/{plan_stem}_{YYYY-MM-DDThh:mm:ss+00:00}/
  task_001_{title}_{hh:mm:ss+00:00}_prompt.txt
  task_001_{title}_{hh:mm:ss+00:00}_output.txt
  task_1_YYYY-MM-DD_completed.txt   (exit 0)
  task_1_YYYY-MM-DD_failed.txt      (exit non-zero)
  task_002_...
  verify_{hh:mm:ss+00:00}_prompt.txt
  verify_{hh:mm:ss+00:00}_output.txt
```

Output files contain the text response only (JSON envelope stripped).

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

Copyright (C) 2026 Kris Kirby

SPDX-License-Identifier: GPL-3.0-or-later 
# autobuilderclaude activity log
# Append-only. One entry per session or significant change.
# Format: YYYY-MM-DDThh:mm:ss+00:00 action -- detail

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
