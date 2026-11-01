import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, rm, lstat } from 'node:fs/promises';
import { join, resolve, relative } from 'node:path';
import { spawn } from 'node:child_process';
import { repositoryFixture, registerRepositories } from './fixtures/repositories.mjs';
import { gitService } from './fixtures/git-service.mjs';
import { prepareAuth, authSelection } from '../scripts/git-setup/auth.mjs';
import { installCredentialHelper } from '../scripts/git-setup/credential-install.mjs';

const roots = [], url = 'https://github.com/team/project.git';
after(async () => {
  const base = resolve(process.env.GIT_SETUP_TEST_ROOT);
  for (const root of roots) {
    assert.ok(!relative(base, root).startsWith('..') && root !== base);
    await rm(root, { recursive: true, force: true });
  }
});

async function fixture(name = 'project with space') {
  const f = await repositoryFixture(); roots.push(f.root);
  const project = await f.repo(name);
  await f.git(project, 'remote', 'add', 'origin', url);
  return { ...f, project };
}

async function credential(f, target = url, options = {}) {
  const prepared = await prepareAuth(f.project, target, { ...options, apply: true });
  const value = target.includes('cnb.cool') ? 'https://cnb:synthetic-cnb@cnb.cool/team/project' : 'synthetic-project-token';
  await writeFile(prepared.credentialFile, value, { mode: 0o600 });
  return prepared.credentialFile;
}

function native(f, operation, body, project = f.project) {
  return new Promise((resolveNative, reject) => {
    const child = spawn('git', ['-C', project, 'credential', operation], { env: { ...f.env, GIT_ASKPASS: '', SSH_ASKPASS: '' }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('Native credential test timed out')); }, 30000);
    child.stdout.on('data', data => { stdout += data; }); child.stderr.on('data', data => { stderr += data; });
    child.stdin.on('error', () => {});
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', code => { clearTimeout(timer); resolveNative({ code, stdout, stderr }); });
    child.stdin.end(body);
  });
}

async function install(f, input = { targets: [url] }, options = {}) {
  const plan = await installCredentialHelper(f.project, input, options);
  const done = await installCredentialHelper(f.project, input, { ...options, apply: true, expectedFingerprint: plan.fingerprint, acceptEffects: true });
  assert.equal(done.complete, true, JSON.stringify(done));
  return { plan, done };
}

test('native Git uses a self-contained helper; preview is read-only and installation repeats safely', async () => {
  const f = await fixture(), file = await credential(f);
  const before = await readFile(join(f.project, '.git/config'));
  const plan = await installCredentialHelper(f.project, { targets: [url] });
  assert.deepEqual(plan.effects.otherWorktrees, [], 'the current checkout is not another worktree');
  assert.ok((await readFile(join(f.project, '.git/config'))).equals(before), 'preview changed config');
  await assert.rejects(lstat(plan.helper.path), { code: 'ENOENT' });
  const { done } = await install(f);
  assert.equal(done.credentialProtocol, 'verified'); assert.equal(done.remoteRead, 'not_performed'); assert.equal(done.ide, 'not_verified');
  const helper = await readFile(done.helper.path, 'utf8');
  assert.doesNotMatch(helper, /from ['"]\.\//);
  assert.equal(helper.includes('synthetic-project-token'), false);
  const result = await native(f, 'fill', `url=${url}\n\n`);
  assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /password=synthetic-project-token/);
  assert.equal((await readFile(file, 'utf8')), 'synthetic-project-token');
  const installed = await readFile(join(f.project, '.git/config'));
  const repeated = await install(f);
  assert.equal(repeated.plan.effects.replacedHelperEntries, 0);
  assert.equal(repeated.plan.requiresAcceptEffects, false);
  assert.ok((await readFile(join(f.project, '.git/config'))).equals(installed), 'repeat installation changed config');
  await writeFile(done.helper.path, helper.replaceAll('\n', '\r\n'));
  assert.equal((await installCredentialHelper(f.project, { targets: [url] })).status, 'planned');
});

test('rotation is immediate; approve/reject never change the credential and unknown targets cannot use a fallback', async () => {
  const f = await fixture(), file = await credential(f);
  const fallback = join(f.root, 'fallback-used').replaceAll('\\', '/');
  await f.git(f.project, 'config', 'credential.helper', `!f() { echo used > '${fallback}'; echo username=fallback; echo password=other; }; f`);
  const plan = await installCredentialHelper(f.project, { targets: [url] });
  assert.equal(plan.requiresAcceptEffects, true);
  await assert.rejects(installCredentialHelper(f.project, { targets: [url] }, { apply: true, expectedFingerprint: plan.fingerprint }), { code: 'auth_helper_effects' });
  await install(f);
  await writeFile(file, 'synthetic-rotated-token');
  const result = await native(f, 'fill', `url=${url}\ncapability[]=one\ncapability[]=two\nwwwauth[]=a\nwwwauth[]=b\n\n`);
  assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /password=synthetic-rotated-token/);
  for (const operation of ['approve', 'reject']) assert.equal((await native(f, operation, `url=${url}\nusername=x-access-token\npassword=irrelevant-synthetic\n\n`)).code, 0);
  assert.equal(await readFile(file, 'utf8'), 'synthetic-rotated-token');
  assert.notEqual((await native(f, 'fill', 'url=https://github.com/team/other.git\n\n')).code, 0);
  await rm(file);
  assert.notEqual((await native(f, 'fill', `url=${url}\n\n`)).code, 0);
  await assert.rejects(lstat(fallback), { code: 'ENOENT' });
});

