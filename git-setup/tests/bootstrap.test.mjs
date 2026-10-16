import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile, execFileSync, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, mkdtemp, writeFile, readFile, readdir, lstat, rm } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import https from 'node:https';
import http from 'node:http';
import net from 'node:net';
import { prepareAuth, authSelection } from '../scripts/git-setup/auth.mjs';
import { setup } from '../scripts/git-setup/setup.mjs';

const exec = promisify(execFile);
const skill = fileURLToPath(new URL('../', import.meta.url));
const owner = execFileSync('git', ['-C', skill, 'rev-parse', '--show-superproject-working-tree'], { encoding: 'utf8' }).trim() || skill;
const base = resolve(process.env.GIT_SETUP_TEST_ROOT || join(owner, 'tmp/git-setup/bootstrap-tests'));
execFileSync('git', ['-C', owner, 'check-ignore', '-q', base]);
const realGit = execFileSync('which', ['git'], { encoding: 'utf8' }).trim();
const cli = join(skill, 'scripts/git-setup/cli.mjs');
const token = 'bootstrap-fixture-only';
const identity = async () => ({ status: 200, data: { login: 'fixture-account', id: 1234 }, headers: {} });
const git = async (root, ...args) => (await exec('git', ['-C', root, '-c', 'commit.gpgsign=false', ...args], { encoding: 'utf8', timeout: 30000 })).stdout.trim();

