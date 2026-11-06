# Claude Desktop 4.23118.0 source audit

Reviewed 2026-11-06 from the exact pinned official archive in PR #22.

- Bundle: `com.anthropic.claudefordesktop`.
- Archive SHA-256: `5b8feee3da048e26bbb29a5d601e97034f674ac3b76f7d0237751c46d6034b23`.
- Candidate preference module: `.vite/build/index.chunk-BBUaTQ8d.js`.
- Candidate module SHA-256: `1ffb394eb87f77ec1e8949c4acb8c30e973fcb6897987133f46444560dcd3df1`.

Static source evidence confirms:

- `userThemeMode` is read from and written to the user `config.json` store; startup accepts `system`, `light`, and `dark`.
- Third-party feature declarations include `chatTabEnabled`, `autoModeEnabled`, and `builtinBrowserEnabled`.
- The local configuration library uses `configLibrary/_meta.json`, UUID entry files, and a `hybridPointer`; entry reads preserve sealed values.
- `desktopHome` accepts the native values `standard`, `simple`, `no-vm`, and `off`, while portable declarations expose `standard` and `off`. The source defines surface grants for Chat, file analysis, and Cowork and applies a deprecation clock beginning 2026-11-10.

This is source evidence only. The existing 4.x executable probe does not yet cover this module's renamed reader, writer, locale, and surface-policy routines. The 4.23118.0 interface therefore remains **needs-review** and must not be registered as compatible until an independent probe and temporary-file round-trip cover those routines.