test('a custom source is recorded once and shared by ordinary Git and Agent selection', async () => {
  const f = await fixture();
  const file = await credential(f, url, { authMethod: 'github-token', credentialFile: '.credentials/custom-token' });
  const input = { targets: [url], sources: [{ url, method: 'github-token', file: '.credentials/custom-token' }] };
  const { plan } = await install(f, input);
  assert.equal(plan.effects.sourceUpdates.length, 1);
  const record = await readFile(join(f.project, '.credentials/repositories/sources.local.md'), 'utf8');
  assert.equal(record.includes('synthetic-project-token'), false);
  assert.equal((await authSelection(f.project, url)).source.file, file);
  assert.equal((await native(f, 'fill', `url=${url}\n\n`)).code, 0);
  assert.equal((await installCredentialHelper(f.project, { targets: [url] })).effects.sourceUpdates.length, 0);
});

test('stale plans and user-owned helper files are preserved', async () => {
  const f = await fixture(), file = await credential(f);
  const plan = await installCredentialHelper(f.project, { targets: [url] });
  await writeFile(file, 'synthetic-new-token');
  await assert.rejects(installCredentialHelper(f.project, { targets: [url] }, { apply: true, expectedFingerprint: plan.fingerprint }), { code: 'auth_helper_changed' });
  await assert.rejects(lstat(plan.helper.path), { code: 'ENOENT' });
  await mkdir(join(f.project, 'scripts/git'), { recursive: true });
  await writeFile(plan.helper.path, '// user helper\n');
  await assert.rejects(installCredentialHelper(f.project, { targets: [url] }), { code: 'auth_helper_modified' });
  assert.equal(await readFile(plan.helper.path, 'utf8'), '// user helper\n');
});

test('URL-specific configuration and environment-only sources require a concrete resolution', async () => {
  const f = await fixture(); await credential(f);
  await f.git(f.project, 'config', 'credential.https://github.com.helper', 'manager');
  await assert.rejects(installCredentialHelper(f.project, { targets: [url] }), { code: 'auth_helper_configuration' });
  await f.git(f.project, 'config', '--unset-all', 'credential.https://github.com.helper');
  await assert.rejects(installCredentialHelper(f.project, { targets: [url], sources: [{ url, method: 'environment' }] }), { code: 'auth_helper_environment' });
  await f.git(f.project, 'config', 'http.https://github.com/team/project.extraHeader', 'Authorization: synthetic-conflict');
  await assert.rejects(installCredentialHelper(f.project, { targets: [url] }), { code: 'auth_http_header_conflict' });
});

