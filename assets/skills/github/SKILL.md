---
name: github
description: Use when the user wants to manage a GitHub repository workflow through git and gh, including local branch and sync operations, pull requests, issues, and CI runs.
argument-hint: <repo|branch|pr|issue|request>
allowed-tools: Bash
---

# github

Use `git` for local repository management and `gh` for GitHub operations.

## When to use

Use this skill when the user wants to:
- inspect or manage branches
- commit, push, pull, merge, or rebase
- create, inspect, or merge pull requests
- inspect or manage issues
- inspect CI / workflow runs
- move between local git state and remote GitHub collaboration cleanly

## When not to use

Do NOT use this skill for:
- non-GitHub remotes unless the user explicitly wants raw git only
- editing code itself
- destructive history rewriting unless the user explicitly requests it
- browser-only GitHub UI flows that cannot be done with `gh`

## Core operating rules

1. Start by checking repository state before making changes:
   - `git status --short`
   - `git branch -vv`
   - `git remote -v`
2. For GitHub operations, prefer `gh` over browser fetches.
3. For local sync operations, prefer safe commands and avoid destructive shortcuts.
4. Never force-push, hard reset, or discard local work unless the user explicitly asks.
5. Before actions visible to others, such as pushing, creating PRs, merging PRs, or commenting on issues, confirm unless the user has already clearly asked for that exact action.
6. If authentication may be missing, check `gh auth status`.
7. If not inside the target repo, use `--repo owner/repo` explicitly.

## Local project management

### Inspect state
```bash
git status --short
git branch -vv
git remote -v
```

### Branching
```bash
git checkout -b feature/name
git switch master
git branch -vv
```

### Commit flow
```bash
git add path/to/file
git commit -m "type: message"
```

### Sync
```bash
git pull
git push -u origin branch-name
```

### Integration
```bash
git merge branch-name
git rebase master
```

## Remote GitHub operations

### Pull requests
```bash
gh pr list --repo owner/repo
gh pr view 55 --repo owner/repo
gh pr checks 55 --repo owner/repo
gh pr create --title "feat: ..." --body "..."
gh pr merge 55 --squash --repo owner/repo
gh pr comment 55 --body "..."
```

### Issues
```bash
gh issue list --repo owner/repo --state open
gh issue view 42 --repo owner/repo
gh issue create --title "Bug: ..." --body "..."
gh issue comment 42 --body "..."
gh issue close 42 --repo owner/repo
```

### CI / workflow runs
```bash
gh run list --repo owner/repo --limit 10
gh run view <run-id> --repo owner/repo
gh run view <run-id> --repo owner/repo --log-failed
gh run rerun <run-id> --failed --repo owner/repo
```

## Typical workflows

### From local changes to PR
1. Check status and branch
2. Stage specific files
3. Commit
4. Push branch
5. Create PR with `gh pr create`

### Sync branch with mainline
1. Check worktree cleanliness
2. Pull latest default branch
3. Merge or rebase as requested
4. Re-run checks if needed
5. Push updated branch if requested

### Review PR readiness
1. `gh pr view`
2. `gh pr checks`
3. Inspect branch status if local repo context matters
4. Summarize merge blockers clearly

## Output style

- Prefer short summaries over raw command dumps
- Use structured `gh --json` / `--jq` when it improves clarity
- Show exact next action when blocked
- Separate local-state problems from remote GitHub problems

## Notes

- Use `--repo owner/repo` when not in a git directory.
- PR and issue URLs can often be passed directly to `gh`.
- Treat `push`, `merge`, `rebase`, and PR merge as higher-impact operations.
