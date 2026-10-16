import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile, readFile, lstat, rm } from 'node:fs/promises';
import { join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { devNull } from 'node:os';
import { digest, runtimeBundle, writePackage, installHelperRuntime } from '../scripts/git-setup/runtime-bundle.mjs';

const exec = promisify(execFile);
const cli = fileURLToPath(new URL('../scripts/git-setup/cli.mjs', import.meta.url));
const base = resolve(process.env.GIT_SETUP_TEST_ROOT || '../../tmp/git-setup/runtime-cleanup');
test('explicit helper cleanup preserves references, active processes and changed packages without creating project files', async () => {
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'cleanup-'));
  const fixtureHome = await mkdtemp(join(base, 'cleanup-home-'));
  const env = { ...process.env, HOME: fixtureHome, USERPROFILE: fixtureHome, XDG_CONFIG_HOME: join(fixtureHome, '.config') };
  for (const key of Object.keys(env)) if (/^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG)/.test(key)) delete env[key];
  env.GIT_CONFIG_NOSYSTEM = '1';
  // Production removes inherited GIT_CONFIG* overrides. Isolate this fixture
  // at the process boundary instead; every call still executes the real Git.
  const preload = join(fixtureHome, 'isolated-git.mjs');
  await writeFile(preload, `import childProcess from 'node:child_process';
import { promisify } from 'node:util';
import { syncBuiltinESMExports } from 'node:module';
const original = childProcess.execFile;
const optionsFor = (file, options = {}) => file === 'git'
  ? { ...options, env: { ...(options.env || process.env), GIT_CONFIG_NOSYSTEM: '1' } }
  : options;
const isolated = (file, args, options, callback) => original(file, args, optionsFor(file, options), callback);
isolated[promisify.custom] = (file, args, options) => original[promisify.custom](file, args, optionsFor(file, options));
childProcess.execFile = isolated;
syncBuiltinESMExports();
`);
  const nodeArgs = ['--import', pathToFileURL(preload).href];
  const git = async (...args) => (await exec('git', ['-C', root, ...args], { timeout: 30000, env })).stdout.trim();
  await git('init', '-q', '-b', 'main');
  await git('config', 'core.hooksPath', devNull);
  await git('config', 'user.name', 'Runtime Fixture'); await git('config', 'user.email', 'runtime@example.invalid');
  await git('commit', '--allow-empty', '-qm', 'fixture');
  // Check through the same production reader and preload used by cleanup,
  // before cleanup can acquire a lock in any real configuration directory.
  const reader = new URL('../scripts/git-setup/repository-facts.mjs', import.meta.url).href;
  const probe = `import { localGit } from ${JSON.stringify(reader)};
const records = (await localGit(process.argv[1], ['config', '--show-origin', '--null', '--list'])).split('\\0');
process.stdout.write(JSON.stringify(records.filter((_, index) => index % 2 === 0 && records[index])));`;
  const origins = JSON.parse((await exec(process.execPath, [...nodeArgs, '--input-type=module', '-e', probe, root], { timeout: 30000, env })).stdout);
  assert.ok(origins.length > 0);
  for (const origin of origins) {
    assert.ok(origin.startsWith('file:'));
    const source = resolve(root, origin.slice(5));
    assert.ok(source.startsWith(root + sep) || source.startsWith(fixtureHome + sep), 'cleanup fixture requires isolated Git configuration sources');
  }
  const common = join(root, '.git');
  const current = await installHelperRuntime(common);
  await git('config', 'credential.helper', `!node '${current.file}' --project '${root}'`);
  const bundle = await runtimeBundle('helper');
  const makeOld = async (version, legacy = false) => {
    const files = { ...bundle.files, 'package.json': JSON.stringify({ ...JSON.parse(bundle.files['package.json']), version }) };
    const names = legacy ? ['package.json', 'LICENSE', ...Object.keys(files).filter(name => name.startsWith('scripts/')).sort()] : Object.keys(files).sort((a, b) => a.localeCompare(b));
    const hashes = Object.fromEntries(names.map(name => [name, digest(files[name])]));
    const id = digest(hashes), path = join(common, 'git-setup', id);
    await writePackage(path, files);
    return { id, path };
  };
  const unused = await makeOld('0.0.1', true), referenced = await makeOld('0.0.2'), active = await makeOld('0.0.3');
  const changed = await makeOld('0.0.4');
  const globalReferenced = await makeOld('0.0.5');
  await writeFile(join(fixtureHome, '.gitconfig'), `[credential]\n\thelper = ${JSON.stringify(globalReferenced.path)}\n`);
  await writeFile(join(changed.path, 'LICENSE'), 'user changed this file');
  const linked = join(base, `linked worktree-${unused.id.slice(0, 12)}-${Date.now()}`);
  await git('worktree', 'add', '--detach', linked);
  await git('config', 'extensions.worktreeConfig', 'true');
  await exec('git', ['-C', linked, 'config', '--worktree', 'credential.helper', `!node '${referenced.path}/scripts/git-setup/credential-helper.mjs' --project '${root}'`], { env });
  const run = async args => {
    try { return JSON.parse((await exec(process.execPath, [...nodeArgs, cli, 'auth-cleanup', '--project', root, ...args], { timeout: 30000, env })).stdout); }
    catch (error) { return JSON.parse(error.stdout || error.stderr); }
  };
  const child = spawn(process.execPath, [join(active.path, 'scripts/git-setup/credential-helper.mjs'), '--project', root, 'get'], { stdio: ['pipe', 'ignore', 'ignore'], env });
  try {
    const plan = await run([]);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    assert.deepEqual(plan.candidates.map(item => item.id), [unused.id]);
    assert.equal(plan.retained.find(item => item.id === referenced.id).reason, 'referenced');
    assert.equal(plan.retained.find(item => item.id === globalReferenced.id).reason, 'referenced');
    assert.equal(plan.retained.find(item => item.id === active.id).reason, 'active_process');
    assert.equal(plan.retained.find(item => item.id === changed.id).reason, 'modified_or_unknown');
    const configBefore = await readFile(join(common, 'config'));
    await writeFile(join(common, 'git-setup-harness.lock'), 'unfinished installation');
    assert.equal((await run(['--apply', '--expected-fingerprint', plan.fingerprint])).code, 'runtime_cleanup_busy');
    await rm(join(common, 'git-setup-harness.lock'));
    await writeFile(join(fixtureHome, '.gitconfig.lock'), 'other operation');
    assert.equal((await run(['--apply', '--expected-fingerprint', plan.fingerprint])).code, 'runtime_cleanup_busy');
    assert.equal(await readFile(join(fixtureHome, '.gitconfig.lock'), 'utf8'), 'other operation');
    await rm(join(fixtureHome, '.gitconfig.lock'));
    const legacyEntry = join(fixtureHome, 'cli.mjs');
    await writeFile(legacyEntry, 'setInterval(() => {}, 1000);\n');
    const legacy = spawn(process.execPath, [legacyEntry, 'auth-verify'], { cwd: root, env, stdio: 'ignore' });
    try { assert.equal((await run([])).code, 'runtime_cleanup_busy'); }
    finally { legacy.kill(); await new Promise(done => legacy.once('close', done)); }
    await git('config', 'credential.extra', unused.path);
    assert.equal((await run(['--apply', '--expected-fingerprint', plan.fingerprint])).code, 'runtime_cleanup_changed');
    await git('config', '--unset', 'credential.extra');
    const fresh = await run([]);
    const applied = await run(['--apply', '--expected-fingerprint', fresh.fingerprint]);
    assert.equal(applied.status, 'cleaned', JSON.stringify(applied));
    assert.deepEqual(applied.removed, [unused.path]);
    await assert.rejects(lstat(unused.path), { code: 'ENOENT' });
    for (const path of [referenced.path, globalReferenced.path, active.path, changed.path]) assert.equal((await lstat(path)).isDirectory(), true);
    assert.deepEqual(await readFile(join(common, 'config')), configBefore);
    await assert.rejects(lstat(join(fixtureHome, '.gitconfig.lock')), { code: 'ENOENT' });
    await assert.rejects(lstat(join(root, 'data')), { code: 'ENOENT' });
    await assert.rejects(lstat(join(root, 'tmp')), { code: 'ENOENT' });
    await git('config', 'includeIf.onbranch:unused.path', '../unknown-config');
    assert.equal((await run([])).code, 'runtime_cleanup_configuration');
  } finally {
    child.kill();
    await new Promise(done => child.exitCode !== null || child.signalCode !== null ? done() : child.once('close', done));
    await git('worktree', 'remove', linked);
  }
});