test('registered repositories share storage only while the management relation remains valid', async () => {
  const f = await fixture('manager');
  const child = await f.repo('manager/child');
  await f.git(child, 'remote', 'add', 'origin', url);
  await registerRepositories(f.project, ['child']);
  const childFixture = { ...f, project: child };
  const options = { workspace: f.project, policyDirectory: 'fixture-git' };
  const file = await credential(childFixture, url, options);
  assert.ok(file.startsWith(join(f.project, '.credentials')));
  await install(childFixture, { targets: [url] }, options);
  assert.equal((await native(f, 'fill', `url=${url}\n\n`, child)).code, 0);
  await writeFile(join(f.project, 'fixture-git/repos.conf'), '');
  assert.notEqual((await native(f, 'fill', `url=${url}\n\n`, child)).code, 0);
  assert.equal(await readFile(file, 'utf8'), 'synthetic-project-token');
});

test('a registered submodule uses the manager source and loses access after its registration is removed', async () => {
  const f = await fixture('manager'), source = await f.repo('module-source');
  await f.git(f.project, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'module');
  const child = join(f.project, 'module');
  await f.git(child, 'remote', 'set-url', 'origin', url);
  const childFixture = { ...f, project: child }, options = { workspace: f.project };
  const file = await credential(childFixture, url, options);
  assert.ok(file.startsWith(join(f.project, '.credentials')));
  await install(childFixture, { targets: [url] }, options);
  assert.equal((await native(f, 'fill', `url=${url}\n\n`, child)).code, 0);
  await f.git(f.project, 'config', '--file', '.gitmodules', '--remove-section', 'submodule.module');
  assert.notEqual((await native(f, 'fill', `url=${url}\n\n`, child)).code, 0);
  assert.equal(await readFile(file, 'utf8'), 'synthetic-project-token');
});

test('CNB native Git uses its full-target file without credential-store erase semantics', async () => {
  const f = await fixture(), target = 'https://cnb.cool/team/project.git';
  await f.git(f.project, 'remote', 'add', 'backup', target);
  await credential(f); const file = await credential(f, target);
  await install(f, { targets: [url, target] });
  const result = await native(f, 'fill', `url=${target}\n\n`);
  assert.equal(result.code, 0, result.stderr); assert.match(result.stdout, /username=cnb/); assert.match(result.stdout, /password=synthetic-cnb/);
  await native(f, 'reject', `url=${target}\nusername=cnb\npassword=synthetic-cnb\n\n`);
  assert.equal(await readFile(file, 'utf8'), 'https://cnb:synthetic-cnb@cnb.cool/team/project');
});

for (const host of ['github.com', 'cnb.cool']) test(`ordinary Git reads and pushes to local HTTPS ${host} fixture without Agent authentication`, async t => {
  const f = await fixture(), target = `https://${host}/team/project.git`;
  await f.git(f.project, 'remote', 'set-url', 'origin', target);
  const file = await credential(f, target);
  const token = host === 'github.com' ? 'synthetic-project-token' : 'synthetic-cnb';
  const accepted = { 'team/project': token };
  const service = await gitService(t, f, accepted, { hosts: [host] });
  await install(f, { targets: [target] });
  assert.equal(await f.git(f.project, 'ls-remote', 'origin'), '');
  await f.git(f.project, 'push', '--set-upstream', 'origin', 'HEAD');
  const head = await f.git(f.project, 'rev-parse', 'HEAD');
  assert.match(await f.git(f.project, 'ls-remote', 'origin', 'refs/heads/main'), new RegExp('^' + head));
  assert.equal(await f.git(service.repositories['team/project'], 'rev-parse', 'refs/heads/main'), head);
  assert.ok(service.requests.some(request => request.path.endsWith('/git-receive-pack')));
  accepted['team/project'] = 'synthetic-rotated';
  await writeFile(file, host === 'github.com' ? 'synthetic-rotated' : 'https://cnb:synthetic-rotated@cnb.cool/team/project');
  assert.match(await f.git(f.project, 'ls-remote', 'origin', 'refs/heads/main'), new RegExp('^' + head));
  accepted['team/project'] = 'synthetic-rejected';
  await assert.rejects(f.git(f.project, 'ls-remote', 'origin'));
  assert.ok((await readFile(file, 'utf8')).includes('synthetic-rotated'));
});
