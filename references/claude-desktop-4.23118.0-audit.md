# Claude Desktop 4.23118.0 preference interface audit

Reviewed 2026-11-07 from the exact pinned official archive.

- Bundle: `com.anthropic.claudefordesktop`.
- Archive SHA-256: `5b8feee3da048e26bbb29a5d601e97034f674ac3b76f7d0237751c46d6034b23`.
- Reviewed module: `.vite/build/index.chunk-BBUaTQ8d.js`.
- Module SHA-256: `1ffb394eb87f77ec1e8949c4acb8c30e973fcb6897987133f46444560dcd3df1`.

The independent probe covers:

- `userThemeMode` startup and changed/unchanged writes;
- the `chatTabEnabled`, `autoModeEnabled`, and `builtinBrowserEnabled` 3p declarations;
- `desktopHome` native values, public options, surface grants, and the 2026-11-10 deprecation clock;
- `configLibrary` path resolution, applied UUID metadata, hybrid pointer preservation, selected feature values, and sealed-value preservation.

The probe runs only extracted, checksum-pinned source with synthetic files. It never launches Electron, reads user data, uses a key store, or claims GUI/provider behavior. The compatibility report remains source-scoped and does not claim account, GUI, provider, or business validation.
