---
name: claude-config
description: Manage Agent and App Profiles, sync selected resources or settings fields, and inspect deployment conflicts with claude-config.
---

# Claude Config

Command: `{command}`
Machine: `{machine}`
Use `--agent {agent}` for this client's Agent settings and resources. Use `--app codex-app` or `--app claude-desktop` for application preferences.

## Choose the sync scope

Scoped deployment requires claude-config 0.7.0+. Check `--version`; inspect subcommand `--help` for complete arguments.

| Request | Selector for `plan`, `validate`, and `apply` |
|---|---|
| One selected skill or plugin | `--resource REF@CATALOG` |
| All selected resources in a group | `--resource-group skills` or `--resource-group plugins` |
| All selected resources | `--scope resources` |
| All declared Agent settings | `--scope settings` |
| A declared settings leaf | `--field /settings/model` |
| A declared App preference | `--app claude-desktop --field /settings/userThemeMode` |
| The complete bound Profile | Omit selectors, or use `--scope all`. |

Choose one selector family: `--scope`, `--resource-group`, `--resource`, or `--field`. Repeat the latter three to select multiple entries within that family. App targets accept settings scopes and fields. Resources must already be selected in the Profile; fields must be declared leaves or command bindings.

## Preview and apply

For an individual resource request, select it explicitly. For example:

```sh
{command} plan --agent {agent} --resource agent-session-migration@claudespace-personal --format text
{command} apply --agent {agent} --resource agent-session-migration@claudespace-personal --expect-plan-id <reviewed-id> --format text
```

Read the human summary and saved JSON before applying. Use the same target, selectors, policies and resolutions for preview and execution. Partial `apply` requires `--expect-plan-id`; a zero-difference plan needs no file update.

Review `deployment_scope.effective`, `expanded`, and `expansion_reasons`: required resources, command bindings and native control fields may join the requested scope. Keep out-of-scope state unchanged. For `scope_expansion_required`, include the affected consumer with its corrected reference and preview again.

On `plan_stale`, inspect which input changed and generate a fresh scoped preview. Handle drift using the report's explicit resolutions. Request a client exit only when the relevant operation reports a concrete lifecycle or in-use blocker. Report file synchronization separately from `activation`; existing conversations may need to re-invoke a skill, reload a component, or start a new session.

## Other operations

- `fetch-baseline` prepares an explicit published catalog version; `adopt-baseline` selects it; then scoped `plan/apply` deploys the requested resources.
- `export`, `rollback`, `adopt-baseline`, and `review-conflicts` preview by default; `--write` executes. For `export`, `--field` and `--resource` select what is saved back to the Profile.
- `switch --profile ... --dry-run` previews a complete Profile switch. `cutover` combines client plans. Software upgrades use the package manager separately.
- `doctor --resources --processes --storage` inspects ownership, drift, App instances and retention; `--no-save` avoids saving a diagnostic report.
- `retire-skills --resource REF@CATALOG` previews removal of an unselected, unchanged owned standalone skill; execute with `--write --expect-plan-id`.

Claudespace owns fixed resource bodies; Profiles select resources and runtime options; Machine supplies local dependencies. Preserve authentication, providers, projects, conversations and native trust. Resolve external skill-manager ownership before deployment. Rollback uses saved journals and preserves later edits.

## Unified plugin management (0.8.0+)

For a Claude Code plugin, prefer `plugin install|upgrade|enable|disable REF --agent claude-code`.
`plugin list|status` are read-only. Short names must be unambiguous.
Interactive commands preview and confirm once. Noninteractive calls preview by default;
In 0.8.0.dev2+, `--write` generates, validates and executes a fresh plan; `--expect-plan-id` is optional for a previously reviewed plan. `--dry-run` may prepare source caches only.
Upgrade preserves enablement unless `--enable`; install can use `--disabled`.
Use `--version` for an exact published release or `--commit` for an explicit full source SHA.
Do not infer latest, change repository identity, or directly toggle native state behind the Profile.
Per-Profile `.plugins.lock.yaml` pins are used by ordinary plan/apply as well.
Preserve existing data; report session activation separately. Do not restart applications without authorization.
