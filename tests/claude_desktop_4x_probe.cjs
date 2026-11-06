// Execute only checksum-pinned preference routines with synthetic memory stores.
// Never require the Electron entrypoint, read user data or call a key-store API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');
const path = require('node:path').posix;
const source = fs.readFileSync(process.argv[2], 'utf8');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const specs = {
  '926694919e5ff5d05cb804945637ffbe6ae75bdeac5e461f30e7c3d5fa043ccb': {
    version: '4.4212.0', store: 'Eo', log: 'N', read: 'xQe', readEnd: 'var Mf=', theme: 'ato',
    catalog: 'gTt', list: '_Tt', valid: 'xTt', available: 'STt', load: 'CTt', activate: 'wTt',
    setLocale: 'TTt', init: 'ETt', initEnd: 'function X(', fallback: 'F_', current: 'I_', selected: 'mTt',
    persisted: 'hTt', subject: 'pTt', subjectClass: 'J_e', best: 'bTt', formatter: 'Awt', formats: 'fTt',
    root: 'ju', rootEnd: 'function PWe(', suffix: 'Ou', modeFile: 'Mu', modeName: 'MWe',
    library: 'Pu', entry: 'Fu', metaFile: 'Iu', pathEnd: 'function GWe(', uuid: 'Nu',
    modeRead: 'ad', modeReadEnd: 'function WKe(', modeKey: 'HKe', modeCache: 'id',
    meta: 'WJe', applied: 'GJe', appliedEnd: 'function KJe(', warn: 'Nd',
    surfaceStart: 'pPe=e=>(', surfaceEnd: 'var yPe=', surfaceList: 'Wc', lookup: 'ts', surfaces: 'Gc',
    defaultRead: 'mPe', dateCheck: 'mje', auto: 'RFe', autoEnd: 'function zFe(', browser: 'jIe',
  },
  'fcc1026c3634f62ae40581c680389b807c49e85626ff9989838fa0dc931d7f1a': {
    version: '4.16336.0', store: 'bo', log: 'P', read: 'h1e', readEnd: 'var ff=', theme: 'wwo',
    catalog: 'Lkt', list: 'Rkt', valid: 'Hkt', available: 'Ukt', load: 'Wkt', activate: 'Gkt',
    setLocale: 'Kkt', init: 'qkt', initEnd: 'function Y(', fallback: '_v', current: 'vv', selected: 'Fkt',
    persisted: 'Ikt', subject: 'Pkt', subjectClass: 'xve', best: 'Vkt', formatter: 'ZOt', formats: 'Nkt',
    root: 'Tu', rootEnd: 'function ZGe(', suffix: 'Su', modeFile: 'Eu', modeName: 'YGe',
    library: 'Ou', entry: 'ku', metaFile: 'Au', pathEnd: 'function cKe(', uuid: 'Du',
    modeRead: 'Zu', modeReadEnd: 'function pJe(', modeKey: 'dJe', modeCache: 'Xu',
    meta: 'xXe', applied: 'SXe', appliedEnd: 'function CXe(', warn: 'xd',
    surfaceStart: 'XPe=e=>(', surfaceEnd: 'var nFe=', surfaceList: 'Qc', lookup: 'Qo', surfaces: '$c',
    defaultRead: 'ZPe', dateCheck: 'Rje', auto: 'RIe', autoEnd: 'function zIe(', browser: 'ELe',
  },
};
const spec = specs[crypto.createHash('sha256').update(source).digest('hex')];
assert.ok(spec, 'Unreviewed source');
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
  [spec.store]: { get: key => store[key], set: (key, value) => { store[key] = value; writes.push([key, value]); } },
  [spec.log]: { warn: quiet, error: quiet, info: quiet, debug: quiet },
  n: { ...path, default: path }, __dirname: '/fixture/Claude.app/Contents/Resources/app.asar/.vite/build',
});
const run = code => vm.runInContext(code, context, { timeout: 1000 });
run(slice('function ' + spec.read + '(', spec.readEnd));
run('var theme = {' + slice('setThemeMode(e){', ',setIncognitoMode(') + '};');
const startup = slice('var ' + spec.theme + '=' + spec.store + '.get("userThemeMode")', ';var ');
for (const value of ['system', 'light', 'dark']) {
  store.userThemeMode = value; run(startup); assert.equal(context.u.nativeTheme.themeSource, value);
  const count = writes.length; run('theme.setThemeMode(' + JSON.stringify(value) + ')');
  assert.equal(writes.length, count);
  const next = value === 'dark' ? 'light' : 'dark';
  run('theme.setThemeMode(' + JSON.stringify(next) + ')'); assert.equal(store.userThemeMode, next);
}
for (const value of [undefined, 'invalid', true]) {
  store.userThemeMode = value; context.u.nativeTheme.themeSource = 'system'; run(startup);
  assert.equal(context.u.nativeTheme.themeSource, 'system');
}
run(slice('function ' + spec.catalog + '()', 'function ' + spec.list + '()'));
assert.equal(run(spec.catalog + '()'), '/fixture/Claude.app/Contents/Resources/app.asar/resources/i18n');
Object.assign(context, {
  [spec.list]: () => fixture.catalogs,
  [spec.load]: locale => { assert.ok(fixture.catalogs.includes(locale + '.json')); return { locale, messages: {} }; },
  [spec.best]: () => 'en-US',
  [spec.subjectClass]: class { constructor(value) { this.value = value; } next(value) { this.value = value; } },
});
run(`var ${spec.fallback}="en-US", ${spec.current}, ${spec.selected}, ${spec.persisted}, ${spec.subject};`);
run(slice('var ' + spec.valid + '=', 'function ' + spec.load + '('));
run(slice('function ' + spec.activate + '(', spec.initEnd));
store.locale = 'fr-FR'; run(spec.init + '()'); assert.equal(run(spec.current + '.locale'), 'fr-FR');
for (const [input, expected] of [['en-US', 'en-US'], ['zh-Hans', 'zh-Hans'], ['zh-CN', 'en-US'], ['zz-ZZ', 'en-US']]) {
  assert.equal(run(spec.setLocale + '(' + JSON.stringify(input) + ')'), true); assert.equal(store.locale, expected);
}
assert.equal(run(spec.setLocale + '("../invalid")'), false);

