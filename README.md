# autobuilderclaude

Document-driven Claude task runner (autobuilder format v1).

https://github.com/ke4ahr/autobuilderclaude

Reads an implementation plan written in Markdown, extracts tasks, and
executes each one by piping the task prompt to `claude` via the CLI.
All prompts and responses are captured to timestamped log files.
Token usage is reported per task and as a run total.

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

Tasks are executed in numeric order. The optional `## Verification`
section runs after all tasks (or alone with `--task verify`).

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
log_dir:       /absolute/path/to/logs
license_file:  /absolute/path/to/LICENSE_HEADER.txt
default_model: sonnet
models:
  haiku:  claude-haiku-4-5-20251001
  sonnet: claude-sonnet-4-6
  opus:   claude-opus-4-6
```

`autobuilder_config_v1.yaml` is a template with placeholder values.
Copy it, fill in real paths, and pass it via `--template` or `--config`.

### Config keys

| Key | Description |
|-----|-------------|
| `repo` | Absolute path to the project root. Passed to claude as `Working directory:` and via `--add-dir`. |
| `log_dir` | Directory for log files. Created if absent. Default: `../tmp_build_logs` relative to the plan file. |
| `license_file` | Path to a plain-text license header. Injected verbatim into every prompt. Set to `null` to skip. |
| `default_model` | Model alias used when a task has no `Model:` field. Default: `sonnet`. |
| `models` | Dict mapping `haiku`/`sonnet`/`opus` aliases to full model IDs. |

## Claude invocation

Each task runs:

```
claude --model MODEL -p --output-format json --allowedTools Edit Write --add-dir REPO < prompt
```

`--allowedTools Edit Write` permits claude to write files without
interactive permission prompts. `--add-dir REPO` grants file access to
the repo directory. JSON output format is used to capture token usage.

## Token usage

Token counts are printed after each task on the output line:

```
  output  -> /path/to/output.txt  (4.2s, exit 0)  tokens: in=1234 out=567 cache_read=890 cache_write=0
```

A cumulative total is printed at the end of the run:

```
Done.  total tokens: in=5432 out=2109 cache_read=1780 cache_write=0
```

Fields: `in` = input tokens, `out` = output tokens, `cache_read` = tokens
read from prompt cache, `cache_write` = tokens written to prompt cache.

## Log files

Each run creates a timestamped subdirectory under `log_dir`:

```
{log_dir}/{plan_stem}_{YYYY-MM-DDTHHMMZ}/
  task_001_{title}_{HHMMSSZ}_prompt.txt
  task_001_{title}_{HHMMSSZ}_output.txt
  task_002_...
  verify_{HHMMSSZ}_prompt.txt
  verify_{HHMMSSZ}_output.txt
```

Output files contain the text response only (JSON envelope stripped).

## Examples

Run a single task using only the plan's embedded config:

```
autobuilderclaude --input docs/plan.md --task 1
```

Run all tasks with a shared template for defaults:

```
autobuilderclaude --input docs/plan.md --template autobuilder_config_v1.yaml
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

Copyright (C) 2026 Kris Kirby, KE4AHR
This file is distributed under the GPLv3.0. 
