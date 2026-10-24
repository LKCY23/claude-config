# Claude Desktop 3.6556.2 preference audit

The initial observation remains unchanged in
`compatibility/claude-desktop/3.6556.2.json`. Its official archive is 378072300
bytes with SHA-256
`5f3e36bd0c500f0e02cca84848f5caf453372117ec664153db187b372754c67a`.
The reviewed module is `.vite/build/index.chunk-jPKPzV-P.js`, SHA-256
`6e898798943b990e6eefa9209c61d977866b7898f681864f02c8cad3bb691ed5`.
The bundle identifier is `com.anthropic.claudefordesktop`.

## Five supported preferences

| Preference | Storage and type | Native read/write and absent-value behavior | Scope |
| --- | --- | --- | --- |
| `userThemeMode` | User-data `config.json`; `system`, `light`, `dark` | Startup reads `wo.get`; only the three values change `nativeTheme.themeSource`. `setThemeMode` updates the native theme and persists only a changed value. Absence leaves Electron's theme source unchanged. | Appearance for the bound user-data directory. |
| `locale` | User-data `config.json`; locale string with a bundled catalog | `Pbt` reads the stored locale; otherwise OS language selection falls back to `en-US`. `Nbt` rejects malformed names, loads the selected catalog, and persists the resolved locale. An absent catalog falls back to `en-US`; the adapter continues to block that request instead of claiming it applied. | Interface language for the bound user-data directory. |
| `chatTabEnabled` | Locally applied UUID JSON in `configLibrary`; optional Boolean | Flat field maps to `chatSurface.enabled`. `zMe` requires explicit true for Chat; `desktopHome` surface grants can override this effective result. | Existing third-party deployment only. |
| `autoModeEnabled` | Same applied UUID JSON; optional Boolean | Flat field maps to `workspace.autoModeEnabled`. `mPe` treats absence as Code allowed / Cowork disabled; false disables both, true enables both at this reader. | Existing third-party deployment only; other policy/capability gates still apply. |
| `builtinBrowserEnabled` | Same applied UUID JSON; optional Boolean | Flat field maps to `builtinBrowser.enabled`, with schema default false. The effective reader also checks policy restrictions. | Existing third-party deployment only. |

The feature schemas retain their flat keys on disk despite nested internal
representations. Native editor `uz` validates the flattened view and writes the
entry through `h7n`; it does not move these fields into `config.json`.
`ju` resolves the third-party directory (normally `Claude-3p` on macOS, or the
explicit native user-data override), and `Fu`/`Iu`/`Lu` resolve the library,
entry, and metadata. Machine bindings must name the actual native data root;
this repair does not infer, migrate, or create a deployment.

## Minimal adapter change

`xKe` reads `_meta.json` and `SKe` reads its `appliedId` UUID. However, `TKe`
selects a valid `hybridPointer` bootstrap source before `SKe`. The previous
adapter could write/export a dormant UUID entry and report it as applied.
The adapter now blocks feature apply/export whenever hybrid metadata is
present, including malformed/unknown pointer forms. It does not attempt to
validate bootstrap URLs, resolve policy, or write that pointer. Appearance
and language remain available. The metadata snapshot also prevents a pointer
introduced between plan and apply from causing a stale write.

The reviewed signature is added alongside all three earlier Claude signatures
(`2.42304.2`, `2.42304.0`, `2.26454.2`). Exact source hashes, bundle identity,
catalog existence, deployment mode, applied UUID/file checks, lifecycle guards,
snapshot guards and the initial pinned report are preserved.

## Reproducible evidence

Run from the repository on a disposable macOS runner:

```sh
.venv/bin/python scripts/desktop_compatibility.py \
  --report compatibility/claude-desktop/3.6556.2.json \
  --out /tmp/desktop-verification --require-compatible
```

The existing Desktop compatibility workflow downloads only the pinned official
archive, checks size/SHA-256, verifies the full application signature with
`codesign --verify --deep --strict`, and requires Anthropic team `Q6L2SF6YDW`.
Its native smoke tests now also run `tests/claude_desktop_36556_probe.cjs` on
the exact reviewed module and the temporary files produced by the adapter.
The probe refuses a different module hash and extracts only selected routines;
no proprietary application bodies or archive are committed.

The probe executes native theme startup/setter, locale startup/setter,
deployment-mode and applied UUID readers, and hybrid source selection in a
JavaScript context with in-memory stores. It checks feature schema type/scope
declarations and executes the Chat/Auto absent-value readers. OS language
selection, catalog formatting, schema validation and bootstrap URL validation
remain external dependencies; mocks of those dependencies are not proof of
their behavior. Temporary apply/export/rollback asserts the exported values
and byte-for-byte restoration of all native fixture files, including the
feature entry and unrelated data.

`test_profile_apps.py` covers all three feature fields, unknown hybrid pointer
forms, unaffected appearance/language, and a pointer added after planning.
Existing rejection, preservation, earlier-interface and source-tampering tests
remain in place. The Linux source probe is useful evidence but does not prove
macOS signing, native filesystem or GUI behavior; those results must be read
from macOS CI for the final PR SHA.

The cloud validation ran 643 Python tests: 586 passed and 57 optional checks
skipped. The test subprocess used `umask 022` and `env -u CODEX_HOME` so fixture
file modes and temporary bootstrap paths were independent of the cloud host.
The default cloud process settings initially caused two unrelated fixture
failures; no production checks or tests were weakened to resolve them. Both
Claude source/temporary-file smoke cases passed on Linux; the unrelated Codex
backend case was skipped. Running the hybrid regression against the previous
adapter produced failures in all nine pointer/feature combinations.

No GUI is launched. Account entitlements, managed/remote policy precedence,
live UI changes, real OS locale selection, and behavior on a user's device
remain unverified. There is no merge, release, device update or provider change.
