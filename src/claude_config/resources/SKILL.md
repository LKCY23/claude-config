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
`doctor --resources --processes --storage` inspects ownership, drift, App instances and retention metadata.
Use `doctor --no-save` for diagnostics without a saved report. Files and settings do not prove native discovery.
`retire-skills --resource REF` previews removal of an unselected, unchanged owned standalone skill;
execution requires `--write --expect-plan-id`. Disabling or deselecting alone does not uninstall it.

Only declared portable leaves and selected resources synchronize. Omitted fields preserve local state.
Claudespace owns fixed resource bodies; Profiles select resources and supported runtime options; Machine supplies local dependencies.
Preserve authentication, providers, projects, conversations and native trust. Never infer resource selections from caches.
Do not publish configuration, run resource business tasks, grant Hook trust or switch providers as a side effect.
Conflicts and drift require explicit resolution. Rollback uses saved journals and preserves later edits.
Hand off overlapping external skill-manager claims and review duplicate legacy entries before deployment.


## Unified plugin management (0.8.0+)

For a Claude Code plugin, prefer `plugin install|upgrade|enable|disable REF --agent claude-code`.
`plugin list|status` are read-only. Short names must be unambiguous.
Interactive commands preview and confirm once. Noninteractive calls preview by default;
use `--write --expect-plan-id` after inspecting the complete report. `--dry-run` may prepare source caches only.
Upgrade preserves enablement unless `--enable`; install can use `--disabled`.
Use `--version` for an exact published release or `--commit` for an explicit full source SHA.
Do not infer latest, change repository identity, or directly toggle native state behind the Profile.
Per-Profile `.plugins.lock.yaml` pins are used by ordinary plan/apply as well.
Preserve existing data; report session activation separately. Do not restart applications without authorization.
