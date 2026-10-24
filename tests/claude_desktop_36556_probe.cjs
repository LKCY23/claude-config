// Reproducible source evidence, not an Electron/GUI launch. No proprietary
// application bodies are committed: slices come from the checksum-pinned ZIP.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');
const path = require('node:path').posix;
const source = fs.readFileSync(process.argv[2], 'utf8');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
assert.equal(crypto.createHash('sha256').update(source).digest('hex'),
  '6e898798943b990e6eefa9209c61d977866b7898f681864f02c8cad3bb691ed5');

function slice(start, end) {
  const a = source.indexOf(start);
  assert.ok(a >= 0 && source.indexOf(start, a + 1) < 0, `Unique start: ${start}`);
  const b = source.indexOf(end, a + start.length);
  assert.ok(b > a, `End: ${end}`);
  return source.slice(a, b);
}
const store = JSON.parse(fixture.files['config.json'] || '{}');
const writes = [];
const quiet = () => {};
const context = vm.createContext({
  a: {nativeTheme: {themeSource: 'system'}, app: {getPath: () => '/fixture/Claude-3p'}},
  wo: {get: key => store[key], set: (key, value) => {store[key] = value; writes.push([key, value]);}},
  N: {warn: quiet, error: quiet, info: quiet, debug: quiet},
});
const run = code => vm.runInContext(code, context, {timeout: 1000});
run(slice('function JYe(', 'var Sf='));
run('var theme = {' + slice('setThemeMode(e){', ',setIncognitoMode(') + '};');
const startup = slice('var qWa=wo.get(', ';var JWa=');
for (const value of ['system', 'light', 'dark']) {
  store.userThemeMode = value;
  run(startup);
  assert.equal(context.a.nativeTheme.themeSource, value);
  run(`theme.setThemeMode(${JSON.stringify(value)})`);
  const count = writes.length;
  run(`theme.setThemeMode(${JSON.stringify(value)})`);
  assert.equal(writes.length, count, 'Unchanged theme does not rewrite the store');
  const next = value === 'dark' ? 'light' : 'dark';
  run(`theme.setThemeMode(${JSON.stringify(next)})`);
  assert.equal(store.userThemeMode, next);
  assert.equal(context.a.nativeTheme.themeSource, next);
}
for (const invalid of [undefined, 'invalid', true]) {
  store.userThemeMode = invalid;
  context.a.nativeTheme.themeSource = 'system';
  run(startup);
  assert.equal(context.a.nativeTheme.themeSource, 'system');
}
Object.assign(context, {
  Tbt: () => fixture.catalogs,
  jbt: locale => {
    assert.ok(fixture.catalogs.includes(locale + '.json'));
    return {locale, messages: {}};
  },
  Obt: () => 'en-US', // OS language selection is outside this isolated test.
  t_e: class {constructor(value) {this.value = value;} next(value) {this.value = value;}},
});
run('var I_="en-US", L_, Sbt, Cbt, xbt;');
run(slice('var kbt=', 'function jbt('));
run(slice('function Mbt(', 'function X(){'));
store.locale = 'fr-FR';
run('Pbt()');
assert.equal(run('L_.locale'), 'fr-FR', 'Startup reads the stored catalog');
assert.equal(run('Nbt("en-US")'), true);
assert.equal(store.locale, 'en-US');
assert.equal(run('Nbt("../invalid")'), false);
assert.equal(store.locale, 'en-US');
assert.equal(run('Nbt("fr-FR")'), true);
assert.equal(run('Nbt("zz-ZZ")'), true);
assert.equal(store.locale, 'en-US', 'Missing catalog falls back, not a successful zz-ZZ write');
const count = writes.length;
assert.equal(run('Nbt("en-US")'), true);
assert.equal(writes.length, count);

// The schema declarations themselves prove optional Boolean type and 3p scope;
// only native readers below are executed. Schema validation is not mocked as proof.
for (const key of ['chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled']) {
  const offset = source.indexOf(`flatKey:"${key}"`);
  assert.ok(offset > 0);
  assert.ok(source.slice(offset - 18, offset).endsWith('U(O().optional(),{'));
  assert.ok(source.slice(offset, offset + 150).includes('scopes:["3p"]'));
}
assert.ok(slice('vFe=A({enabled:', 'defaultDomainPolicy:').includes('default:!1'));
run(slice('function zMe(', 'function Ic('));
run(slice('function mPe(', 'function hPe('));
assert.equal(run('zMe({}).chat'), false);
assert.equal(run('zMe({chatSurface:{enabled:true}}).chat'), true);
assert.equal(run('mPe({}).code'), true);
assert.equal(run('mPe({}).cowork'), false);
assert.equal(run('mPe({workspace:{autoModeEnabled:false}}).code'), false);
assert.equal(run('mPe({workspace:{autoModeEnabled:true}}).cowork'), true);

const files = new Map(Object.entries(fixture.files));
Object.assign(context, {
  n: path, Eu: '-3p', Au: 'claude_desktop_config.json',
  process: {platform: 'darwin', env: {}},
  o: {readFileSync: filename => {
    const relative = path.relative('/fixture/Claude-3p', filename);
    if (!files.has(relative)) throw Object.assign(new Error('Missing fixture'), {code: 'ENOENT'});
    return files.get(relative);
  }},
  Pd: quiet,
});
run(slice('function ju(', 'function zHe('));
run(slice('function Pu(', 'function YHe('));
run(slice('function xKe(', 'function CKe('));
run(slice('function sd(', 'function FWe('));
run('var NWe="deploymentMode", od=null;');
assert.equal(run('Fu()'), '/fixture/Claude-3p/configLibrary');
assert.equal(run('Pu()'), '/fixture/Claude-3p/claude_desktop_config.json');
for (const [key, value] of Object.entries(fixture.settings)) {
  if (key === 'userThemeMode' || key === 'locale') {
    store[key] = JSON.parse(fixture.files['config.json'])[key];
    assert.equal(run(`JYe(${JSON.stringify(key)})`), value, 'Native reader sees adapter output');
  } else {
    assert.equal(run('sd()'), '3p');
    assert.equal(run(`SKe(xKe())[${JSON.stringify(key)}]`), value, 'Applied UUID reader sees adapter output');
  }
}
// Isolate source-selection precedence: URL validation and configuration schema
// are separate dependencies, not evidence of full policy or account behavior.
Object.assign(context, {
  mu: value => value?.bootstrapUrl, Dl: value => value, wl: () => [], vd: {},
  Id: new Set(), lKe: new Set(), Fd: quiet, fKe: quiet, Uc: value => value,
  rKe: quiet, iKe: quiet, Nd: {local: new Set()},
});
run(slice('function CKe(', 'var EKe='));
const uid = '11111111-2222-3333-4444-555555555555';
files.set('configLibrary/_meta.json', JSON.stringify({appliedId: uid}));
files.set(`configLibrary/${uid}.json`, JSON.stringify({chatTabEnabled: false}));
assert.equal(run('TKe().chatTabEnabled'), false);
files.set('configLibrary/_meta.json', JSON.stringify({appliedId: uid, hybridPointer: {bootstrapUrl: 'https://example.invalid/'}}));
assert.equal(run('TKe().bootstrapUrl'), 'https://example.invalid/');
assert.equal(run('TKe().chatTabEnabled'), undefined, 'Hybrid source bypasses appliedId');
files.set('configLibrary/_meta.json', JSON.stringify({appliedId: '../escape'}));
assert.equal(run('SKe(xKe())'), undefined);
console.log('PASS: pinned native theme/locale routines, feature schema/defaults, applied UUID readback and hybrid precedence');
