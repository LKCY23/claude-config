# Desktop compatibility automation with subscription Codex

The GitHub repository is connected to Codex. The connector completed a review on PR #6; repair delegation still requires a human's explicit PR comment. This automation uses no OpenAI API key and stores no ChatGPT/Codex login tokens in Actions.

## Workflow

`Watch Claude Desktop` runs every six hours (minute 17 UTC) or manually. GitHub scheduling may be delayed. It queries the official stable universal macOS Squirrel feed, downloads the exact archive, checks the upstream SHA-256, bundle identity, version and Anthropic signer, then checks the framework interface. Known interfaces also undergo isolated native preference apply/export/rollback tests.

The job does not install or start Desktop, log in, access user configuration or connect to a user's Mac. Temporary downloads are deleted; only JSON reports and test logs become seven-day workflow artifacts. No third-party application code is committed or uploaded as an artifact.

- `compatible`: currently reviewed interface and isolated checks pass. No PR is needed.
- `needs-review`: signature differs; compatibility is unknown. A separate limited-write job creates one **draft report PR** for that version. Existing open or closed PRs are not overwritten or repeatedly reopened.
- Download, integrity, signature or test errors fail the workflow. They do not become compatibility approvals or automated patches.

The monitor repeats checks on schedule, but deduplicates PRs; it has no installer cache or background daemon. An interrupted publication that leaves a branch but no PR fails safely and requires inspection rather than force-pushing over that branch.

## Human subscription handoff

Copy the `@codex` instruction from the draft PR into a **new PR comment**, or paste it into a Codex task with the PR context. GitHub's connected Codex can then prepare the adaptation using `.github/codex/desktop-compatibility.md`. The workflow does not post a bot-authored delegation comment or claim such comments activate subscription tasks.

Codex code review and Codex repair delegation are different actions: automatic review comments confirm the review integration, not that a repair has run. Cloud repair environments may lack macOS; the dedicated macOS workflow remains the platform check.

## Verify a proposed fix

`Desktop compatibility` runs on PRs changing a pinned report. It downloads **that report's exact release**, not whatever is newest at rerun time. Unknown interfaces or failed native smoke tests cause failure. The initial report-only PR is expected to fail this check until adapted.

For bot-created PRs, GitHub may require approval for workflows or may not automatically trigger them. Manually dispatch `Desktop compatibility` with the PR branch and the report version if needed:

```sh
gh workflow run desktop-compatibility.yml --ref compatibility/claude-desktop-3.6556.2 -f version=3.6556.2
```

Require both `Package and verify CLI` and `Desktop compatibility` to pass before merging an adaptation. This is a documented release gate; this change does not alter repository-wide branch protections. Do not merge a report-only PR as the fix. After review and merge, use the existing version-tag release workflow. Machine adoption stays separate.

## Sources and limits

The feed shape was verified on 2026-10-23 at `https://api.anthropic.com/api/desktop/darwin/universal/squirrel/update`. Its parameters and host come from the installed Desktop updater; it is not a promised public API. A generated CI-only `device_id` satisfies the feed request without collecting a real device identifier. Feed changes fail visibly.

The signature check still uses reviewed modules. This workflow supplies evidence and a repair path; it does not yet implement a semantic replacement for whole-module signatures. Scope reduction requires separate design and independent tests, not automatic acceptance of changed modules. GUI, provider behavior and organization policy remain outside the unsigned-in CI checks.

Official references: [subscription GitHub integration](https://developers.openai.com/codex/integrations/github/), [GitHub scheduling](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [workflow PR permission](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).