async function fixture(t, { cnb = false, populated = true } = {}) {
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'bootstrap-'));
  const bin = join(root, 'bin'); await mkdir(bin);
  // Keep fixtures inside the worktree while making their Git discovery boundary explicit.
  // Production config inspection deliberately strips ambient ceiling variables. This test shim
  // restores only that boundary and delegates every operation, including HTTPS, to real Git.
  const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
  await writeFile(join(bin, 'git'), `#!/bin/sh\nGIT_CEILING_DIRECTORIES=${quote(root)}\nexport GIT_CEILING_DIRECTORIES\nexec ${quote(realGit)} "$@"\n`, { mode: 0o755 });
  const previousPath = process.env.PATH;
  process.env.PATH = `${bin}:${previousPath}`;
  t.after(() => { process.env.PATH = previousPath; });
  const project = join(root, 'project'); await mkdir(project);
  const path = cnb ? 'group/sub/project.git' : 'example/project.git';
  const url = `https://${cnb ? 'cnb.cool' : 'github.com'}/${path}`;
  const serverRoot = join(root, 'server'), bare = join(serverRoot, path), seed = join(root, 'seed');
  await mkdir(bare, { recursive: true }); await git(bare, 'init', '--bare', '-q', '-b', 'main');
  if (populated) {
    await mkdir(seed); await git(seed, 'init', '-q', '-b', 'main');
    await writeFile(join(seed, 'README.md'), 'real remote history\n');
    await writeFile(join(seed, 'AGENTS.md'), 'Read these project rules after checkout.\n');
    await git(seed, 'add', 'README.md', 'AGENTS.md');
    await git(seed, '-c', 'user.name=Bootstrap Test', '-c', 'user.email=bootstrap@example.invalid', 'commit', '-qm', 'fixture history');
    await git(seed, 'push', bare, 'main');
    await git(seed, 'push', bare, 'main:refs/heads/feature', 'main:refs/tags/v1');
  }
  await exec('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(root, 'key.pem'), '-out', join(root, 'cert.pem'), '-subj', '/CN=fixture', '-days', '1'], { timeout: 30000 });
  const requests = [];
  const expected = `Basic ${Buffer.from(`${cnb ? 'cnb' : 'x-access-token'}:${token}`).toString('base64')}`;
  const server = https.createServer({ key: await readFile(join(root, 'key.pem')), cert: await readFile(join(root, 'cert.pem')) }, (req, res) => {
    if (req.headers.authorization !== expected) { res.writeHead(401, { 'www-authenticate': 'Basic realm="fixture"' }); res.end(); return; }
    const endpoint = new URL(req.url, 'https://fixture'); requests.push(endpoint.pathname);
    const child = spawn('git', ['http-backend'], { env: { ...process.env, GIT_PROJECT_ROOT: serverRoot, GIT_HTTP_EXPORT_ALL: '1', PATH_INFO: endpoint.pathname, QUERY_STRING: endpoint.search.slice(1), REQUEST_METHOD: req.method, CONTENT_TYPE: req.headers['content-type'] || '', CONTENT_LENGTH: req.headers['content-length'] || '', REMOTE_USER: cnb ? 'cnb' : 'x-access-token', HTTP_GIT_PROTOCOL: req.headers['git-protocol'] || '' }, stdio: ['pipe', 'pipe', 'pipe'] });
    let buffer = Buffer.alloc(0), sent = false;
    const timer = setTimeout(() => child.kill('SIGKILL'), 30000);
    req.pipe(child.stdin); child.stdin.on('error', () => {}); child.stderr.resume();
    child.stdout.on('data', chunk => {
      if (sent) { res.write(chunk); return; }
      buffer = Buffer.concat([buffer, chunk]); const split = buffer.indexOf('\r\n\r\n');
      if (split < 0) return;
      let status = 200; const headers = {};
      for (const line of buffer.subarray(0, split).toString().split('\r\n')) {
        const index = line.indexOf(':'); const key = line.slice(0, index), value = line.slice(index + 1).trim();
        if (key.toLowerCase() === 'status') status = Number.parseInt(value, 10); else headers[key] = value;
      }
      res.writeHead(status, headers); res.write(buffer.subarray(split + 4)); sent = true;
    });
    child.on('error', () => { clearTimeout(timer); res.destroy(); });
    child.on('close', () => { clearTimeout(timer); if (!sent) res.writeHead(500); res.end(); });
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const sockets = new Set(), proxy = http.createServer();
  proxy.on('connect', (req, socket, extra) => {
    assert.equal(req.url, cnb ? 'cnb.cool:443' : 'github.com:443');
    const upstream = net.connect(server.address().port, '127.0.0.1', () => { socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); if (extra.length) upstream.write(extra); socket.pipe(upstream); upstream.pipe(socket); });
    for (const stream of [socket, upstream]) { sockets.add(stream); stream.on('error', () => stream.destroy()); stream.on('close', () => sockets.delete(stream)); }
  });
  await new Promise(done => proxy.listen(0, '127.0.0.1', done));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await Promise.all([new Promise(done => proxy.close(done)), new Promise(done => server.close(done))]); });
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
  const env = { https_proxy: proxyUrl, HTTPS_PROXY: proxyUrl, ALL_PROXY: '', NO_PROXY: '', no_proxy: '', GIT_SSL_NO_VERIFY: '1', GIT_TERMINAL_PROMPT: '0' };
  const method = cnb ? 'cnb-url' : 'github-token';
  const options = { url, env, apiTransport: identity, credentialsFilled: true };
  const prepare = async () => {
    const result = await exec(process.execPath, [cli, 'auth-prepare', '--project', project, '--url', url, '--auth-method', method, '--allow-ignore', '--apply'], { encoding: 'utf8', timeout: 30000 });
    return JSON.parse(result.stdout);
  };
  const fill = async prep => writeFile(prep.credentialFile, cnb ? `https://cnb:${token}@cnb.cool/group/sub/project\n` : `${token}\n`);
  return { root, project, bare, seed, url, method, options, env, prepare, fill, requests };
}

