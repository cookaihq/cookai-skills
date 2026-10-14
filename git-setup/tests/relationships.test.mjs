import { issueInput, inputFile } from './fixtures/issue-input.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, lstat, mkdir, cp, rename } from 'node:fs/promises';
import { join } from 'node:path';
import { repositoryFixture, cli, skill } from './fixtures/repositories.mjs';
import { gitService } from './fixtures/git-service.mjs';

test('public check and auth use the registered independent repository and its primary checkout', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer'), child = await f.repo('outer/children/api');
  await f.install(outer);
  await assert.rejects(lstat(join(outer, 'vcs/repos.conf')), { code: 'ENOENT' });
  await writeFile(join(outer, 'vcs/repos.conf'), 'children/api\n');
  const checkout = join(f.root, 'api-worktree');
  await f.git(child, 'worktree', 'add', '-b', 'fixture-worktree', checkout);
  const args = ['--project', checkout, '--workspace', outer, '--harness-directory', 'vcs'];
  const checked = await f.run(cli, ['check', ...args]);
  assert.equal(checked.relationship?.registration.kind, 'independent', JSON.stringify(checked));
  assert.equal(checked.relationship.project.root, checkout);
  assert.equal(checked.relationship.project.main, child);
  assert.equal(checked.relationship.management.main, outer);
  const prepared = await f.run(cli, ['auth-prepare', ...args, '--url', 'https://cnb.cool/team/api', '--auth-method', 'cnb-url', '--apply']);
  assert.equal(prepared.status, 'awaiting_credentials', JSON.stringify(prepared));
  assert.equal(prepared.credentialFile, join(outer, '.credentials/repositories/team--api.cnb-url'));
  assert.equal(await readFile(prepared.credentialFile, 'utf8'), '');
  const choice = await f.run(cli, ['auth-info', '--project', checkout, '--url', 'https://cnb.cool/team/api']);
  assert.deepEqual(choice.required, ['credentials-filled']);
  const resumedCheck = await f.run(cli, ['check', '--project', checkout]);
  assert.equal(resumedCheck.relationship?.management.main, outer, JSON.stringify(resumedCheck));
  await writeFile(join(outer, 'vcs/repos.conf'), '');
  const removed = await f.run(cli, ['auth-info', '--project', checkout, '--url', 'https://cnb.cool/team/api']);
  assert.equal(removed.exit, 1);
  assert.equal(removed.code, 'WORKSPACE_UNREGISTERED');
});

test('regenerating a registered manager preserves its independent repository list', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer');
  await f.repo('outer/child');
  await f.install(outer);
  const registry = '# Owned by this project\nchild\n';
  await writeFile(join(outer, 'vcs/repos.conf'), registry);
  const checked = await f.run(cli, ['check', '--project', outer]);
  assert.equal(checked.harness.installed.status, 'installed', JSON.stringify(checked));
  await f.install(outer);
  assert.equal(await readFile(join(outer, 'vcs/repos.conf'), 'utf8'), registry);
  assert.equal((await f.run(cli, ['check', '--project', join(outer, 'child'), '--workspace', outer])).relationship.registration.kind, 'independent');
  const key = await f.git(outer, 'config', '--name-only', '--get-regexp', '^gitsetup.harness-');
  await f.git(outer, 'config', '--unset-all', key);
  const operation = await issueInput(outer);
  operation.initiatingProject = join(outer, 'child');
  operation.relationship = { workspace: outer, kind: 'independent', path: 'child', harnessDirectory: 'vcs' };
  operation.target.scope = 'outer';
  const resolved = await f.run(cli, ['issue-resolve', '--project', operation.initiatingProject, '--input', await inputFile(f, operation)]);
  assert.equal(resolved.local?.root, outer, JSON.stringify(resolved));
});

