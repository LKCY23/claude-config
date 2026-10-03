# Native Claude Code and Codex profiles

The historical name claude-config now covers two native hosts. It is a deployment
framework, not a translator between their instructions, permissions or hooks.
This implementation is installed and tested on the current Mac. Existing legacy
platform workflows are preserved, with no new Linux/Windows compatibility claim.

## Ownership and precedence

- claudespace owns upstream pins, packaging revisions and candidate maturity.
- agents.yaml in claude-config-data owns explicit selections and native overlays.
- scripts/agent_config.py owns validation, deployment, status and scoped rollback.
- Installed config.yaml owns machine paths and the selected Python interpreter.
- The native host owns authentication, OAuth tokens, sessions, hook trust, caches
  and all configuration keys not explicitly selected.

When agents.yaml exists, profiles take precedence over legacy plugins.yaml and
manifest.yaml for helper deployment. Do not merge legacy plugin selections into
profiles. Legacy Git synchronization and external-tool setup remain separate
workflows in legacy-claude.md. An explicit --agent wins; the bootstrap entry binds
the default host; a standalone helper invocation defaults to Claude.

## Prepare and bootstrap

Use Python 3.10+ with requirements.txt installed. Prepare a durable catalog clone
with git submodule update --init --recursive. Catalog source preparation, native
registration and plugin installation are separate operations. Apply never pulls
Git, updates pins, creates project docs or logs into a service.

Commit the reviewed local framework before bootstrap:

```sh
python scripts/bootstrap.py plan --source ~/claude-config --config-dir ~/claude-config-data --catalog-root ~/claudespace --agent both
python scripts/bootstrap.py apply --source ~/claude-config --config-dir ~/claude-config-data --catalog-root ~/claudespace --agent both
```

The installed tool records framework_source, framework_commit, config_dir,
python, catalog_roots, state_dir and backup_dir in ignored config.yaml. Claude
gets a pointer in ~/.claude/skills/claude-config; Codex gets a pointer in
~/.agents/skills/claude-config (an existing legacy ~/.codex/skills entry is reused).
Duplicate entries and linked/dirty managed sources are reported before bootstrap.
The pointer reads canonical installed instructions rather than duplicating them.
update-self uses the recorded local source and bootstrap; it must not pull an
unrelated remote branch over the reviewed source. A private bootstrap receipt
supports scripts/bootstrap.py rollback --receipt <path> after plugin rollback.

## Declaration

```yaml
version: 1
catalogs:
  claudespace-candidates:
    repo: LKCY23/claudespace
    path: domains/candidates
agents:
  claude:
    plugins: [code-review@claudespace-candidates]
    config: {source: assets/claude/preferences.json}
  codex:
    plugins: [code-review@claudespace-candidates]
    config: {source: assets/codex/preferences.toml}
    # instructions: {source: assets/codex/AGENTS.md}
    # files: [{source: assets/codex/agents/reviewer.toml, target: agents/reviewer.toml}]
    # mcp_servers: {example: {command: example-server, args: [serve]}}
```

All paths are repository-relative and cannot escape their root. Machine-local
catalog_roots maps repo names to durable checkouts. Registration exposes choices;
only plugin@catalog selectors install. Existing different catalog roots, installed
versions or cache contents are conflicts. Reconcile through the native host after
reviewing the source update; the helper currently does not perform automatic
version upgrades or restore arbitrary older plugin versions.

## Commands and configuration semantics

Use the interpreter recorded by bootstrap with the installed helper:

```sh
python scripts/agent_config.py validate --agent both
python scripts/agent_config.py plan --agent both
python scripts/agent_config.py apply --agent both
python scripts/agent_config.py status --agent codex
python scripts/agent_config.py rollback --agent both --run-id <id> --dry-run
```

plan and apply --dry-run make no writes. validate also checks effective native
inventory. diff reports differing managed paths/keys without printing runtime
values. export --output <file> projects only the key shape already selected in
config.source, not entire native configuration. Never select credential values;
secret-bearing key names are rejected, but this is not a general secret scanner.

Claude settings.json objects and Codex config.toml tables merge recursively.
Unselected values stay local. Empty overlays are exact no-ops. Other arrays
replace explicitly supplied keys, except Claude permissions allow/ask/deny and
both hosts' hook-event arrays merge uniquely; Codex skills.config merges by path.
Plugin/marketplace state uses native selection commands, not raw overlays. MCP
registration uses mcp_servers declarations, not raw native config overlays.