test('GitHub file selection prepares metadata, resumes clone and keeps native Git on the selected file', async t => {
  const f = await fixture(t);
  const choice = await setup(f.project, { url: f.url });
  assert.deepEqual(choice.required, ['auth-method']);
  assert.deepEqual(await readdir(f.project), []);
  const preview = await prepareAuth(f.project, f.url, { authMethod: f.method });
  assert.equal(preview.initialization.originalEmpty, true);
  assert.deepEqual(await readdir(f.project), []);
  const prep = await f.prepare();
  assert.equal(prep.status, 'awaiting_credentials');
  assert.equal(prep.initialization.stage, 'prepared');
  assert.equal(await readFile(prep.credentialFile, 'utf8'), '');
  assert.equal((await lstat(prep.credentialFile)).mode & 0o777, 0o600);
  assert.equal(f.requests.length, 0);
  await f.fill(prep);
  assert.deepEqual((await setup(f.project, { url: f.url })).required, ['credentials-filled']);
  assert.equal(f.requests.length, 0);
  const plan = await setup(f.project, f.options);
  assert.equal(plan.kind, 'clone');
  const cloned = await setup(f.project, { ...f.options, apply: true, expectedFingerprint: plan.fingerprint });
  assert.equal(cloned.status, 'needs_project_rules', JSON.stringify(cloned));
  assert.equal(await git(f.project, 'rev-parse', 'HEAD'), await git(f.bare, 'rev-parse', 'main'));
  assert.equal(await readFile(join(f.project, 'AGENTS.md'), 'utf8'), 'Read these project rules after checkout.\n');
  assert.equal(await git(f.project, 'rev-list', '--count', 'HEAD'), '1');
  assert.equal(await git(f.project, 'rev-parse', 'refs/remotes/origin/feature'), await git(f.bare, 'rev-parse', 'feature'));
  assert.equal(await git(f.project, 'rev-parse', 'refs/tags/v1'), await git(f.bare, 'rev-parse', 'v1'));
  const record = JSON.parse(await git(f.project, 'config', '--local', 'gitsetup.initialization'));
  // Reproduce interruption immediately after checkout and before recording its completion.
  await git(f.project, 'config', 'gitsetup.initialization', JSON.stringify({ ...record, stage: 'checkout' }));
  const continuation = { ...f.options, action: 'add', name: 'origin' };
  const recovery = await setup(f.project, continuation);
  assert.equal(recovery.kind, 'clone');
  assert.equal((await setup(f.project, { ...continuation, apply: true, expectedFingerprint: recovery.fingerprint })).status, 'needs_project_rules');
  const next = await setup(f.project, continuation);
  assert.equal(next.kind, 'connect');
  const result = await setup(f.project, { ...continuation, apply: true, expectedFingerprint: next.fingerprint });
  assert.equal(result.status, 'configured', JSON.stringify(result));
  assert.equal(result.pushed, false); assert.equal(result.committed, false);
  await assert.rejects(git(f.project, 'config', '--local', 'gitsetup.initialization'));
  const native = await exec('git', ['-C', f.project, 'ls-remote', f.url], { env: { ...process.env, ...f.env }, encoding: 'utf8', timeout: 30000 });
  assert.ok(native.stdout.includes(await git(f.bare, 'rev-parse', 'main')));
  assert.equal(await readFile(prep.credentialFile, 'utf8'), `${token}\n`);
  assert.equal(JSON.stringify(result).includes(token), false);
  assert.equal(f.requests.some(path => path.endsWith('git-receive-pack')), false);
});

test('CNB file preparation for a non-Git project preserves files and completes an empty remote initialization', async t => {
  const f = await fixture(t, { cnb: true, populated: false });
  await writeFile(join(f.project, 'notes.txt'), 'preserve this local file\n');
  const prep = await f.prepare(); await f.fill(prep);
  assert.equal(prep.initialization.originalEmpty, false);
  const missing = await setup(f.project, f.options);
  assert.deepEqual(missing.required, ['branch', 'author-name', 'author-email']);
  const args = { ...f.options, branch: 'development', authorName: 'Bootstrap Test', authorEmail: 'bootstrap@example.invalid' };
  const plan = await setup(f.project, args); assert.equal(plan.kind, 'init');
  const result = await setup(f.project, { ...args, apply: true, expectedFingerprint: plan.fingerprint });
  assert.equal(result.status, 'configured', JSON.stringify(result));
  assert.equal(result.local.head, null); assert.equal(result.local.branch, 'development');
  assert.equal(await git(f.project, 'ls-files'), '');
  assert.equal(await git(f.bare, 'for-each-ref'), '');
  assert.equal(await readFile(join(f.project, 'notes.txt'), 'utf8'), 'preserve this local file\n');
  assert.equal(await git(f.project, 'config', '--local', 'user.email'), 'bootstrap@example.invalid');
  assert.equal(result.authentication.persistentHelper, true);
  assert.equal(f.requests.some(path => path.endsWith('git-receive-pack')), false);
});

