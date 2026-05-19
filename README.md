# autobuilderclaude

Document-driven Claude task runner (autobuilder format v1).

https://github.com/ke4ahr/autobuilderclaude

Reads an implementation plan written in Markdown, extracts tasks, and
executes each one by piping the task prompt to `claude` via the CLI.
Tasks may run sequentially or concurrently. All prompts and responses
are captured to timestamped log files. Token usage is reported per task
and as a run total.

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

Token counts are printed after each task on the output line:

```
  output  -> /path/to/1997-07-16T19:20:30+00:00_output.txt  (4.2s, exit 0)  tokens: in=1234 out=567 cache_read=890 cache_write=0
```

A cumulative total is printed at the end of the run:

```
Done.  total tokens: in=5432 out=2109 cache_read=1780 cache_write=0
```

Fields: `in` = input tokens, `out` = output tokens, `cache_read` = tokens
read from prompt cache, `cache_write` = tokens written to prompt cache.

## Rate-limit handling

If claude exits non-zero and its output contains a usage-rate-limit message
(`"hit your limit"`), the run aborts immediately with:

```
ERROR: rate limit reached -- <message excerpt>
Remaining tasks skipped.
```

No further tasks are dispatched. The reset time is included in the message
excerpt taken directly from the claude CLI output.

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