Optional instructions own one claude-config:begin/end block, preserving surrounding
text. Explicit files may target agents/, rules/, hooks/ and Codex hooks.json only.
Claude agent Markdown and Codex agent TOML get native required-field checks;
JSON/TOML files are parsed. This is basic format validation, not complete native
schema validation. Executable bits and native hook trust are not automatically set.

Managed hooks currently support a conservative set of documented native events
and command-handler structures. Unknown events/handler types are refused. Codex
hooks.json merges existing events; Claude hooks belong in its settings overlay.
Validation checks configuration shape and timeout limits, not a hook program's
stdin/stdout protocol or side effects. Native /hooks review remains the authority
for trust; status of optional hooks is configuration-only until tested in use.
See [Codex hooks](https://developers.openai.com/codex/hooks) and
[Claude hooks](https://code.claude.com/docs/en/hooks).

Optional MCP entries use native stdio or URL registration. Definitions cannot
contain headers, credential values or arbitrary env mappings; Codex may reference
a bearer_token_env_var name. Registration and connection/authentication are
separate: status reports registered and connection_verified=false. Existing
unmanaged definitions are never adopted or overwritten. Login remains native.
No MCP or hook is selected by the initial three-plugin deployment.

Memory, UI preferences and external tools have distinct consumers. The initial
profiles do not migrate automatic memory, Desktop caches, sessions or runtime
folders. Existing Claude Desktop deployment remains its legacy explicit path.
Only selected stable preferences, instructions or files may be added later.

## Verification, retry and recovery

Preflight checks the full selected host set before writes, including source
pins/original file hashes and existing native cache identity. Runtime file
symlinks are refused. Modified configs get 0600 backups under the ignored local
backup_dir. Operations are recorded before execution in private 0700 directories
with 0600 state/runs/<UTC-id>.json files. These never belong in the data repo.

Apply is sequential across native hosts, not a two-host atomic transaction. On
failure the helper reports its journal id. Inspect status and retry the affected
host; matching installed/enabled plugins are skipped. A successful repeat with
no changes returns run_id=null.

rollback --run-id restores only that run's managed changes: newly installed
plugins and registrations, former enabled state, changed config leaves,
instruction block and explicit files. It preserves unrelated later edits, and
stops if a managed key/file/plugin has since changed. Marketplace rollback refuses
removal when other installed plugins now depend on it. New MCP rollback checks
its observed native fingerprint. Bootstrap rollback is separate from deployment.

## Test scope and acceptance

Default repository checks cover configuration management, source integrity,
packaging and native installation. Run the deterministic tests without model
calls or subagents:

```sh
python -m unittest discover -s tests -v
```

Validate profiles through the existing helper. On a machine with the native CLIs,
use explicit isolated runtime directories for installation/repeat/rollback tests;
normal CI does not require OAuth or inference access. For an existing live install,
validate/status are read-only checks; do not reinstall plugins solely to retest.
Report native inventory, resolvable package entries and actual process discovery
as separate evidence when the host lacks a non-inference discovery interface.

Original Karpathy, code-review and setup business tasks are not deployment
acceptance gates. Do not run upstream skills, create project setup documents,
Agent Teams or additional worktrees as part of ordinary apply or validation.
Skill quality comparisons belong to a separately requested evaluation.

The added Codex Code Simplifier wrapper gets package-path checks and one bounded
manual dispatch check on first integration or a material wrapper change. That
check verifies one independent executor and hand-back, not simplification quality.
Reuse applicable existing evidence and record its model/config conditions. Missing
runtime evidence stays unverified; do not replace it with a prompt-text assertion
or a mock of a dispatcher that does not exist. An own-package path or dispatch
defect still blocks acceptance of that adapter.

Report management checks, native integration, wrapper structure, manual dispatch
and usage observations separately. Upstream-call timeouts and pre-existing model
availability errors stay visible as observations; they are not automatically
framework failures. Candidate quality remains not-evaluated until a separate
evaluation or actual usage supports promotion. Matt's original setup helper is
installed as a dependency; run it explicitly inside a chosen project when needed.