test('prepared metadata does not bypass target, user-file, index or plan-change checks', async t => {
  const f = await fixture(t, { cnb: true });
  const prep = await f.prepare(); await f.fill(prep);
  const plan = await setup(f.project, f.options); assert.equal(plan.kind, 'clone');
  const before = f.requests.length;
  await assert.rejects(setup(f.project, { ...f.options, url: 'https://cnb.cool/group/other' }), { code: 'setup_initialization_target_changed' });
  await assert.rejects(prepareAuth(f.project, 'https://cnb.cool/group/other', { authMethod: 'cnb-url', apply: true }), { code: 'auth_initialization_target_changed' });
  assert.equal(f.requests.length, before);
  await writeFile(join(f.project, '.hidden'), 'keep');
  const conflict = await setup(f.project, f.options);
  assert.deepEqual(conflict.required, ['local-files']);
  await assert.rejects(setup(f.project, { ...f.options, apply: true, expectedFingerprint: plan.fingerprint }), { code: 'setup_choice_required' });
  assert.equal(await git(f.project, 'remote'), '');
  assert.equal(await readFile(join(f.project, '.hidden'), 'utf8'), 'keep');
  await rm(join(f.project, '.hidden'));
  await git(f.project, 'config', 'bootstrap.test', 'changed');
  await assert.rejects(setup(f.project, { ...f.options, apply: true, expectedFingerprint: plan.fingerprint }), { code: 'setup_plan_changed' });
  await writeFile(join(f.project, 'staged.txt'), 'staged content'); await git(f.project, 'add', 'staged.txt');
  assert.deepEqual((await setup(f.project, f.options)).required, ['local-files']);
  assert.equal(await readFile(join(f.project, 'staged.txt'), 'utf8'), 'staged content');
  await rm(join(f.project, 'staged.txt'));
  const stagedPlan = await setup(f.project, f.options);
  const stopped = await setup(f.project, { ...f.options, apply: true, expectedFingerprint: stagedPlan.fingerprint });
  assert.equal(stopped.error.code, 'setup_initialization_changed');
  assert.equal(await git(f.project, 'ls-files'), 'staged.txt');
  assert.equal(await git(f.project, 'remote'), '');
});

test('remote credential-directory collisions stop checkout and an interrupted fetch can resume after repair', async t => {
  const f = await fixture(t, { cnb: true });
  await mkdir(join(f.seed, '.credentials'));
  await writeFile(join(f.seed, '.credentials/from-remote'), 'remote must not own credentials');
  await git(f.seed, 'add', '.credentials');
  await git(f.seed, '-c', 'user.name=Bootstrap Test', '-c', 'user.email=bootstrap@example.invalid', 'commit', '-qm', 'collision');
  await git(f.seed, 'push', f.bare, 'main');
  const prep = await f.prepare(); await f.fill(prep);
  const plan = await setup(f.project, f.options);
  const stopped = await setup(f.project, { ...f.options, apply: true, expectedFingerprint: plan.fingerprint });
  assert.equal(stopped.status, 'incomplete', JSON.stringify(stopped));
  assert.equal(stopped.error.code, 'setup_credential_collision');
  await assert.rejects(git(f.project, 'rev-parse', '--verify', 'HEAD'));
  assert.equal(await git(f.project, 'ls-files'), '');
  assert.equal(await readFile(prep.credentialFile, 'utf8'), `https://cnb:${token}@cnb.cool/group/sub/project\n`);
  assert.equal(JSON.parse(await git(f.project, 'config', 'gitsetup.initialization')).stage, 'fetching');
  await git(f.seed, 'rm', '.credentials/from-remote');
  await git(f.seed, '-c', 'user.name=Bootstrap Test', '-c', 'user.email=bootstrap@example.invalid', 'commit', '-qm', 'remove collision');
  await git(f.seed, 'push', f.bare, 'main');
  const args = { ...f.options, action: 'add', name: 'origin' };
  const next = await setup(f.project, args); assert.equal(next.kind, 'clone');
  const resumed = await setup(f.project, { ...args, apply: true, expectedFingerprint: next.fingerprint });
  assert.equal(resumed.status, 'needs_project_rules', JSON.stringify(resumed));
  assert.equal(await git(f.project, 'rev-parse', 'HEAD'), await git(f.bare, 'rev-parse', 'HEAD'));
  assert.equal((await authSelection(f.project, f.url)).status, 'needs_input');
});
