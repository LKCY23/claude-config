// Execute pinned schema and settings-store slices with in-memory dependencies.
// Never load the Electron entrypoint, real filesystem, account or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const root = process.argv[2];
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const hashes = {
  'src-CTNXdakG.js': '8bef484d79c728e944bf1ee5bc9d91e135864363ad449a3c182f0d888a991660',
  'startup-requirements-CuyNnLmn.js': '3451b138221aaad11aad1db8ec122a3363189379b9d47d2b0fe7bf553636a46f',
  'zod-DDAr8jEZ.js': 'e21ef21d2a511d5fa0b276b4f759011baf5b21d99d11e82a7c4515996eb4e0db',
  'rolldown-runtime-CPUxUITh.js': '9a875a54cb52a6ee0d1097895ae92aa8c9d999af95f167e90456d6fc0c03f296',
};
const sources = {};
for (const [name, hash] of Object.entries(hashes)) {
  sources[name] = fs.readFileSync(path.join(root, name), 'utf8');
  assert.equal(crypto.createHash('sha256').update(sources[name]).digest('hex'), hash);
}
const modules = new Map();
function vendor(name) {
  assert.ok(['zod-DDAr8jEZ.js', 'rolldown-runtime-CPUxUITh.js'].includes(name));
  if (!modules.has(name)) {
    const exports = {};
    modules.set(name, exports);
    vm.runInNewContext(sources[name], {
      exports, require: id => vendor(id.replace(/^\.\//, '')),
    }, {timeout: 1000});
  }
  return modules.get(name);
}
const source = sources['src-CTNXdakG.js'];
const storage = sources['startup-requirements-CuyNnLmn.js'];
function slice(text, start, end) {
  const a = text.indexOf(start), b = text.indexOf(end, a + start.length);
  assert.ok(a >= 0 && text.indexOf(start, a + 1) < 0 && b > a);
  return text.slice(a, b);
}
// The reviewed declarations contain string literals, nested objects and a
// balanced regex quantifier. No interpolation is evaluated by this scanner.
function objectAt(text, start) {
  let depth = 0, quote = null, escaped = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (c === '\\') escaped = true;
      else if (c === quote) quote = null;
    } else if ('"\'`'.includes(c)) quote = c;
    else if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return text.slice(start, i + 1);
  }
  throw new Error('Incomplete reviewed declaration');
}
const context = vm.createContext({t: vendor('zod-DDAr8jEZ.js')});
const run = code => vm.runInContext(code, context, {timeout: 1000});
run(slice(source, 'var q={AYU:', 'var J=t.v()'));
run(slice(source, 'var J=t.v()', 'var Zc='));
run('var vp=' + objectAt(source, source.indexOf('vp={sans:') + 3) + ';');
run('var Pn=' + source.match(/Pn=(\[[^\]]+\])/)[1] + ',Fn=`in-app-browser`;');
assert.ok(source.includes('Fn=`in-app-browser`'));
assert.deepEqual(JSON.parse(run('JSON.stringify(Object.fromEntries(Object.entries(Uc).map(([k,v])=>[k,v.variants])))')),
  fixture.themes);
const definitions = new Map();
for (const row of fixture.cases) {
  const marker = `key:\`${row.key}\``;
  const at = source.indexOf(marker);
  assert.ok(at > 0 && source.indexOf(marker, at + 1) < 0);
  const start = source.lastIndexOf('{agentAccess:', at);
  const factory = source[start - 2];
  assert.ok(['X', 'Y', 'Z'].includes(factory));
  const definition = run(factory + '(' + objectAt(source, start) + ')');
  assert.equal(definition.key, row.key);
  for (const value of row.valid) assert.ok(definition.schema.safeParse(value).success, row.key);
  for (const value of row.invalid) assert.equal(definition.schema.safeParse(value).success, false, row.key);
  definitions.set(row.key, definition);
}
Object.assign(context, {
  o: {a: () => () => ({warning() {}})},
  l: {existsSync: () => false}, // Constructor reads an empty fixture, never disk.
  K: 'fixture-legacy-atoms', lr: 'fixture-initial-followup',
  n: {
    L: [...definitions.values()], N: key => definitions.get(key),
    P: key => definitions.get(key).schema,
    U: Object.fromEntries(['followUpQueueMode', 'composerEnterBehavior',
      'keepRemoteControlAwakeWhilePluggedIn', 'preventSleepWhileRunning'].map(k => [k, definitions.get(k)])),
    gs: {LOCAL_PROJECTS: 'fixture-projects'}, B: {defaultServiceTier: {key: 'unmanaged-service-tier'}},
  },
});
run(slice(storage, 'var oe=', 'var me='));
run(slice(storage, 'var X=o.a(`settings-store`)', 'var Ki=new Set('));
const globalValues = new Map([['fixture-initial-followup', 'queue']]);
const globals = {getStored: key => globalValues.get(key), set: (key, value) => globalValues.set(key, value),
  delete: key => globalValues.delete(key), async flush() {}};
context.globals = globals;
const store = run('new zi("/fixture/config.toml",globals)');
context.values = Object.fromEntries(fixture.cases.map(r => [r.key, r.valid[0]]));
// Use the real codec so nullable font members have their actual TOML encoding.
const desktop = run('Object.fromEntries(Object.entries(values).map(([k,v])=>[k,le(n.P(k),v)]))');
desktop.unmanagedPreference = 'preserve';
const writes = [];
let fail = false;
const client = {config: {desktop}, async batchWriteConfigValues(value) {
  if (fail) throw new Error('isolated-write-failure');
  writes.push(JSON.parse(JSON.stringify(value)));
}};
async function main() {
  await store.initialize(client);
  assert.equal(writes.length, 0, 'Explicit settings need no migration/default write');
  for (const row of fixture.cases) {
    assert.deepEqual(JSON.parse(JSON.stringify(store.get(row.key))), row.valid[0], row.key);
    await store.setAndPersist(row.key, row.valid.at(-1));
    assert.equal(writes.at(-1).edits[0].keyPath, 'desktop.' + row.key);
    assert.equal(writes.at(-1).edits[0].mergeStrategy, 'replace');
  }
  assert.equal(store.getDesktopConfig().unmanagedPreference, 'preserve');
  await store.setAndPersist('reviewDelivery', 'inline');
  fail = true;
  await assert.rejects(store.setAndPersist('reviewDelivery', 'detached'), /isolated-write-failure/);
  assert.equal(store.get('reviewDelivery'), 'inline', 'Failed write restores in-memory value');
  assert.equal(store.getDesktopConfig().reviewDelivery, 'inline');
  fail = false;
  await store.flush();
  const count = writes.length;
  assert.throws(() => store.setAndPersist('reviewDelivery', 'invalid'));
  assert.equal(writes.length, count, 'Invalid schema never reaches persistence');
  const fresh = run('new zi("/fixture/config.toml",globals)');
  assert.throws(() => fresh.setAndPersist('reviewDelivery', 'inline'), /not initialized/);
  console.log(JSON.stringify({registered_fields: definitions.size, code_themes: Object.keys(fixture.themes).length,
    native_read_write: 'passed', failed_write_recovery: 'passed', model_requests: 0}));
}
main().catch(error => {console.error(error); process.exitCode = 1;});
