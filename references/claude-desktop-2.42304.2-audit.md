# Claude Desktop 2.42304.2 preference interface audit

Reviewed 2026-10-23 for the Mac mini migration. This adds a reviewed source signature; no synchronization fields or write policy change.

- Bundle: `com.anthropic.claudefordesktop`.
- Source: `.vite/build/index.chunk-BkhOOUzl.js`.
- SHA-256: `07d785c56978eb335c933562a514b3eba79cc5f307b4cd46f7f84e2dca5c83ec`.
- `userThemeMode` and `locale` still use user-data `config.json`. Theme startup accepts system/light/dark. Locale changes reject malformed input and fall back to en-US when a catalog is absent.
- `chatTabEnabled`, `autoModeEnabled`, `builtinBrowserEnabled` remain optional boolean third-party fields. Local configuration resolves `configLibrary/_meta.json.appliedId` to the selected UUID JSON. Managed/remote policy still has precedence; file checks do not prove GUI availability.
- Prior 2.42304.0 and 2.26454.2 signatures remain supported. Unknown content is still blocked.

Validation: extracted theme/locale routines ran in memory with no native writes; the installed-archive smoke tests passed for official appearance and third-party feature settings, including temporary apply/export/rollback; five audit regression tests passed. Codex backend smoke was explicitly skipped because this change is Claude-only.

Local evidence: `/Users/liyao/claude-migration-20261023/interface-audit/`. Proprietary extracted application code stays outside Git. The migration candidate wheel is labelled `0.3.1` (local candidate; Bootstrap accepts stable-format versions only), distinct from published `0.3.0`; it is not a new formal release.