test('public setup installs independent sources for native ls-remote, fetch and push after the Skill moves', async t => {
  const f = await repositoryFixture(), outer = await f.repo('outer'), first = await f.repo('outer/first'), second = await f.repo('outer/second');
  await f.install(outer);
  await writeFile(join(outer, 'vcs/repos.conf'), 'first\nsecond\n');
  const service = await gitService(t, f, { 'team/first': 'first-fixture-token', 'team/second': 'second-fixture-token' });
  const copy = join(f.root, 'skill-copy'); await cp(skill, copy, { recursive: true });
  const entry = join(copy, 'scripts/git-setup/cli.mjs');
  const sources = [];
  for (const [project, name, token] of [[first, 'first', 'first-fixture-token'], [second, 'second', 'second-fixture-token']]) {
    const url = `https://cnb.cool/team/${name}.git`;
    const prepared = await f.run(entry, ['auth-prepare', '--project', project, '--workspace', outer, '--url', url, '--auth-method', 'cnb-url', '--apply']);
    assert.equal(prepared.status, 'awaiting_credentials', JSON.stringify(prepared));
    sources.push(prepared.credentialFile);
    await writeFile(prepared.credentialFile, `https://cnb:${token}@cnb.cool/team/${name}\n`);
    const args = ['--project', project, '--url', url, '--credentials-filled'];
    const plan = await f.run(entry, ['setup', ...args]);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    assert.equal(plan.relationship.management.main, outer);
    const result = await f.run(entry, ['setup', ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
    assert.equal(result.status, 'configured', JSON.stringify(result));
    assert.equal(result.pushed, false); assert.equal(result.committed, false);
  }
  assert.notEqual(sources[0], sources[1]);
  await rename(copy, `${copy}-removed`);
  for (const [project, name] of [[first, 'first'], [second, 'second']]) {
    assert.equal(await f.git(project, 'ls-remote', 'origin'), '');
    await f.git(project, 'fetch', 'origin');
    await f.git(project, 'push', 'origin', 'refs/heads/main:refs/heads/main');
    assert.equal(await f.git(service.repositories[`team/${name}`], 'rev-parse', 'main'), await f.git(project, 'rev-parse', 'main'));
  }
  const count = service.requests.length;
  await writeFile(join(outer, 'vcs/repos.conf'), 'second\n');
  await assert.rejects(f.git(first, 'ls-remote', 'origin'));
  assert.equal(service.requests.length, count);
  assert.ok((await f.git(second, 'ls-remote', 'origin')).includes(await f.git(second, 'rev-parse', 'main')));
  assert.equal((await readFile(join(first, '.git/config'), 'utf8')).includes('first-fixture-token'), false);
});

test('public relationship checks reject legacy registration, duplicate paths and same-URL clones', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer'), child = await f.repo('outer/child'), clone = await f.repo('clone');
  for (const repo of [child, clone]) await f.git(repo, 'remote', 'add', 'origin', 'https://cnb.cool/team/shared');
  await f.git(outer, 'config', '--add', 'gitsetup.repository', 'child');
  await mkdir(join(outer, '.git-setup'));
  await writeFile(join(outer, '.git-setup/project.json'), JSON.stringify({ schemaVersion: 1, workspace: { repositories: [{ path: 'child', issues: 'inherit' }] } }));
  const args = ['--workspace', outer];
  assert.equal((await f.run(cli, ['check', '--project', child, ...args])).code, 'WORKSPACE_UNREGISTERED');
  await f.install(outer);
  await writeFile(join(outer, 'vcs/repos.conf'), 'child\n');
  assert.equal((await f.run(cli, ['check', '--project', clone, ...args])).code, 'WORKSPACE_UNREGISTERED');
  await writeFile(join(outer, 'vcs/repos.conf'), 'child\nchild\n');
  assert.equal((await f.run(cli, ['auth-prepare', '--project', child, ...args, '--url', 'https://cnb.cool/team/shared', '--auth-method', 'cnb-url', '--apply'])).code, 'WORKSPACE_DUPLICATE');
  await assert.rejects(lstat(join(outer, '.credentials')), { code: 'ENOENT' });
  await writeFile(join(outer, 'vcs/repos.conf'), 'missing\n');
  assert.equal((await f.run(cli, ['check', '--project', child, ...args])).code, 'WORKSPACE_REPOSITORY_MISSING');
});

test('a normal repository beneath a modules directory remains an independent repository', async () => {
  const f = await repositoryFixture(), project = await f.repo('modules/api');
  const result = await f.run(cli, ['check', '--project', project]);
  assert.equal(result.relationship?.registration.kind, 'self', JSON.stringify(result));
  const separate = join(f.root, 'storage/modules/api');
  await mkdir(join(f.root, 'storage/modules'), { recursive: true });
  await f.git(project, 'init', '--separate-git-dir', separate);
  const moved = await f.run(cli, ['check', '--project', project]);
  assert.equal(moved.relationship?.registration.kind, 'self', JSON.stringify(moved));
});

test('public submodule checks require registration, gitlink and actual Git metadata together', async () => {
  const f = await repositoryFixture(), source = await f.repo('source'), outer = await f.repo('outer');
  await f.git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'module');
  const child = join(outer, 'module');
  const checked = await f.run(cli, ['check', '--project', child]);
  assert.equal(checked.relationship?.registration.kind, 'submodule', JSON.stringify(checked));
  assert.equal(checked.relationship.management.main, outer);
  assert.equal(checked.relationship.registration.gitlink, await f.git(child, 'rev-parse', 'HEAD'));
  const linked = join(f.root, 'linked');
  await f.git(child, 'worktree', 'add', '-b', 'linked-module', linked);
  const worktree = await f.run(cli, ['auth-prepare', '--project', linked, '--url', 'https://cnb.cool/team/module', '--auth-method', 'cnb-url']);
  assert.equal(worktree.binding?.owner.main, outer, JSON.stringify(worktree));
  await f.git(outer, 'rm', '--cached', 'module');
  assert.equal((await f.run(cli, ['check', '--project', child])).code, 'WORKSPACE_SUBMODULE_INVALID');
  await f.git(outer, 'update-index', '--add', '--cacheinfo', `160000,${await f.git(child, 'rev-parse', 'HEAD')},module`);
  await f.git(outer, 'submodule', 'deinit', '-f', 'module');
  assert.equal((await f.run(cli, ['check', '--project', outer, '--workspace', outer])).status, 'checked');
  assert.equal((await f.run(cli, ['check', '--project', source, '--workspace', outer])).code, 'WORKSPACE_SUBMODULE_UNINITIALIZED');
});