for (const key of ['chatTabEnabled', 'autoModeEnabled', 'builtinBrowserEnabled']) {
  const marker = 'flatKey:"' + key + '"', at = source.indexOf(marker);
  assert.ok(at > 0 && source.indexOf(marker, at + 1) < 0);
  assert.match(source.slice(at - 20, at), /[UW]\(O\(\)\.optional\(\),\{$/);
  assert.ok(source.slice(at, at + 200).includes('scopes:["3p"]'));
}
const definition = slice(spec.version === '4.4212.0' ? 'desktopHome:U(j(' : 'desktopHome:W(M(', ',surfaceGrantsWhenUnset:');
assert.ok(definition.includes('["standard","simple","no-vm","off"]).optional()'));
assert.ok(definition.includes('optionValues:["standard","off"]'));
assert.ok(definition.includes('scopes:["3p"]'));
const grants = run('(' + definition.slice(definition.indexOf('surfaceGrants:') + 'surfaceGrants:'.length) + ')');
context[spec.surfaceList] = [{ key: 'desktopHome', grants }];
context[spec.lookup] = (values, key) => values[key];
// The real source clock computes noon Pacific, including its DST boundary.
run('var ' + (spec.version === '4.4212.0'
  ? slice('sje="2026-11-10"', 'var hje=')
  : slice('jje="2026-11-10"', 'var zje=')));
run('var ' + slice(spec.surfaceStart, spec.surfaceEnd));
for (const home of ['standard', 'off']) {
  const value = JSON.stringify({ chatSurface: { desktopHome: home, enabled: home !== 'standard' }, coworkSurface: { enabled: home !== 'standard' } });
  const actual = run(spec.surfaces + '(' + value + ')');
  assert.equal(actual.chat, home === 'standard'); assert.equal(actual.cowork, home === 'standard');
}
assert.equal(run(spec.surfaces + '({chatSurface:{enabled:false}}).chat'), false);
const when = { whenUnset: { from: '2026-11-10', value: 'standard', uncovered: { written: 'chatSessionRetentionDays', sameAs: 'coworkSessionRetentionDays' } } };
for (const [now, expected] of [['2026-11-10T19:59:59Z', undefined], ['2026-11-10T20:00:00Z', 'standard']]) {
  const opts = { mode: '3p', now: Date.parse(now) };
  const args = spec.version === '4.4212.0' ? [when, {}, opts] : [when, opts];
  assert.equal(run(spec.defaultRead + '(' + args.map(JSON.stringify).join(',') + ')').value, expected);
}
for (const overrides of [{ mode: '1p' }, { awaitingServed: true }]) {
  const opts = { mode: '3p', now: Date.parse('2026-11-11T00:00:00Z'), ...overrides };
  const args = spec.version === '4.4212.0' ? [when, {}, opts] : [when, opts];
  assert.equal(run(spec.defaultRead + '(' + args.map(JSON.stringify).join(',') + ')').value, undefined);
}
if (spec.version === '4.4212.0') {
  const args = [when, { chatSessionRetentionDays: 7, coworkSessionRetentionDays: 30 },
    { mode: '3p', now: Date.parse('2026-11-11T00:00:00Z') }];
  assert.equal(run(spec.defaultRead + '(' + args.map(JSON.stringify).join(',') + ')').heldBack, true);
}
run(slice('function ' + spec.auto + '(', spec.autoEnd));
assert.equal(run(spec.auto + '({}).code'), true); assert.equal(run(spec.auto + '({}).cowork'), false);
assert.equal(run(spec.auto + '({workspace:{autoModeEnabled:false}}).code'), false);
assert.equal(run(spec.auto + '({workspace:{autoModeEnabled:true}}).cowork'), true);
run('var ' + slice(spec.browser + '=e=>', ';function ') + ';');
assert.equal(run(spec.browser + '({})'), false);
assert.equal(run(spec.browser + '({builtinBrowser:{enabled:true}})'), true);

const files = new Map(Object.entries(fixture.files));
Object.assign(context, {
  [spec.suffix]: '-3p', [spec.modeName]: 'claude_desktop_config.json', process: { platform: 'darwin', env: {} },
  a: { readFileSync: filename => {
    const name = path.relative('/fixture/Claude-3p', filename);
    if (!files.has(name)) throw Object.assign(new Error('Missing fixture'), { code: 'ENOENT' });
    return files.get(name);
  } }, [spec.warn]: quiet,
});
run(slice('function ' + spec.root + '()', spec.rootEnd));
run(slice('function ' + spec.modeFile + '()', spec.pathEnd));
run(`var ${spec.modeKey}="deploymentMode", ${spec.modeCache}=null;`);
run(slice('function ' + spec.modeRead + '()', spec.modeReadEnd));
run(slice('function ' + spec.meta + '(', spec.appliedEnd));
assert.equal(run(spec.library + '()'), '/fixture/Claude-3p/configLibrary');
assert.equal(run(spec.modeFile + '()'), '/fixture/Claude-3p/claude_desktop_config.json');
for (const [key, value] of Object.entries(fixture.settings)) {
  if (key === 'userThemeMode' || key === 'locale') {
    store[key] = JSON.parse(fixture.files['config.json'])[key];
    assert.equal(run(spec.read + '(' + JSON.stringify(key) + ')'), value);
  } else {
    assert.equal(run(spec.modeRead + '()'), '3p');
    assert.equal(run(spec.applied + '(' + spec.meta + '())[' + JSON.stringify(key) + ']'), value);
  }
}
files.set('configLibrary/_meta.json', JSON.stringify({ appliedId: '../escape' }));
assert.equal(run(spec.applied + '(' + spec.meta + '())'), undefined);
// Hybrid selection is inspected, not executed: no key-store or credential routine.
const hybrid = spec.version === '4.4212.0' ? slice('function JJe()', 'var YJe=') : slice('function TXe()', 'var EXe=');
assert.ok(hybrid.includes('hybridPointer'));
assert.ok(hybrid.includes(spec.version === '4.4212.0' ? '(t?KJe(t):GJe(e))' : '(t?CXe(t):SXe(e))'));
assert.ok(hybrid.includes('sealedValues:"keep"'));
console.log('PASS: pinned ' + spec.version + ' theme/locale/catalog, desktopHome override/default helper, feature readers and local UUID storage; no GUI/key-store');
