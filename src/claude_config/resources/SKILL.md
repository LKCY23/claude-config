---
name: claude-config
description: Manage explicit Agent and App Profiles with the installed claude-config command.
---

# Claude Config

Command: `{command}`
Machine: `{machine}`
Use `--agent {agent}` explicitly; App preferences require `--app codex-app` or `--app claude-desktop` instead.

Run `plan` or `validate`, inspect the human summary and saved JSON, then `apply --expect-plan-id <reviewed-id>`.
Use `--format text` for noninteractive human output or `--format json` for structured output.
`export`, `rollback`, `adopt-baseline`, and `review-conflicts` preview by default; `--write` executes.
`switch` applies a selected Profile; preview with `--dry-run`. Inspect each command's `--help` before use.
`cutover` combines multiple clients' configuration writes; software upgrades use the package manager separately.

Only declared portable leaves and selected resources synchronize. Omitted fields preserve local state.
Claudespace owns fixed resource bodies; Profiles select resources and supported runtime options; Machine supplies local dependencies.
Preserve authentication, providers, projects, conversations and native trust. Never infer resource selections from caches.
Do not publish configuration, run resource business tasks, grant Hook trust or switch providers as a side effect.
Conflicts and drift require explicit resolution. Rollback uses saved journals and preserves later edits.
