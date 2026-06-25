<!--
SPDX-License-Identifier: GPL-3.0-or-later
Copyright (C) 2026 Kris Kirby
https://github.com/ke4ahr/autobuilderclaude

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
-->
<!--
  autobuilder_plan_template_v1.md
  Template and format specification for auto-builder implementation plans.
  Parser: auto-builder.py (format v1)

  COMPANION FILES:
    autobuilder_config_v1.yaml  -- runtime config (repo path, model aliases, log
                                   dir, license file). Passed to auto-builder.py
                                   via --config. When present, its values override
                                   any Build Config block in this plan.
                                   See autobuilder_config_v1.yaml for the template.

  INSTRUCTIONS FOR THE CLAUDE INSTANCE WRITING A PLAN:
  - Copy this file. Replace every {PLACEHOLDER} with real values.
  - Remove all HTML comments (including this block) from the final file.
  - Follow the parser rules below exactly. Field names and spacing matter.
  - Build Config is optional when a --config file is provided at runtime.
    If the plan should be self-contained (no separate config file), include it.
    If a config file will always be provided, you may omit the Build Config block.
  - Do not add extra ### headings inside the Implementation Tasks section.
    Use #### or plain paragraphs inside prompt bodies if sub-headings are needed.
  - Every prompt body MUST end with: "Confirm when done."
-->

<!--
  PARSER RULES (format v1):

  BUILD CONFIG (optional if --config file is provided)
    A "## Build Config" section contains a fenced YAML block.
    Keys: repo, log_dir, license_file, default_model, effort, allowed_tools, models (dict of aliases).
    The --config file overrides any key present in this block.
    See autobuilder_config_v1.yaml for all supported keys.

  TASK HEADINGS
    Format:   ### Task N -- short-title
    N         Positive integer. Tasks execute in ascending numeric order.
              Gap-free sequence starting at 1. No letters, no decimals.
    short-title  Plain ASCII words or hyphens. No colons, parens, or slashes.

  PER-TASK FIELDS
    Appear immediately after the ### Task heading, one per line, no blank lines
    between them.
    Optional:
      Model: haiku | sonnet | opus | <full-model-id>
              (falls back to default_model from Build Config / --config if omitted)
      Files:  path/relative/to/repo [, path2, path3]
      Effort: low | medium | high | xhigh | max
      ExecWindow: HH:MM-HH:MM [TZ] [; HH:MM-HH:MM [TZ] ...]
    "haiku", "sonnet", "opus" resolve via models aliases in Build Config or
    config file. A full model ID (e.g. nvidia/nemotron-3-super-120b-a12b:free)
    is passed directly to the claude CLI. OpenRouter models require
    ANTHROPIC_BASE_URL=https://openrouter.ai/api and ANTHROPIC_AUTH_TOKEN set
    in the environment. A command-line --model flag overrides the per-task field.
    Effort: overrides the config effort key for that task; a command-line --effort
    flag is a global override that takes precedence over all per-task Effort: fields.
    ExecWindow: restricts when this task may run. TZ is an IANA zone
    (America/Chicago) or abbreviation (CDT/CST/UTC); omitted TZ defaults to UTC.
    start>end wraps past midnight (22:00-06:00). Multiple windows separated by
    ";". The task blocks (sleeping in wait increments) until inside a window.
    Overrides the config exec_window key for that task. No command-line override
    exists for this field.

  PROMPT BODY
    Everything after the blank line that follows the per-task fields,
    up to (but not including) the next ### Task heading or a "---" rule,
    is the prompt body sent to claude.
    The builder prepends:
      - "Working directory: {repo}"          (if repo is set)
      - preamble text                        (if preamble is set in config)
      - License header block                 (if license_file is set)
    The builder appends nothing -- the plan author controls the ending.
    Convention: end every prompt body with "Confirm when done."

  VERIFICATION SECTION
    "## Verification" is a reserved heading treated as the final task.
    It must have exactly one field line immediately after the heading:
      Model: sonnet   (or haiku or opus)
    The prompt body instructs claude to run shell/sqlite commands and
    report PASS or FAIL per step with actual output quoted.

  CHAT LOG CONVENTIONS
    ALL discovered behavior must be written in prose form to the project chat log,
    immediately upon discovery. Do NOT defer to end-of-session.
    Each entry: wc-l line count marker, timestamp (UTC / local with session tag), prose description.
    Format: one finding per entry. Chat log is the primary narrative record.
    Activity log holds summaries, counts, and session-level milestones.

  ACTIVITY LOG CONVENTIONS
    Every activity log entry heading must include the session number in (sN) form.
    Session numbering starts at s1 for new projects and increments by 1 each session.
    Entry heading format:
      ## YYYY-MM-DDThh:mm:ss+00:00 / YYYY-MM-DDThh:mm:ss-0500 (sN) -- short description
    Lessons Learned, Findings, and User Contributions belong in the activity log:
      Findings: tag as "FINDING:" -- significant factual discoveries (addresses, hardware
        identification, protocol behavior, data layout, etc.).
      Lessons Learned: tag as "LL-NNN:" -- VERIFIED reusable process insights only.
        Copy each LL entry to lessons_learned_0000.md after writing it here.
      User Contributions: tag as "UC:" -- items submitted by the user that have not yet
        been verified. These stay under "User Contributions" in the activity log until
        verified. After verification, promote to LL-NNN and copy to lessons_learned_0000.md.
    Before appending: run wc -l and write the count as a bare "# line N" comment above
    the new entry. This is required format for all activity log appends.

  SECTIONS IGNORED BY PARSER
    ## Overview, ## Dependencies, ## Notes, and any other ## headings outside
    Implementation Tasks and Verification are preserved in the document but
    not executed. They may be referenced by name in prompt bodies.
