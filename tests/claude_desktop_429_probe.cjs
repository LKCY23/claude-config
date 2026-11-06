// Optional source evidence only: no Electron launch or proprietary bodies here.
// Extract small checksum-pinned routines into an in-memory context on Mac mini.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');
const path = require('node:path').posix;
const source = fs.readFileSync(process.argv[2], 'utf8');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
assert.equal(crypto.createHash('sha256').update(source).digest('hex'),
  '92e8f69fb04f151845edc56f4246a0d3a2d2f91b311cfdb26e4f64edc43819e0');
function slice(start, end) {
  const a = source.indexOf(start);
  assert.ok(a >= 0 && source.indexOf(start, a + 1) < 0, 'Unique source start: ' + start);
  const b = source.indexOf(end, a + start.length);
  assert.ok(b > a, 'Source end: ' + end);
  return source.slice(a, b);
}
const store = JSON.parse(fixture.files['config.json'] || '{}'), writes = [];
const quiet = () => {};
const context = vm.createContext({
  u: { nativeTheme: { themeSource: 'system' }, app: { getPath: () => '/fixture/Claude-3p' } },
  Do: { get: key => store[key], set: (key, value) => { store[key] = value; writes.push([key, value]); } },
  N: { warn: quiet, error: quiet, info: quiet, debug: quiet },
  n: { ...path, default: path }, __dirname: '/fixture/Claude.app/Contents/Resources/app.asar/.vite/build',
});
const run = code => vm.runInContext(code, context, { timeout: 1000 });
run(slice('function pQe(', 'var hf='));
run('var theme = {' + slice('setThemeMode(e){', ',setIncognitoMode(') + '};');
const startup = slice('var p9a=Do.get(', ';var m9a=');
for (const value of ['system', 'light', 'dark']) {
  store.userThemeMode = value; run(startup); assert.equal(context.u.nativeTheme.themeSource, value);
  const count = writes.length; run('theme.setThemeMode(' + JSON.stringify(value) + ')');
  assert.equal(writes.length, count, 'Unchanged theme does not write');
  const next = value === 'dark' ? 'light' : 'dark';
  run('theme.setThemeMode(' + JSON.stringify(next) + ')');
  assert.equal(store.userThemeMode, next); assert.equal(context.u.nativeTheme.themeSource, next);
}
for (const value of [undefined, 'invalid', true]) {
  store.userThemeMode = value; context.u.nativeTheme.themeSource = 'system'; run(startup);
  assert.equal(context.u.nativeTheme.themeSource, 'system');
}
run(slice('function GCt()', 'function KCt()'));
assert.equal(run('GCt()'), '/fixture/Claude.app/Contents/Resources/app.asar/resources/i18n');
Object.assign(context, {
  KCt: () => fixture.catalogs,
  QCt: locale => { assert.ok(fixture.catalogs.includes(locale + '.json')); return { locale, messages: {} }; },
  YCt: () => 'en-US', // OS locale selection remains outside this in-memory probe.
  D_e: class { constructor(value) { this.value = value; } next(value) { this.value = value; } },
});
run('var _v="en-US", vv, HCt, UCt, WCt;');
run(slice('var XCt=', 'function QCt('));
run(slice('function $Ct(', 'function X(){'));
store.locale = 'fr-FR'; run('twt()'); assert.equal(run('vv.locale'), 'fr-FR');
assert.equal(run('ewt("en-US")'), true); assert.equal(store.locale, 'en-US');
assert.equal(run('ewt("../invalid")'), false);
assert.equal(run('ewt("fr-FR")'), true);
assert.equal(run('ewt("zz-ZZ")'), true); assert.equal(store.locale, 'en-US', 'Absent catalog falls back');
const localeWrites = writes.length;
assert.equal(run('ewt("en-US")'), true); assert.equal(writes.length, localeWrites);
if (fixture.catalogs.includes('zh-Hans.json') && !fixture.catalogs.includes('zh-CN.json')) {
  assert.equal(run('ewt("zh-Hans")'), true); assert.equal(store.locale, 'zh-Hans');
  assert.equal(run('ewt("zh-CN")'), true); assert.equal(store.locale, 'en-US', 'No implicit Chinese locale alias');
}

