// Execute only checksum-pinned Claude Desktop 4.23118.0 routines with synthetic stores.
// No Electron launch, user data, GUI, account, or key-store access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');
const path = require('node:path').posix;

const source = fs.readFileSync(process.argv[2], 'utf8');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
assert.equal(crypto.createHash('sha256').update(source).digest('hex'),
  '1ffb394eb87f77ec1e8949c4acb8c30e973fcb6897987133f46444560dcd3df1');

function slice(start, end) {
  const a = source.indexOf(start);
  assert.ok(a >= 0 && source.indexOf(start, a + 1) < 0, 'Unique source start: ' + start);
  const b = source.indexOf(end, a + start.length);
  assert.ok(b > a, 'Source end: ' + end);
  return source.slice(a, b);
}
function functionSlice(name, next) {
  return slice('function ' + name + '(', 'function ' + next + '(');
}

const store = JSON.parse(fixture.files['config.json'] || '{}');
const writes = [];
const files = new Map(Object.entries(fixture.files));
const quiet = () => {};
const context = vm.createContext({
  n: { ...path, default: path },
  u: { nativeTheme: { themeSource: 'system' } },
  Co: { get: key => store[key], set: (key, value) => { store[key] = value; writes.push([key, value]); } },
  b0e: key => store[key],
  ku: () => '/fixture/Claude-3p',
  process: { platform: 'darwin', env: {} },
  a: { readFileSync: filename => {
    const name = path.relative('/fixture/Claude-3p', filename);
    if (!files.has(name)) throw Object.assign(new Error('Missing fixture: ' + name), { code: 'ENOENT' });
    return files.get(name);
  } },
  h: { readFile: async filename => {
    const name = path.relative('/fixture/Claude-3p', filename);
    if (!files.has(name)) throw Object.assign(new Error('Missing fixture: ' + name), { code: 'ENOENT' });
    return files.get(name);
  } },
  $r: fn => fn(),
  P: { warn: quiet, error: quiet, info: quiet, debug: quiet },
  f: value => String(value),
  pze: () => undefined,
  n5n: () => false,
  hl: value => value,
  fl: value => value,
  X8n: { runExclusive: fn => fn() },
  di: async () => {},
  ri: async () => {},
});
const run = code => vm.runInContext(code, context, { timeout: 1000 });

;(async () => {
// Theme startup and setter: only accepted values change Electron theme state;
// unchanged values do not write back to the config store.
run('var theme = {' + slice('setThemeMode(e){', ',setIncognitoMode(') + '};');
const startup = slice('var zPo=Co.get("userThemeMode")', 'var BPo=');
for (const value of ['system', 'light', 'dark']) {
  store.userThemeMode = value;
  run(startup);
  assert.equal(context.u.nativeTheme.themeSource, value);
  const count = writes.length;
  run('theme.setThemeMode(' + JSON.stringify(value) + ')');
  assert.equal(writes.length, count, 'Unchanged theme does not write');
  const next = value === 'dark' ? 'light' : 'dark';
  run('theme.setThemeMode(' + JSON.stringify(next) + ')');
  assert.equal(store.userThemeMode, next);
  assert.equal(context.u.nativeTheme.themeSource, next);
}
for (const value of [undefined, 'invalid', true]) {
  store.userThemeMode = value;
  context.u.nativeTheme.themeSource = 'system';
  run(startup);
  assert.equal(context.u.nativeTheme.themeSource, 'system');
}

// The five managed third-party fields and desktopHome are declared in the
// exact source module with explicit 3p scope and source-side surface grants.
for (const key of ['chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled']) {
  const marker = 'flatKey:"' + key + '"';
  const at = source.indexOf(marker);
  assert.ok(at > 0 && source.indexOf(marker, at + 1) < 0);
  assert.ok(source.slice(at, at + 220).includes('scopes:["3p"]'));
}
const home = slice('desktopHome:W(M(["standard","simple","no-vm","off"]).optional()', '},category:');
assert.ok(home.includes('flatKey:"desktopHome"'));
assert.ok(home.includes('optionValues:["standard","off"]'));
assert.ok(home.includes('surfaceGrants:{standard:{chat:!0,fileAnalysis:!0,cowork:!0}'));
assert.ok(home.includes('off:{chat:!1,fileAnalysis:!1,cowork:!1}'));
assert.ok(source.includes('aNe="2026-11-10"'));

// Read the local config-library metadata and UUID entry through the exact
// source path helpers. Sealed values are preserved by the editor read path.
run('function Mu(){return n.join(ku(),"configLibrary")}');
run('function Nu(e){return n.join(Mu(), `${e}.json`)}');
run('function Pu(){return n.join(Mu(),"_meta.json")}');
run(functionSlice('Z8n', 'Q8n'));
run('function t5n(e){try{let t=JSON.parse(e);return typeof t==="object"&&t&&!Array.isArray(t)?t:void 0}catch{return}}');
run('async ' + functionSlice('r5n', 'i5n'));
const uid = '11111111-2222-3333-4444-555555555555';
files.set('configLibrary/_meta.json', JSON.stringify({ appliedId: uid }));
files.set('configLibrary/' + uid + '.json', JSON.stringify({
  chatTabEnabled: true,
  autoModeEnabled: false,
  builtinBrowserEnabled: true,
  desktopHome: 'standard',
  inferenceGatewayApiKey: 'sealed:v1:KEEP',
}));
const meta = run('Z8n()');
assert.equal(meta.appliedId, uid);
const entry = await run('r5n(' + JSON.stringify(uid) + ', {})');
assert.equal(entry.chatTabEnabled, true);
assert.equal(entry.autoModeEnabled, false);
assert.equal(entry.builtinBrowserEnabled, true);
assert.equal(entry.desktopHome, 'standard');
assert.equal(entry.inferenceGatewayApiKey, 'sealed:v1:KEEP');
files.set('configLibrary/_meta.json', JSON.stringify({ appliedId: uid, hybridPointer: { bootstrapUrl: 'https://example.invalid/' } }));
assert.equal(run('Z8n()').hybridPointer.bootstrapUrl, 'https://example.invalid/');

console.log('PASS: pinned 4.23118.0 theme, fields, desktopHome grants, config-library metadata and sealed-value read path; no GUI/key-store');
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
