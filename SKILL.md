---
name: claude-config
description: Manage explicit Agent and App Profiles using the installed claude-config CLI.
---

Use `claude-config`; `--machine` defaults to `~/.claude-config-tool/machine.yaml`.
Specify `--agent claude-code|codex` or `--app claude-desktop|codex-app` explicitly.
Run plan/validate, inspect the human summary and saved JSON, then apply with --expect-plan-id.
Use --format text for static output or --format json for agents. --no-input is removed.

Read [commands](references/new-agent-profile.md), [packaging](references/cli-packaging-and-distribution.md),
[conflicts](references/resource-conflict-resolution.md), and [combined configuration](references/framework-cutover.md) as needed.
Tool installation and upgrades belong to uv; normal configuration operations never install tool code.
For GUI agents with a restricted PATH, init can install a Skill with the stable absolute command entry.

Claudespace owns resource bodies; Profiles select portable settings/resources and supported runtime options;
Machine supplies local dependencies. Preserve authentication, providers, projects, conversations and trust.
Never infer selections from caches, publish configuration, execute resource business tasks or grant trust as a side effect.
Legacy declarations are inputs to the explicit migration converter only, never runtime fallbacks.