// Static type/scope declarations, not mocked schema validation as proof.
for (const key of ['chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled']) {
  const marker = 'flatKey:"' + key + '"', at = source.indexOf(marker);
  assert.ok(at > 0 && source.indexOf(marker, at + 1) < 0);
  assert.ok(source.slice(at - 18, at).endsWith('U(O().optional(),{'));
  assert.ok(source.slice(at, at + 160).includes('scopes:["3p"]'));
}
assert.ok(slice('oIe=A({enabled:', 'defaultDomainPolicy:').includes('default:!1'));
run('var ' + slice('pNe=e=>(', ';function mNe(') + ';');
run(slice('function vNe(', 'function fl('));
run(slice('function aFe(', 'function oFe('));
run('var ' + slice('nIe=e=>', ';function rIe(') + ';');
assert.equal(run('vNe({}).chat'), false);
assert.equal(run('vNe({chatSurface:{enabled:true}}).chat'), true);
assert.equal(run('aFe({}).code'), true); assert.equal(run('aFe({}).cowork'), false);
assert.equal(run('aFe({workspace:{autoModeEnabled:false}}).code'), false);
assert.equal(run('aFe({workspace:{autoModeEnabled:true}}).cowork'), true);
assert.equal(run('nIe({})'), false); assert.equal(run('nIe({builtinBrowser:{enabled:true}})'), true);

const files = new Map(Object.entries(fixture.files));
Object.assign(context, {
  ju: '-3p', cWe: 'claude_desktop_config.json', process: { platform: 'darwin', env: {} },
  a: { readFileSync: filename => {
    const name = path.relative('/fixture/Claude-3p', filename);
    if (!files.has(name)) throw Object.assign(new Error('Missing fixture'), { code: 'ENOENT' });
    return files.get(name);
  } }, Dd: quiet,
});
run(slice('function Pu(', 'function uWe('));
run(slice('function Fu(', 'function xWe('));
run(slice('function rd(', 'function SKe('));
run('var bKe="deploymentMode", nd=null;');
run(slice('function DJe(', 'function kJe('));
assert.equal(run('Lu()'), '/fixture/Claude-3p/configLibrary');
assert.equal(run('Fu()'), '/fixture/Claude-3p/claude_desktop_config.json');
for (const [key, value] of Object.entries(fixture.settings)) {
  if (key === 'userThemeMode' || key === 'locale') {
    store[key] = JSON.parse(fixture.files['config.json'])[key];
    assert.equal(run('pQe(' + JSON.stringify(key) + ')'), value);
  } else {
    assert.equal(run('rd()'), '3p');
    assert.equal(run('OJe(DJe())[' + JSON.stringify(key) + ']'), value);
  }
}
// Source precedence only. No cryptographic/key-store operation is invoked;
// supplied fixture feature entries contain no sealed credentials.
Object.assign(context, { AHe: value => value?.bootstrapUrl, ql: value => value, Hl: () => [],
  dd: {}, gu: 'darwin', kd: new Set(), hJe: new Set(), Od: quiet, lJe: quiet,
  vJe: quiet, mPe: value => value, Ul: () => false, lLe: () => false,
  uJe: () => quiet, Ed: { local: new Set() } });
run(slice('function kJe(', 'function AJe('));
run(slice('function jJe()', 'var MJe='));
const uid = '11111111-2222-3333-4444-555555555555';
files.set('configLibrary/_meta.json', JSON.stringify({ appliedId: uid }));
files.set('configLibrary/' + uid + '.json', JSON.stringify({ chatTabEnabled: false }));
assert.equal(run('jJe().chatTabEnabled'), false);
files.set('configLibrary/_meta.json', JSON.stringify({ appliedId: uid, hybridPointer: { bootstrapUrl: 'https://example.invalid/' } }));
assert.equal(run('jJe().bootstrapUrl'), 'https://example.invalid/');
assert.equal(run('jJe().chatTabEnabled'), undefined, 'Hybrid selection bypasses local appliedId');
files.set('configLibrary/_meta.json', JSON.stringify({ appliedId: '../escape' }));
assert.equal(run('OJe(DJe())'), undefined);
assert.ok(slice('async function lV(', 'var Rir=').includes('aV(e,{...t,...await Lir(e,t)})'));
assert.ok(slice('async function aV(', 'async function oV(').includes('mi(Ru(e),'));
console.log('PASS: pinned 4.29 theme/locale, packed catalog path, feature readers and local/hybrid selection; no GUI');