-->

# {PROJECT_NAME} -- Implementation Plan v{VERSION}
<!-- auto-builder format v1 -->

## Build Config

```yaml
# Optional when --config is passed to auto-builder.py.
# If included, values here serve as defaults; --config file overrides.
repo:          /absolute/path/to/project/root
log_dir:       /absolute/path/to/tmp_build_logs
license_file:  /absolute/path/to/LICENSE_HEADER.txt
default_model: sonnet
# effort:      high   # Effort level for claude (low|medium|high|xhigh|max). Omit for default.
# exec_window: "22:00-06:00 America/Chicago"   # Default time window for all tasks; a
# task's ExecWindow: field overrides this. Omit for unrestricted (run anytime).
# models_file: load additional model IDs from a plain-text file (one per line).
# Each ID becomes a self-mapping alias usable in task Model: fields.
# Requires ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN for OpenRouter models.
# models_file: /absolute/path/to/models.txt
models:
  haiku:  claude-haiku-4-5-20251001
  sonnet: claude-sonnet-4-6
  opus:   claude-opus-4-6
  # Short alias example for an OpenRouter model:
  # nemotron: nvidia/nemotron-3-super-120b-a12b:free
```

## Overview

{One to three sentences. What does this plan build or change, and why.}

---

## {Optional background sections: schema, file layout, design decisions, etc.}

{Free-form content. The parser ignores all ## sections except Build Config
and Verification. Reference these sections by name in your prompt bodies
if the context is useful to claude.}

---

## Implementation Tasks

### Task 1 -- {short-title}
Model: haiku
Files: {lib/example.py}
# ExecWindow: 22:00-06:00 America/Chicago

{Prompt body. Describe exactly what to implement.
Include: class/function names, method signatures, argument types, return types,
behavior, edge cases, logging calls, imports.
Be explicit -- this text is sent verbatim to claude.
The builder prepends the repo path and license header instruction automatically.
Confirm when done.}

### Task 2 -- {short-title}
Model: sonnet
Files: {lib/complex.py}

{Prompt body.
Confirm when done.}

### Task 3 -- {short-title}
Model: haiku

{Prompt body for a task with no output file (e.g., git commit, config update).
Omit the Files: line when not applicable.
Confirm when done.}

<!-- Repeat ### Task N blocks as needed. N must be sequential: 1, 2, 3 ... -->

---

## Verification
Model: sonnet

{Verification prompt body.

List every step the verifier must check. For each step include:
  - The exact shell command or SQL query to run.
  - The expected output or pass criterion.
  - What a failure looks like.

Example step format:

  Step 1: ./openscraper.py --help
  Expected: usage line present, all new flags listed.
  Fail: missing flag, error exit, or traceback.

  Step 2: sqlite3 test.db "SELECT count(*) FROM scrape_sessions;"
  Expected: integer >= 1.
  Fail: error or 0.

Working directory for all commands: {repo}
Run every command, collect output, and report PASS or FAIL per step with
the actual output quoted inline. Print a final summary: N passed, M failed.}
