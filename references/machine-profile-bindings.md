# Repository Machine Profile selections

The configuration data repository stores one `machines/<machine_id>.yaml` per
machine. This file is the authoritative Profile selection after the local
Machine explicitly adopts it. It contains no executable paths, credentials,
provider configuration, runtime state or native preferences.

```yaml
schema_version: 1
machine_id: mac-mini-third-party
agents:
  claude-code: profiles/agents/claude-code/third-party.yaml
apps:
  claude-desktop: profiles/apps/claude-desktop/current.yaml
```

The local `~/.claude-config-tool/machine.yaml` keeps `machine_id`, `config_dir`,
`catalog_roots`, runtime bindings and dependency references. Add:

```yaml
profile_selection: machines/mac-mini-third-party.yaml
agents:
  claude-code:
    binding:
      executable: /Users/example/.local/bin/claude
      config_root: /Users/example/.claude
apps:
  claude-desktop:
    binding:
      application: /Applications/Claude.app
      data_root: /Users/example/Library/Application Support/Claude-3p
```

This is a fragment; retain the other required local Machine fields. File paths
inside the repository are relative to `config_dir`, never the current directory.
The selection identity and filename must match the local `machine_id`. Selection
client keys and local runtime binding keys must match exactly. A selection cannot
supply runtime bindings or secrets, escape the repository, or refer to another
client type. A missing or malformed selection is an error, with no fallback.

An existing Machine without `profile_selection` keeps its explicit local choices.
A referenced Machine must not also contain per-client `profile` fields: there is
one authoritative choice, not two competing lists. Creating a repository record
alone does not adopt it or change an existing machine.

## Adopt current choices

First create the repository selection with exactly the machine's current choices.
Then preview:

```sh
claude-config init --profile-selection machines/mac-mini-third-party.yaml --format text
```

Review its saved report, then execute the identical command with `--write` and
`--expect-plan-id <reviewed-id>`. Init refuses different choices, so adopting the
file cannot silently switch native configuration. It updates only the local
Machine representation. Ordinary `init` recovery restores its prior contents.

## Read, switch and recover

`plan`, `validate`, `apply`, `export` and `doctor` resolve the explicit repository
selection. Reports identify its file. `switch --profile ...` uses the normal
checked plan and updates the selected key in the repository file, preserving all
other clients and the local Machine's paths. It does not commit or push Git.
Both the selection and the local Machine are plan inputs; later modifications
invalidate execution. Normal recovery restores the selection and native changes
from the same operation, refusing to overwrite later edits.

`adopt-baseline` changes only catalog pins and local prepared roots, preserving
selection ownership. `cutover` validates and copies machine selection files along
with Profiles while retaining the relative selection reference at the target.

## Git update boundary

Authenticate GitHub on each machine with that user's normal account. Clone the
private configuration repository, reconcile existing portable files, and retain
local `.state/` and dependency directories. Do not copy authentication material
from another device or replace working native configuration with the repository.

Use `git pull --ff-only`, inspect the diff, prepare any newly selected catalog
sources, then run the selected target's `plan` and reviewed `apply`. Pulling Git
does not deploy anything. A `switch` leaves a visible Git change that must be
reviewed and committed before another device can obtain it. Git conflicts and
uncommitted local changes require reconciliation, not reset/clean automation.
Software/Bootstrap upgrades use their releases and remain separate from Profile
synchronization. No scheduler or unattended native write is created by adoption.
