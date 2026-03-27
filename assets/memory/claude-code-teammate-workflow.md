---
name: claude-code-teammate-workflow
description: User prefers a disciplined Claude Code teammate workflow: create real tasks, assign owners, control whether agents stay visible via progress messages, and send explicit start messages instead of only spawning agents.
type: feedback
---
Use a disciplined Claude Code teammate workflow: do not treat spawning an agent as sufficient. **Why:** the user encountered multiple teammate panes showing idle/blank agents because agents were started without a real task/owner/message chain, stale team state made the situation confusing, and visibility into teammate progress was inconsistent. **How to apply:** when using teammates, first ensure the team state is valid, then create tasks, assign each task to a specific teammate, set status to in_progress, and send an explicit message telling that teammate to start and report back. If the user wants the teammate to stay more in the foreground, explicitly require an immediate progress update and, when useful, staged status messages; UI visibility increases when the teammate sends messages back. If the user prefers the teammate to stay in the background, ask for only a final result unless a blocker appears. Judge success by task ownership and active work, not by the mere presence of agent panes.

