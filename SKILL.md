---
name: claude-config
description: Manage declared Claude Code and Codex plugins and native configuration. Use to preview, validate, apply, inspect, export or roll back this configuration system, or maintain its source catalogs.
---

# Claude Config

Use the installed framework as the canonical source. claudespace owns sources,
exact versions and candidate maturity; claude-config-data selects plugins and
native settings independently for each host. Registration does not install or
validate every candidate. Auth, OAuth tokens, sessions and local trust remain
with the native host. Existing connection settings stay unchanged unless the
user explicitly selects a connection change.

## Routing

Determine the host from the user's --agent or the installed bootstrap entry's
bound host. Without either, legacy commands target Claude. Never guess that a
Codex request should run Claude static-config phases.

When agents.yaml exists, use scripts/agent_config.py for plan, validate, apply,
status, diff, export and rollback. The installed config.yaml records config_dir,
Python interpreter and durable catalog roots. Pass --agent explicitly. Read
[dual-agent deployment](references/dual-agent.md) for schema, ownership and
recovery behavior. Use the native plugin installers, not cache-file editing.

For apply, validate the complete selected plan before writes. Do not silently
merge plugins.yaml into profiles, install all candidates, adopt a different
model/provider, run project setup, or trust hooks. Existing version/content drift
is a preflight conflict: preserve the installed version until the user requests
an explicit native source update. A failed run reports its journal id; status
and retry handle remaining work, and rollback --run-id restores its owned changes.

Export only selected configuration keys. Permission lists use native merge
semantics; unmanaged fields are preserved. Optional files deploy only explicitly
named role, rule and hook files, not runtime folders or authentication files.

## Bootstrap and source updates

scripts/bootstrap.py plan/apply installs a committed local framework into the
Mac tool directory and creates host-bound entry pointers. It records a private
receipt for scripts/bootstrap.py rollback. The source checkout stays canonical;
the data bootstrap file is only a pointer. Don't maintain two instruction copies.

For update-self, use the source recorded in installed config.yaml and the same
bootstrap procedure; do not git-pull an unrelated default branch over the reviewed
local source. Updates never imply selecting additional plugins or upgrading pins.

## Existing synchronization and legacy setup

For Git data sync, remote management, initialization and external-tool setup,
read only the relevant section of [legacy Claude workflows](references/legacy-claude.md).
Those existing workflows remain available. sync --apply first synchronizes the
data and then invokes the native helper for the selected host. If no agents.yaml
exists, the original Claude-only workflow applies; skip legacy plugin entries
with enabled: false. Do not reinterpret Claude permissions, memory or hooks as
Codex configuration. This run targets the current Mac; other machine deployment
requires a separately specified target.
