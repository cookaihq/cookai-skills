import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { inspectProject, loadProject, writeProject, resolveIssue, loadEnvironment } from '../scripts/git-setup/config.mjs';

const exec = promisify(execFile);
const workspace = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const tempBase = process.env.GIT_SETUP_TEST_TMP ?? path.join(workspace, 'tmp/git-setup');
const github = { backend: 'github', target: 'specified', github: { host: 'github.com', repository: 'example/issues' } };
const local = { backend: 'local', target: 'current', local: { directory: 'tickets', filename: '{number}-{slug}.md', numbering: { start: 1, width: 2 } } };
const config = issues => ({ schemaVersion: 1, issues });
const code = expected => error => error.code === expected;

async function fixture(t) {
  await fs.mkdir(tempBase, { recursive: true });
  const root = await fs.mkdtemp(path.join(tempBase, 'config-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return root;
}

async function git(cwd, ...args) {
  return (await exec('git', ['-C', cwd, ...args], { encoding: 'utf8', timeout: 15000, env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } })).stdout.trim();
}

async function repository(directory) {
  await fs.mkdir(directory, { recursive: true });
  await git(directory, 'init', '--quiet', '--initial-branch=main');
  await git(directory, 'config', 'user.name', 'Git setup fixture');
  await git(directory, 'config', 'user.email', 'fixture@example.invalid');
  await git(directory, 'config', 'core.hooksPath', path.join(directory, '.fixture-no-hooks'));
  await fs.writeFile(path.join(directory, 'README.md'), 'Fixture\n');
  await git(directory, 'add', 'README.md');
  await git(directory, 'commit', '--quiet', '-m', 'fixture');
  return directory;
}

test('write shared config without staging, commits or credential fields', async t => {
  const root = await repository(await fixture(t));
  const head = await git(root, 'rev-parse', 'HEAD');
  await writeProject(root, config(local));
  assert.deepEqual((await loadProject(root)).config, config(local));
  assert.equal(await git(root, 'rev-parse', 'HEAD'), head);
  assert.equal(await git(root, 'diff', '--cached', '--name-only'), '');
  await assert.rejects(writeProject(root, { ...config(local), token: 'secret' }), code('CONFIG_INVALID'));
  await assert.rejects(writeProject(root, { ...config(local), remotes: { origin: 'https://secret@example.com/o/r.git' } }), code('CONFIG_SECRET'));
});

test('an ordinary subdirectory remains part of its parent Git repository', async t => {
  const root = await repository(await fixture(t));
  const child = path.join(root, 'not-a-repository');
  await fs.mkdir(child);
  const result = await inspectProject(child);
  assert.equal(result.root, root);
  assert.equal(result.requestedPath, child);
  assert.equal(result.isGit, true);
  await assert.rejects(fs.stat(path.join(child, '.git')), error => error.code === 'ENOENT');
});

test('ordinary nesting is not a workspace relationship', async t => {
  const root = await repository(await fixture(t));
  await writeProject(root, config(github));
  const child = await repository(path.join(root, 'child'));
  await writeProject(child, config(local));
  assert.equal((await resolveIssue(child, { env: {} })).backend, 'local');
  await assert.rejects(resolveIssue(child, { env: {}, workspace: root }), code('WORKSPACE_UNREGISTERED'));
});

test('registered children either inherit or use their independent complete configuration', async t => {
  const root = await repository(await fixture(t));
  const a = await repository(path.join(root, 'a'));
  const b = await repository(path.join(root, 'b'));
  await writeProject(root, { ...config(github), workspace: { repositories: [{ path: 'a', issues: 'independent' }, { path: 'b', issues: 'inherit' }] } });
  await writeProject(a, config(local));
  const independent = await resolveIssue(a, { workspace: root, env: {} });
  const inherited = await resolveIssue(b, { workspace: root, env: {} });
  assert.equal(independent.backend, 'local');
  assert.equal(independent.configOwner, a);
  assert.equal(inherited.configOwner, root);
  assert.deepEqual(inherited.github, github.github);
  await fs.rm(path.join(a, '.git-setup/project.json'));
  await assert.rejects(resolveIssue(a, { workspace: root, env: {} }), code('INDEPENDENT_CONFIG_MISSING'));
  await fs.writeFile(path.join(a, '.git-setup/project.json'), '{broken');
  await assert.rejects(resolveIssue(a, { workspace: root, env: {} }), code('CONFIG_INVALID_JSON'));
  await writeProject(a, config(local));
  assert.equal((await resolveIssue(a, { workspace: root, env: {} })).backend, 'local');
});

test('inherited current means initiating child; outer resolves the registered workspace', async t => {
  const root = await repository(await fixture(t));
  const child = await repository(path.join(root, 'child'));
  const shared = { ...config(local), workspace: { repositories: [{ path: 'child', issues: 'inherit' }] } };
  await writeProject(root, shared);
  const current = await resolveIssue(child, { workspace: root, env: {} });
  assert.equal(current.configOwner, root);
  assert.equal(current.targetProject, child);
  assert.equal(current.local.root, child);
  await writeProject(root, { ...shared, issues: { ...local, target: 'outer' } });
  assert.equal((await resolveIssue(child, { workspace: root, env: {} })).local.root, root);
});

test('project limits survive child configuration and request overrides', async t => {
  const root = await repository(await fixture(t));
  const child = await repository(path.join(root, 'child'));
  await writeProject(child, config(local));
  await writeProject(root, { ...config(local), workspace: { repositories: [{ path: 'child', issues: 'independent' }], issuePolicy: { allowIndependent: false } } });
  await assert.rejects(resolveIssue(child, { workspace: root, env: {} }), code('ISSUE_POLICY_CONFLICT'));
  await writeProject(root, { ...config(local), workspace: { repositories: [{ path: 'child', issues: 'inherit' }], issuePolicy: { backend: 'local' } } });
  await assert.rejects(resolveIssue(child, { workspace: root, env: {}, issueOverride: github }), code('ISSUE_POLICY_CONFLICT'));
  await assert.rejects(resolveIssue(root, { env: {}, issueOverride: github }), code('ISSUE_POLICY_CONFLICT'));
});

test('environment precedence is explicit, does not source shell, and replaces whole issue records', async t => {
  const root = await repository(await fixture(t));
  await writeProject(root, config(local));
  await fs.writeFile(path.join(root, '.env'), `GIT_SETUP_ISSUE_JSON='${JSON.stringify(github)}'\nUNRELATED_SECRET=ignored\n`);
  const envLocal = { ...local, local: { directory: 'local-tickets' } };
  await fs.writeFile(path.join(root, '.env.local'), `GIT_SETUP_ISSUE_JSON='${JSON.stringify(envLocal)}'\n`);
  await fs.writeFile(path.join(root, '.env.git-setup'), `GIT_SETUP_ISSUE_JSON=\nGIT_SETUP_ISSUE_JSON='${JSON.stringify(github)}'\n`);
  assert.equal((await resolveIssue(root, { env: {} })).backend, 'github');
  assert.equal((await resolveIssue(root, { env: { GIT_SETUP_ISSUE_JSON: JSON.stringify(envLocal) } })).local.directory, 'local-tickets');
  await assert.rejects(resolveIssue(root, { env: { GIT_SETUP_ISSUE_JSON: '{"backend":"local"}' } }), code('CONFIG_INVALID'));
  const result = await loadEnvironment(root, { env: {}, issueOnly: true });
  assert.deepEqual(Object.keys(result.values), ['GIT_SETUP_ISSUE_JSON']);
  assert.equal(result.sources.GIT_SETUP_ISSUE_JSON, path.join(root, '.env.git-setup'));
  await fs.writeFile(path.join(root, '.env.git-setup'), 'GIT_SETUP_ISSUE_JSON=$(touch should-never-exist)\n');
  await assert.rejects(resolveIssue(root, { env: {} }), code('CONFIG_INVALID_JSON'));
  await assert.rejects(fs.stat(path.join(root, 'should-never-exist')), error => error.code === 'ENOENT');
});

test('global config is opt-in and does not beat project environment; parent env is not scanned', async t => {
  const root = await fixture(t);
  const child = await repository(path.join(root, 'child'));
  const globalConfigPath = path.join(root, 'personal.env');
  await fs.writeFile(globalConfigPath, `GIT_SETUP_ISSUE_JSON='${JSON.stringify(github)}'\n`);
  await fs.writeFile(path.join(root, '.env'), `GIT_SETUP_ISSUE_JSON='${JSON.stringify(local)}'\n`);
  await assert.rejects(resolveIssue(child, { env: {}, globalConfigPath }), code('ISSUE_CONFIG_REQUIRED'));
  assert.equal((await resolveIssue(child, { env: {}, globalConfigPath, useGlobalConfig: true })).backend, 'github');
  await fs.writeFile(path.join(child, '.env.local'), `GIT_SETUP_ISSUE_JSON='${JSON.stringify(local)}'\n`);
  assert.equal((await resolveIssue(child, { env: {}, globalConfigPath, useGlobalConfig: true })).backend, 'local');
});

test('token host and repository bind to a single source; local resolution ignores unused auth errors', async t => {
  const root = await repository(await fixture(t));
  await writeProject(root, config(local));
  await fs.writeFile(path.join(root, '.env.local'), 'GIT_SETUP_GITHUB_TOKEN=fixture-only\nGIT_SETUP_GITHUB_HOST=github.com\nGIT_SETUP_GITHUB_REPOSITORY=example/issues\n');
  const loaded = await loadEnvironment(root, { env: {} });
  assert.equal(loaded.values.GIT_SETUP_GITHUB_AUTH, 'token');
  await assert.rejects(loadEnvironment(root, { env: { GIT_SETUP_GITHUB_TOKEN: 'different-fixture' } }), code('CREDENTIAL_GROUP_INCOMPLETE'));
  assert.equal((await resolveIssue(root, { env: { GIT_SETUP_GITHUB_TOKEN: 'different-fixture' } })).backend, 'local');
  const gh = await loadEnvironment(root, { env: { GIT_SETUP_GITHUB_AUTH: 'gh' } });
  assert.equal(gh.values.GIT_SETUP_GITHUB_TOKEN, undefined);
});

test('explicit project rules work only as fallback and caller override never persists', async t => {
  const root = await repository(await fixture(t));
  const fallback = await resolveIssue(root, { env: {}, projectRuleIssue: local });
  assert.equal(fallback.source.kind, 'project-rule');
  await writeProject(root, config(github));
  assert.equal((await resolveIssue(root, { env: {}, projectRuleIssue: local })).backend, 'github');
  assert.equal((await resolveIssue(root, { env: {}, issueOverride: local })).backend, 'local');
  assert.equal((await loadProject(root)).config.issues.backend, 'github');
});

test('GitHub identity requires unique repository, never arbitrary origin preference', async t => {
  const root = await repository(await fixture(t));
  await writeProject(root, config({ backend: 'github', target: 'current' }));
  await git(root, 'remote', 'add', 'origin', 'git@github.com:example/code.git');
  assert.equal((await resolveIssue(root, { env: {} })).github.repository, 'example/code');
  await git(root, 'remote', 'add', 'mirror', 'https://github.com/example/other.git');
  await assert.rejects(resolveIssue(root, { env: {} }), code('GITHUB_TARGET_AMBIGUOUS'));
  await writeProject(root, { ...config({ backend: 'github', target: 'current' }), repository: { host: 'github.com', repository: 'example/code' } });
  assert.equal((await resolveIssue(root, { env: {} })).github.repository, 'example/code');
});

test('actual submodule relationship inherits without parent directory scanning', async t => {
  const root = await fixture(t);
  const source = await repository(path.join(root, 'source'));
  const outer = await repository(path.join(root, 'outer'));
  await writeProject(outer, config({ ...local, target: 'outer' }));
  await git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', '--quiet', source, 'child');
  const child = path.join(outer, 'child');
  const result = await resolveIssue(child, { env: {} });
  assert.equal(result.configOwner, outer);
  assert.equal(result.relationship.evidence, '.gitmodules');
  assert.equal(result.local.root, outer);
});

test('worktree identity matches a registered child while keeping current checkout separate', async t => {
  const root = await repository(await fixture(t));
  const child = await repository(path.join(root, 'child'));
  const linked = path.join(root, 'linked');
  await writeProject(root, { ...config(local), workspace: { repositories: [{ path: 'child', issues: 'inherit' }] } });
  await git(child, 'worktree', 'add', '--quiet', '--detach', linked, 'HEAD');
  const info = await inspectProject(linked);
  assert.equal(info.isWorktree, true);
  const result = await resolveIssue(linked, { workspace: root, env: {} });
  assert.equal(result.configOwner, root);
  assert.equal(result.local.root, linked);
});

test('local path cannot escape via traversal or symlink, and templates use config-owner base', async t => {
  const root = await repository(await fixture(t));
  await fs.writeFile(path.join(root, 'issue-template.md'), '# Issue\n');
  await writeProject(root, config({ ...local, local: { ...local.local, template: 'issue-template.md' } }));
  const before = await resolveIssue(root, { env: {} });
  assert.equal(before.local.template, path.join(root, 'issue-template.md'));
  await fs.writeFile(path.join(root, 'issue-template.md'), '# Changed\n');
  assert.notEqual((await resolveIssue(root, { env: {} })).fingerprint, before.fingerprint);
  await assert.rejects(writeProject(root, config({ ...local, local: { directory: '../outside' } })), code('CONFIG_INVALID'));
  const outside = await fixture(t);
  await fs.symlink(outside, path.join(root, 'tickets'), 'dir');
  await assert.rejects(resolveIssue(root, { env: {} }), code('PATH_OUTSIDE_PROJECT'));
});

test('specified local issues support an explicitly configured sibling repository', async t => {
  const base = await fixture(t);
  const source = await repository(path.join(base, 'source'));
  const tickets = await repository(path.join(base, 'issues'));
  await writeProject(source, config({ backend: 'local', target: 'specified', local: { directory: 'tickets', checkout: '../issues' } }));
  const result = await resolveIssue(source, { env: {} });
  assert.equal(result.configOwner, source);
  assert.equal(result.targetProject, tickets);
  assert.equal(result.local.root, tickets);
  await fs.mkdir(path.join(tickets, 'subdirectory'));
  await writeProject(source, config({ backend: 'local', target: 'specified', local: { directory: 'tickets', checkout: '../issues/subdirectory' } }));
  await assert.rejects(resolveIssue(source, { env: {} }), code('LOCAL_CHECKOUT_INVALID'));
});

test('resolution fingerprint changes when effective default or workspace restriction changes', async t => {
  const root = await repository(await fixture(t));
  await writeProject(root, config(local));
  const first = await resolveIssue(root, { env: {} });
  assert.equal((await resolveIssue(root, { env: {} })).fingerprint, first.fingerprint);
  await writeProject(root, config(github));
  assert.notEqual((await resolveIssue(root, { env: {} })).fingerprint, first.fingerprint);
});

test('legacy remote URL secrets are redacted in inspection output', async t => {
  const root = await repository(await fixture(t));
  await git(root, 'remote', 'add', 'origin', 'https://fixture-secret@github.com/example/code.git');
  const output = JSON.stringify(await inspectProject(root));
  assert.equal(output.includes('fixture-secret'), false);
  assert.equal(output.includes('[redacted]'), true);
});
