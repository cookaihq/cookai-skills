import { registerRepositories } from './fixtures/repositories.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile, readFile, lstat, rm, symlink, chmod } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { execFile, execFileSync, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { devNull } from 'node:os';
import https from 'node:https';
import http from 'node:http';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import { authTarget, credentialOwner } from '../scripts/git-setup/auth-roots.mjs';
import { prepareAuth, authSelection, verifySelection, verifyAuth, publicSelection, installAuth, helperCredential, boundGitHubClient, gitAuthentication } from '../scripts/git-setup/auth.mjs';
import { setup } from '../scripts/git-setup/setup.mjs';
import { inspectRemote, publish, sync } from '../scripts/git-setup/git.mjs';
import { entryCheck } from '../scripts/git-setup/entry-check.mjs';

const exec = promisify(execFile);
const skill = fileURLToPath(new URL('../', import.meta.url));
let testOwner = skill;
try {
  testOwner = execFileSync('git', ['-C', skill, 'rev-parse', '--show-superproject-working-tree'], { encoding: 'utf8', timeout: 10000 }).trim() || skill;
} catch { /* Copied installations keep test files in their own ignored tmp directory. */ }
const base = resolve(process.env.GIT_SETUP_TEST_ROOT || join(testOwner, 'tmp/git-setup/auth-tests'));
execFileSync('git', ['-C', testOwner, 'check-ignore', '-q', base], { timeout: 10000 });
const cli = fileURLToPath(new URL('../scripts/git-setup/cli.mjs', import.meta.url));
const url = 'https://github.com/example/project.git';
const fakeToken = 'fixture-only-token-12345';
const fakeApi = async ({ path }) => ({ status: 200, data: path === '/user' ? { login: 'fixture-account', id: 1234 } : { id: 9 }, headers: {} });
async function git(root, ...args) { return (await exec('git', ['-C', root, ...args], { timeout: 30000, encoding: 'utf8' })).stdout.trim(); }
async function fixture() {
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'auth-'));
  await git(root, 'init', '-q', '-b', 'main');
  await git(root, 'config', 'core.hooksPath', devNull);
  await writeFile(join(root, '.gitignore'), '.credentials/\nchildren/\n');
  return root;
}
async function tokenBinding(root, remote = url, options = {}) {
  const prep = await prepareAuth(root, remote, { authMethod: 'github-token', apply: true, ...options });
  await writeFile(prep.credentialFile, fakeToken + '\n');
  const selected = await verifySelection(await authSelection(root, remote, { credentialsFilled: true, ...options }), { apiTransport: fakeApi });
  await installAuth(root, selected);
  return { prep, selected };
}
function credentialFill(root, target, env = {}) {
  return new Promise((done, reject) => {
    const process = spawn('git', ['-C', root, 'credential', 'fill'], { env: { ...globalThis.process.env, ...env, GIT_TERMINAL_PROMPT: '0' }, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    process.stdout.on('data', c => { stdout += c; }); process.stderr.on('data', c => { stderr += c; });
    const timer = setTimeout(() => process.kill('SIGKILL'), 20000);
    process.on('error', reject);
    process.on('close', code => { clearTimeout(timer); done({ code, stdout, stderr }); });
    process.stdin.end(`protocol=https\nhost=${target.host}\npath=${target.repository}.git\n\n`);
  });
}

test('GitHub URL requires a conversational auth choice before network or configuration writes', async () => {
  const root = await fixture();
  const result = await setup(root, { url });
  assert.equal(result.status, 'needs_input');
  assert.deepEqual(result.required, ['auth-method']);
  assert.equal(result.authentication.choices[1].label.includes(join(root, '.credentials/repositories/example--project.github-token')), true);
  assert.equal(await git(root, 'remote'), '');
  await assert.rejects(lstat(join(root, '.credentials')), { code: 'ENOENT' });
  const output = await exec(process.execPath, [cli, 'setup', '--project', root, '--url', url], { encoding: 'utf8', timeout: 30000 });
  assert.equal(JSON.parse(output.stdout).status, 'needs_input');
});

test('entry checks invalidate previous plans after same-file same-account credential rotation', async () => {
  const root = await fixture();
  await git(root, 'remote', 'add', 'origin', url);
  const { prep } = await tokenBinding(root);
  const before = await entryCheck(root);
  await writeFile(prep.credentialFile, 'rotated-fixture-token\n');
  const changed = await entryCheck(root);
  assert.equal(changed.authentication[0].code, 'auth_source_changed');
  const selected = await verifySelection(await authSelection(root, url, { credentialsFilled: true }), { apiTransport: fakeApi });
  await installAuth(root, selected);
  const after = await entryCheck(root);
  assert.equal(after.authentication[0].status, 'ready');
  assert.deepEqual(after.authentication[0].previousIdentity, before.authentication[0].previousIdentity);
  assert.equal(after.authentication[0].source, before.authentication[0].source);
  assert.notEqual(after.authentication[0].sourceRevision, before.authentication[0].sourceRevision);
  assert.notEqual(after.fingerprint, before.fingerprint);
  assert.equal(JSON.stringify(after).includes('rotated-fixture-token'), false);
});

test('protected empty file, explicit filled confirmation, API identity and native Git use one source', async () => {
  const root = await fixture();
  const prep = await prepareAuth(root, url, { authMethod: 'github-token', apply: true });
  assert.equal(prep.status, 'awaiting_credentials');
  assert.equal(await readFile(prep.credentialFile, 'utf8'), '');
  if (process.platform !== 'win32') {
    assert.equal((await lstat(prep.credentialFile)).mode & 0o777, 0o600);
    assert.equal((await lstat(join(root, '.credentials/repositories'))).mode & 0o777, 0o700);
  }
  await writeFile(prep.credentialFile, fakeToken + '\n');
  assert.deepEqual((await authSelection(root, url)).required, ['credentials-filled']);
  const observed = [];
  const apiTransport = async request => { observed.push(request); return fakeApi(request); };
  const selected = await verifySelection(await authSelection(root, url, { credentialsFilled: true }), { apiTransport });
  assert.equal(observed[0].headers.Authorization, `Bearer ${fakeToken}`);
  await installAuth(root, selected);
  const api = await boundGitHubClient(root, { host: 'github.com', repository: 'example/project' }, { apiTransport });
  await api.getRepository('example/project');
  assert.equal(observed.at(-1).headers.Authorization, `Bearer ${fakeToken}`);
  const fill = await credentialFill(root, authTarget(url));
  assert.equal(fill.code, 0, fill.stderr);
  assert.ok(fill.stdout.includes(`password=${fakeToken}`));
  assert.equal(JSON.stringify(publicSelection(selected)).includes(fakeToken), false);
  assert.equal((await readFile(join(root, '.git/config'), 'utf8')).includes(fakeToken), false);
  await rm(prep.credentialFile);
  const failed = await credentialFill(root, authTarget(url), { GIT_ASKPASS: 'echo ambient-token' });
  assert.notEqual(failed.code, 0);
  assert.equal(failed.stdout.includes('ambient-token'), false);
  assert.equal(failed.stderr.includes(fakeToken), false);
});

test('existing files are preserved and filenames cannot collide across complete remote paths', async () => {
  const root = await fixture();
  const first = await prepareAuth(root, 'https://github.com/a--b/c', { authMethod: 'github-token', apply: true });
  await writeFile(first.credentialFile, fakeToken);
  const again = await prepareAuth(root, 'https://github.com/a--b/c', { authMethod: 'github-token', apply: true });
  assert.equal(again.created, false);
  assert.equal(await readFile(first.credentialFile, 'utf8'), fakeToken);
  if (process.platform !== 'win32') {
    await chmod(first.credentialFile, 0o644);
    await assert.rejects(prepareAuth(root, 'https://github.com/a--b/c', { authMethod: 'github-token', apply: true }), { code: 'auth_file_permissions' });
    assert.equal(await readFile(first.credentialFile, 'utf8'), fakeToken);
    await chmod(first.credentialFile, 0o600);
    await chmod(join(root, '.credentials/repositories'), 0o755);
    await assert.rejects(authSelection(root, 'https://github.com/a--b/c', { credentialsFilled: true }), { code: 'auth_directory_permissions' });
    await chmod(join(root, '.credentials/repositories'), 0o700);
  }
  await assert.rejects(prepareAuth(root, 'https://github.com/a/b--c', { authMethod: 'github-token', apply: true }), { code: 'auth_filename_collision' });
  assert.equal(await readFile(first.credentialFile, 'utf8'), fakeToken);
});

test('tracked, unignored and symbolic-link credential files are refused before reading or overwriting', async () => {
  const root = await fixture();
  await writeFile(join(root, '.gitignore'), '');
  await assert.rejects(prepareAuth(root, url, { authMethod: 'github-token', apply: true }), { code: 'auth_file_not_ignored' });
  const prep = await prepareAuth(root, url, { authMethod: 'github-token', apply: true, allowIgnore: true });
  await git(root, 'add', '-f', '.credentials/repositories/example--project.github-token');
  await assert.rejects(authSelection(root, url, { credentialsFilled: true }), { code: 'auth_file_tracked' });
  await git(root, 'rm', '--cached', '.credentials/repositories/example--project.github-token');
  await rm(prep.credentialFile);
  await writeFile(join(root, 'other'), fakeToken);
  await symlink(join(root, 'other'), prep.credentialFile);
  await assert.rejects(authSelection(root, url, { credentialsFilled: true }), { code: 'auth_file_symlink' });
});

test('CNB full path, fixed username, target validation and separate GitHub files', async () => {
  const root = await fixture();
  const target = 'https://cnb.cool/zhidateam/han/obvault_tannt.git';
  const prep = await prepareAuth(root, target, { authMethod: 'cnb-url', apply: true });
  assert.equal(prep.credentialFile, join(root, '.credentials/repositories/zhidateam--han--obvault_tannt.cnb-url'));
  await writeFile(prep.credentialFile, 'https://cnb:fixture-cnb-token@cnb.cool/zhidateam/han/obvault_tannt\n');
  const selected = await verifySelection(await authSelection(root, target, { credentialsFilled: true }));
  await installAuth(root, selected);
  const native = await credentialFill(root, authTarget(target));
  assert.equal(native.code, 0, native.stderr);
  assert.match(native.stdout, /username=cnb\npassword=fixture-cnb-token/);
  await tokenBinding(root);
  assert.equal((await authSelection(root, target)).binding.method, 'cnb-url');
  await writeFile(prep.credentialFile, 'https://cnb:fixture-cnb-token@cnb.cool/another/project\n');
  await assert.rejects(authSelection(root, target), { code: 'auth_file_target_mismatch' });
  assert.throws(() => authTarget('git@cnb.cool:a/b'), { code: 'cnb_https_required' });
  assert.throws(() => authTarget('ssh://git@cnb.cool/a/b'), { code: 'cnb_https_required' });
});

test('registered children share credential storage with the outer main checkout, not auth choices', async () => {
  const outer = await fixture();
  const child = join(outer, 'children/app');
  await mkdir(child, { recursive: true });
  await git(child, 'init', '-q', '-b', 'main');
  await registerRepositories(outer, ['children/app']);
  const own = await tokenBinding(outer);
  const childChoice = await authSelection(child, url, { credentialWorkspace: outer });
  assert.equal(childChoice.status, 'needs_input');
  assert.deepEqual(childChoice.required, ['auth-method']);
  const prep = await prepareAuth(child, 'https://github.com/example/child', { authMethod: 'github-token', credentialWorkspace: outer, apply: true });
  assert.equal(prep.credentialFile, join(outer, '.credentials/repositories/example--child.github-token'));
  assert.notEqual(prep.credentialFile, own.prep.credentialFile);
  const nested = join(outer, 'children/unregistered');
  await mkdir(nested); await git(nested, 'init', '-q', '-b', 'main');
  assert.equal((await credentialOwner(nested)).owner.main, nested);
  await assert.rejects(credentialOwner(nested, { credentialWorkspace: outer }), { code: 'WORKSPACE_UNREGISTERED' });
});

test('linked worktrees use the primary checkout file', async () => {
  const root = await fixture();
  await git(root, '-c', 'user.name=Auth Test', '-c', 'user.email=auth@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture');
  const linked = join(base, `linked-${Date.now()}`);
  await git(root, 'worktree', 'add', '--detach', linked);
  const { prep } = await tokenBinding(linked);
  assert.equal(prep.credentialFile, join(root, '.credentials/repositories/example--project.github-token'));
  const fill = await credentialFill(linked, authTarget(url));
  assert.equal(fill.code, 0, fill.stderr);
  assert.ok(fill.stdout.includes(fakeToken));
  await git(root, 'worktree', 'remove', linked);
});

test('Issue-only verification persists identity without requiring Git read or changing its API target', async () => {
  const root = await fixture();
  const prep = await prepareAuth(root, url, { authMethod: 'github-token', apply: true });
  await writeFile(prep.credentialFile, fakeToken);
  const requests = [];
  const apiTransport = async request => { requests.push(request); return fakeApi(request); };
  const options = { credentialsFilled: true, apiOnly: true, apiTransport };
  const plan = await verifyAuth(root, url, options);
  assert.equal(plan.remoteRead, 'not_performed');
  const saved = await verifyAuth(root, url, { ...options, apply: true, expectedFingerprint: plan.fingerprint });
  assert.equal(saved.status, 'verified');
  assert.equal(saved.apiPermissions, 'not_verified');
  const client = await boundGitHubClient(root, { host: 'github.com', repository: 'example/project' }, { apiTransport });
  await client.getRepository('example/project');
  assert.equal(requests.at(-1).headers.Authorization, `Bearer ${fakeToken}`);
  assert.equal((await authSelection(root, url)).status, 'selected');
  await assert.rejects(verifyAuth(root, url, { ...options, apiOnly: false, apply: true, expectedFingerprint: plan.fingerprint }), { code: 'auth_plan_changed' });
  assert.equal(await git(root, 'remote'), '');
});

test('registered owner loss and changed source bindings do not create replacements', async () => {
  const outer = await fixture(), child = join(outer, 'children/app');
  await mkdir(child, { recursive: true }); await git(child, 'init', '-q', '-b', 'main');
  await registerRepositories(outer, ['children/app']);
  const { prep } = await tokenBinding(child, url, { credentialWorkspace: outer });
  await writeFile(join(outer, 'fixture-git/repos.conf'), '');
  await assert.rejects(authSelection(child, url), { code: 'WORKSPACE_UNREGISTERED' });
  await assert.rejects(lstat(join(child, '.credentials')), { code: 'ENOENT' });
  assert.equal(await readFile(prep.credentialFile, 'utf8'), fakeToken + '\n');
});

test('legacy dotenv compatibility retains its exact source and never silently switches to a higher layer', async () => {
  const root = await fixture();
  const value = `GIT_SETUP_GITHUB_AUTH=token\nGIT_SETUP_GITHUB_HOST=github.com\nGIT_SETUP_GITHUB_REPOSITORY=example/project\nGIT_SETUP_GITHUB_TOKEN=${fakeToken}\n`;
  await writeFile(join(root, '.env.local'), value);
  const selected = await verifySelection(await authSelection(root, url, { githubAuth: true }), { apiTransport: fakeApi });
  await installAuth(root, selected);
  assert.equal((await credentialFill(root, authTarget(url))).code, 0);
  const client = await boundGitHubClient(root, { host: 'github.com', repository: 'example/project' }, { apiTransport: fakeApi });
  await client.getRepository('example/project');
  await writeFile(join(root, '.env.git-setup'), value);
  await assert.rejects(authSelection(root, url), { code: 'auth_legacy_source_changed' });
  assert.notEqual((await credentialFill(root, authTarget(url))).code, 0);
  await assert.rejects(lstat(join(root, '.credentials')), { code: 'ENOENT' });
});

test('gh selects an explicit account, excludes environment overrides and shares its token with API', async () => {
  const root = await fixture();
  const calls = [];
  const run = async (_exe, args, options) => {
    calls.push({ args, env: options.env });
    if (args[0] === '--version') return { stdout: 'gh version fixture' };
    if (args[1] === 'status') return { stdout: JSON.stringify({ hosts: { 'github.com': [{ login: 'another', state: 'success', active: true }, { login: 'fixture-account', state: 'success', active: false }] } }) };
    assert.deepEqual(args, ['auth', 'token', '--hostname', 'github.com', '--user', 'fixture-account']);
    return { stdout: fakeToken };
  };
  assert.deepEqual((await authSelection(root, url, { authMethod: 'gh', run })).required, ['gh-user']);
  const selected = await verifySelection(await authSelection(root, url, { authMethod: 'gh', ghUser: 'fixture-account', run }), { apiTransport: fakeApi });
  assert.equal(selected.identity.login, 'fixture-account');
  assert.ok(calls.every(call => call.env.GH_TOKEN === undefined && call.env.GITHUB_TOKEN === undefined));
  const failed = async (...args) => { if (args[1][1] === 'token') throw Error('do not expose secret'); return run(...args); };
  await assert.rejects(authSelection(root, url, { authMethod: 'gh', ghUser: 'fixture-account', run: failed }), { code: 'gh_account_unavailable' });
  await assert.rejects(lstat(join(root, '.credentials')), { code: 'ENOENT' });
});

test('native Git helper reuses the selected gh account without exporting it to a project file', { skip: process.platform === 'win32' }, async () => {
  const root = await fixture(), bin = join(root, 'fixture-bin');
  await mkdir(bin);
  const script = `#!${process.execPath}\nconst a=process.argv.slice(2); if(process.env.GH_TOKEN||process.env.GITHUB_TOKEN) process.exit(2); if(a[0]==='--version') console.log('fixture gh'); else if(a[1]==='status') console.log(JSON.stringify({hosts:{'github.com':[{login:'fixture-account',state:'success',active:false},{login:'other-account',state:'success',active:true}]}})); else if(a.join(' ')==='auth token --hostname github.com --user fixture-account') console.log('${fakeToken}'); else process.exit(3);\n`;
  await writeFile(join(bin, 'gh'), script, { mode: 0o755 });
  const original = { PATH: process.env.PATH, GH_TOKEN: process.env.GH_TOKEN, GITHUB_TOKEN: process.env.GITHUB_TOKEN };
  Object.assign(process.env, { PATH: `${bin}:${process.env.PATH}`, GH_TOKEN: 'ambient-fixture', GITHUB_TOKEN: 'ambient-fixture' });
  try {
    const prep = await prepareAuth(root, url, { authMethod: 'gh', ghUser: 'fixture-account', apply: true });
    assert.equal(prep.binding.account, 'fixture-account');
    const selected = await verifySelection(await authSelection(root, url), { apiTransport: fakeApi });
    await installAuth(root, selected);
    const native = await credentialFill(root, authTarget(url));
    assert.equal(native.code, 0, native.stderr);
    assert.match(native.stdout, /username=fixture-account/);
    assert.ok(native.stdout.includes(fakeToken));
    await assert.rejects(lstat(join(root, '.credentials')), { code: 'ENOENT' });
    assert.equal((await readFile(join(root, '.git/config'), 'utf8')).includes(fakeToken), false);
    await writeFile(join(bin, 'gh'), `#!${process.execPath}\nprocess.exit(1);\n`);
    const unavailable = await credentialFill(root, authTarget(url));
    assert.notEqual(unavailable.code, 0);
    assert.ok(!unavailable.stdout.includes('ambient-fixture'));
  } finally { for (const [key, value] of Object.entries(original)) if (value === undefined) delete process.env[key]; else process.env[key] = value; }
});

test('an API denial identifies the selected file and a conflicting native helper cannot mark setup ready', async () => {
  const root = await fixture();
  const { prep } = await tokenBinding(root);
  const denied = async () => ({ status: 401, data: {}, headers: {} });
  await assert.rejects(boundGitHubClient(root, { host: 'github.com', repository: 'example/project' }, { apiTransport: denied }), error => error.status === 401 && error.message.includes(prep.credentialFile) && !error.message.includes(fakeToken));
  await git(root, 'config', 'http.https://github.com/.extraheader', 'Authorization: Basic fixture');
  const selected = await verifySelection(await authSelection(root, url), { apiTransport: fakeApi });
  await assert.rejects(installAuth(root, selected), { code: 'auth_http_header_conflict' });
  assert.deepEqual((await authSelection(root, url)).required, ['credentials-filled']);
  assert.notEqual((await credentialFill(root, authTarget(url))).code, 0);
});

test('network entrypoints require the selected source; unrelated targets and rotation cannot reuse it', async () => {
  const root = await fixture();
  await assert.rejects(inspectRemote(root, url), { code: 'auth_selection_required' });
  const { prep, selected } = await tokenBinding(root);
  await assert.rejects(helperCredential(root, { protocol: 'https', host: 'github.com', path: 'example/other.git' }), { code: 'auth_binding_required' });
  await assert.rejects(boundGitHubClient(root, { host: 'github.com', repository: 'example/other' }), { code: 'auth_selection_required' });
  await writeFile(prep.credentialFile, 'rotated-fixture-token');
  await assert.rejects(gitAuthentication(root, {}, selected).authForUrl(url), { code: 'auth_source_changed' });
  const identityChanged = async () => ({ status: 200, data: { login: 'different', id: 987 }, headers: {} });
  await assert.rejects(boundGitHubClient(root, { host: 'github.com', repository: 'example/project' }, { apiTransport: identityChanged }), { code: 'auth_identity_changed' });
});

test('submodules initialized in an outer worktree resolve storage to the outer primary checkout', async () => {
  const seed = await fixture(), outer = await fixture();
  await git(seed, '-c', 'user.name=Auth Test', '-c', 'user.email=auth@example.invalid', 'commit', '--allow-empty', '-qm', 'seed');
  await git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', seed, 'module');
  await git(outer, '-c', 'user.name=Auth Test', '-c', 'user.email=auth@example.invalid', 'commit', '-qm', 'register submodule');
  const linked = join(base, `submodule-linked-${Date.now()}`);
  await git(outer, 'worktree', 'add', '--detach', linked);
  await git(linked, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init');
  const scope = await credentialOwner(join(linked, 'module'));
  assert.equal(scope.owner.main, outer);
  const prep = await prepareAuth(join(linked, 'module'), url, { authMethod: 'github-token', apply: true });
  assert.equal(prep.credentialFile, join(outer, '.credentials/repositories/example--project.github-token'));
  await git(outer, 'worktree', 'remove', '--force', linked);
});

test('ordinary git ls-remote and setup use the bound helper over a local HTTPS Git server', async t => {
  const root = await fixture();
  const { prep } = await tokenBinding(root);
  const certDir = await mkdtemp(join(base, 'tls-'));
  // Test-only certificate and private key stay in the ignored task directory.
  await exec('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(certDir, 'key.pem'), '-out', join(certDir, 'cert.pem'), '-subj', '/CN=github.com', '-days', '1'], { timeout: 30000 });
  const accepted = [];
  const expected = `Basic ${Buffer.from(`x-access-token:${fakeToken}`).toString('base64')}`;
  const server = https.createServer({ key: await readFile(join(certDir, 'key.pem')), cert: await readFile(join(certDir, 'cert.pem')) }, (req, res) => {
    if (req.headers.authorization !== expected) { res.writeHead(401, { 'www-authenticate': 'Basic realm="fixture"' }); res.end(); return; }
    accepted.push(req.url);
    res.writeHead(200, { 'content-type': 'application/x-git-upload-pack-advertisement' });
    res.end('001e# service=git-upload-pack\n00000000');
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const sockets = new Set();
  const proxy = http.createServer();
  proxy.on('connect', (req, socket, head) => {
    assert.equal(req.url, 'github.com:443');
    const upstream = net.connect(server.address().port, '127.0.0.1', () => { socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); if (head.length) upstream.write(head); socket.pipe(upstream); upstream.pipe(socket); });
    for (const stream of [socket, upstream]) { sockets.add(stream); stream.on('error', () => stream.destroy()); stream.on('close', () => sockets.delete(stream)); }
  });
  await new Promise(done => proxy.listen(0, '127.0.0.1', done));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await Promise.all([new Promise(done => proxy.close(done)), new Promise(done => server.close(done))]); });
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
  const env = { ...process.env, https_proxy: proxyUrl, HTTPS_PROXY: proxyUrl, ALL_PROXY: '', NO_PROXY: '', no_proxy: '', GIT_SSL_NO_VERIFY: '1', GIT_TERMINAL_PROMPT: '0' };
  const native = await exec('git', ['-C', root, 'ls-remote', url], { env, timeout: 30000, encoding: 'utf8' });
  assert.equal(native.stdout, '');
  assert.ok(accepted.length > 0);
  const args = { url, env, apiTransport: fakeApi };
  const plan = await setup(root, args);
  assert.equal(plan.status, 'planned');
  await writeFile(prep.credentialFile, fakeToken + '\r\n');
  await assert.rejects(setup(root, { ...args, apply: true, expectedFingerprint: plan.fingerprint }), { code: 'setup_plan_changed' });
  await writeFile(prep.credentialFile, fakeToken + '\n');
  const result = await setup(root, { ...args, apply: true, expectedFingerprint: plan.fingerprint });
  assert.equal(result.status, 'configured', JSON.stringify(result));
  assert.equal(result.authentication.persistentHelper, true);
  assert.equal(result.authentication.remoteRead, 'verified');
  assert.equal(result.authentication.pushPreflight, 'not_performed');
  assert.equal(JSON.stringify(result).includes(fakeToken), false);
  assert.equal(await git(root, 'remote', 'get-url', 'origin'), url);
  await rm(prep.credentialFile);
  await assert.rejects(exec('git', ['-C', root, 'ls-remote', url], { env, timeout: 30000, encoding: 'utf8' }), error => !error.stderr.includes(fakeToken));
});

test('publish and sync authenticate each HTTPS target independently and stop when its file disappears', async t => {
  const root = await fixture();
  const serverRoot = await mkdtemp(join(base, 'git-http-'));
  const source = 'https://github.com/example/source.git', target = 'https://cnb.cool/group/sub/target.git';
  const sourceDir = join(serverRoot, 'example/source.git'), targetDir = join(serverRoot, 'group/sub/target.git');
  for (const dir of [sourceDir, targetDir]) {
    await mkdir(dir, { recursive: true }); await git(dir, 'init', '--bare', '-q');
    await git(dir, 'config', 'http.receivepack', 'true');
  }
  await writeFile(join(root, 'file.txt'), 'fixture content\n');
  await git(root, 'add', 'file.txt');
  await git(root, '-c', 'user.name=Auth Test', '-c', 'user.email=auth@example.invalid', 'commit', '-qm', 'fixture');
  const head = await git(root, 'rev-parse', 'HEAD');
  await tokenBinding(root, source);
  const cnb = await prepareAuth(root, target, { authMethod: 'cnb-url', apply: true });
  await writeFile(cnb.credentialFile, 'https://cnb:fixture-cnb-only@cnb.cool/group/sub/target\n');
  await installAuth(root, await verifySelection(await authSelection(root, target, { credentialsFilled: true })));
  await git(root, 'remote', 'add', 'origin', source); await git(root, 'remote', 'add', 'cnb', target);
  await exec('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(serverRoot, 'key.pem'), '-out', join(serverRoot, 'cert.pem'), '-subj', '/CN=fixture', '-days', '1'], { timeout: 30000 });
  const requests = [];
  const server = https.createServer({ key: await readFile(join(serverRoot, 'key.pem')), cert: await readFile(join(serverRoot, 'cert.pem')) }, (req, res) => {
    const endpoint = new URL(req.url, 'https://fixture');
    const isSource = endpoint.pathname.startsWith('/example/source.git/');
    const expected = `Basic ${Buffer.from(isSource ? `x-access-token:${fakeToken}` : 'cnb:fixture-cnb-only').toString('base64')}`;
    if (req.headers.authorization !== expected) { res.writeHead(401, { 'www-authenticate': 'Basic realm="fixture"' }); res.end(); return; }
    requests.push({ host: req.headers.host, path: endpoint.pathname, method: req.method });
    const child = spawn('git', ['http-backend'], { env: { ...process.env, GIT_PROJECT_ROOT: serverRoot, GIT_HTTP_EXPORT_ALL: '1', PATH_INFO: endpoint.pathname, QUERY_STRING: endpoint.search.slice(1), REQUEST_METHOD: req.method, CONTENT_TYPE: req.headers['content-type'] || '', CONTENT_LENGTH: req.headers['content-length'] || '', REMOTE_USER: isSource ? 'x-access-token' : 'cnb', HTTP_GIT_PROTOCOL: req.headers['git-protocol'] || '' }, stdio: ['pipe', 'pipe', 'pipe'] });
    let headers = Buffer.alloc(0), sent = false;
    const timer = setTimeout(() => child.kill('SIGKILL'), 30000);
    req.pipe(child.stdin); child.stdin.on('error', () => {}); child.stderr.resume();
    child.stdout.on('data', chunk => {
      if (sent) { res.write(chunk); return; }
      headers = Buffer.concat([headers, chunk]); const split = headers.indexOf('\r\n\r\n');
      if (split < 0) return;
      let status = 200; const responseHeaders = {};
      for (const line of headers.subarray(0, split).toString().split('\r\n')) {
        const index = line.indexOf(':'); const key = line.slice(0, index), value = line.slice(index + 1).trim();
        if (key.toLowerCase() === 'status') status = Number.parseInt(value, 10); else responseHeaders[key] = value;
      }
      res.writeHead(status, responseHeaders); res.write(headers.subarray(split + 4)); sent = true;
    });
    child.on('error', () => { clearTimeout(timer); res.destroy(); });
    child.on('close', () => { clearTimeout(timer); if (!sent) res.writeHead(500); res.end(); });
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const sockets = new Set(), proxy = http.createServer();
  proxy.on('connect', (req, socket, extra) => {
    assert.ok(['github.com:443', 'cnb.cool:443'].includes(req.url));
    const upstream = net.connect(server.address().port, '127.0.0.1', () => { socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); if (extra.length) upstream.write(extra); socket.pipe(upstream); upstream.pipe(socket); });
    for (const stream of [socket, upstream]) { sockets.add(stream); stream.on('error', () => stream.destroy()); stream.on('close', () => sockets.delete(stream)); }
  });
  await new Promise(done => proxy.listen(0, '127.0.0.1', done));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await Promise.all([new Promise(done => proxy.close(done)), new Promise(done => server.close(done))]); });
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
  const env = { https_proxy: proxyUrl, HTTPS_PROXY: proxyUrl, ALL_PROXY: '', NO_PROXY: '', no_proxy: '', GIT_SSL_NO_VERIFY: '1' };
  const auth = gitAuthentication(root, { apiTransport: fakeApi });
  const published = await publish(root, { remote: 'origin', refs: ['refs/heads/main'], ...auth, env });
  assert.equal(published.complete, true, JSON.stringify(published));
  assert.equal(await git(sourceDir, 'rev-parse', 'refs/heads/main'), head);
  const config = { remotes: { origin: source, cnb: target }, sync: { source: 'origin', targets: ['cnb'], refs: ['refs/heads/main'] } };
  const plan = await sync(root, config, { dryRun: true, ...auth, env });
  assert.equal(plan.results[0].status, 'planned');
  const synced = await sync(root, config, { ...auth, env });
  assert.equal(synced.complete, true, JSON.stringify(synced));
  assert.equal(await git(targetDir, 'rev-parse', 'refs/heads/main'), head);
  assert.ok(requests.some(request => request.host === 'github.com' && request.path.endsWith('/git-receive-pack')));
  assert.ok(requests.some(request => request.host === 'cnb.cool' && request.path.endsWith('/git-receive-pack')));
  const writes = requests.filter(request => request.path.endsWith('/git-receive-pack')).length;
  await rm(cnb.credentialFile);
  const failed = await sync(root, config, { dryRun: true, ...gitAuthentication(root, { apiTransport: fakeApi }), env });
  assert.equal(failed.results[0].status, 'failed');
  assert.equal(requests.filter(request => request.path.endsWith('/git-receive-pack')).length, writes);
  assert.equal(JSON.stringify(auth.authReports).includes(fakeToken), false);
  assert.equal(JSON.stringify(auth.authReports).includes('fixture-cnb-only'), false);
});