test('multiple verified managers require a choice and setup rejects a removed registration before writes', async () => {
  const f = await repositoryFixture(), source = await f.repo('source'), outer = await f.repo('outer');
  await f.git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'middle');
  const middle = join(outer, 'middle');
  await f.git(middle, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'leaf');
  const leaf = join(middle, 'leaf');
  assert.equal((await f.run(cli, ['check', '--project', leaf])).code, 'WORKSPACE_AMBIGUOUS');
  const chosen = await f.run(cli, ['check', '--project', leaf, '--workspace', outer]);
  assert.equal(chosen.relationship?.management.main, outer, JSON.stringify(chosen));
  assert.equal(chosen.relationship.registration.kind, 'submodule-chain');

  const manager = await f.repo('manager'), child = await f.repo('manager/child');
  await f.install(manager); await writeFile(join(manager, 'vcs/repos.conf'), 'child\n');
  const target = join(f.root, 'target.git'); await f.git(f.root, 'init', '--bare', '-q', target);
  const args = ['--project', child, '--workspace', manager, '--url', target];
  const plan = await f.run(cli, ['setup', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  const before = await readFile(join(child, '.git/config'));
  await writeFile(join(manager, 'vcs/repos.conf'), '');
  const rejected = await f.run(cli, ['setup', ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(rejected.code, 'WORKSPACE_UNREGISTERED');
  assert.deepEqual(await readFile(join(child, '.git/config')), before);
});
