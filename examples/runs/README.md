# Run log examples

These files are illustrations only. They contain invented tasks, invented
phone screens and harmless invented dates; they do not match any real phone
and contain no real names, messages, accounts or device details.

The JSON fields are:

- `format_version`: record format number; supported version is `1`.
- `run_id`: unique ID used for the file and report links.
- `task`: the original task text exactly as entered.
- `start_time` / `end_time`: full local timestamps with UTC offsets.
- `status`: `completed`, `stuck`, `failed`, `max_steps` or `aborted`.
- `status_message`: the existing reason text emitted by Phil.
- `total_steps`, `total_seconds`, `total_tokens`: run totals.
- `run_type`: `ai` or `shortcut`, plus an optional AI handover step.
- `model_calls`: model name, local call time, duration and token counts.
- `steps`: time, duration, pruned screen elements, action, result and guards.
- `summary`: an AI-written or code-written summary.
- `why_stopped` / `last_action`: plain-English stop explanation.
- `keep`: whether automatic cleanup must preserve this run.
