import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { createLocalIssue, createRemoteIssue, createRemoteRepository, recoverIssue, updateRemoteIssue } from '../scripts/git-setup/issues.mjs';

// Tests use workspace tmp/git-setup, never the operating system's temporary root.
const tempRoot = process.env.GIT_SETUP_TEST_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../tmp/git-setup');
async function fixture(t, { ignored = true } = {}) {
  await mkdir(tempRoot, { recursive: true });
  const root = await mkdtemp(path.join(tempRoot, 'issues-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  execFileSync('git', ['init', '-q', root]);
  await writeFile(path.join(root, '.gitignore'), ignored ? '/data/\n' : '');
  return root;
}
const local = root => ({ backend: 'local', configOwner: root, initiatingProject: root, targetProject: root, local: { root, directory: 'issues', filename: '{number}-{slug}.md', numbering: { start: 1, width: 3 } }, fingerprint: 'fixture' });
const remote = root => ({ backend: 'github', configOwner: root, initiatingProject: root, targetProject: root, github: { host: 'github.com', repository: 'example/project' }, fingerprint: 'fixture' });
const content = { title: '不能打开页面', body: '步骤：打开页面。\n预期：返回内容。' };
function issue(payload, number = 21) { return { ...payload, number, html_url: `https://github.com/example/project/issues/${number}`, repository_url: 'https://api.github.com/repos/example/project' }; }
function fakeClient({ failCreate = false, disabled = false } = {}) {
  const state = { creates: 0, issues: [] };
  const client = {
    host: 'github.com',
    getRepository: async () => ({ full_name: 'example/project', has_issues: !disabled }),
    createIssue: async (_repository, payload) => {
      state.creates++;
      const result = issue(payload);
      state.issues.push(result);
      if (failCreate) throw Object.assign(new Error('write timeout'), { code: 'ambiguous', ambiguous: true });
      return result;
    },
    getIssue: async (_repository, number) => state.issues.find(value => value.number === number),
    listIssues: async () => state.issues,
    updateIssue: async (_repository, number, fields) => Object.assign(state.issues.find(value => value.number === number), fields),
  };
  return { client, state };
}

test('local creation preserves template and returns explicit local message and actual path', async t => {
  const root = await fixture(t);
  const resolution = local(root);
  await writeFile(path.join(root, 'template.md'), '# {number} {title}\n\n状态：待处理\n\n{body}\n');
  resolution.local.template = path.join(root, 'template.md');
  const result = await createLocalIssue({ resolution, content, authorized: true });
  assert.equal(result.message, '已按当前项目配置将 Issue 保存到本地');
  assert.equal(path.basename(result.filePath), '001-不能打开页面.md');
  assert.match(await readFile(result.filePath, 'utf8'), /^# 001 不能打开页面\n\n状态：待处理/);
  const record = JSON.parse(await readFile(path.join(root, 'data/git-setup', `${result.operationId}.json`), 'utf8'));
  assert.equal(record.status, 'verified');
  assert.equal(JSON.stringify(record).includes(content.body), false);
  assert.throws(() => execFileSync('git', ['-C', root, 'rev-parse', '--verify', 'HEAD'], { stdio: 'pipe' }));
});

test('same operation is recoverable without duplicate local files', async t => {
  const root = await fixture(t), resolution = local(root), operationId = randomUUID();
  const first = await createLocalIssue({ resolution, content, operationId, authorized: true });
  const second = await createLocalIssue({ resolution, content, operationId, authorized: true });
  assert.equal(first.filePath, second.filePath);
  assert.equal((await readdir(path.join(root, 'issues'))).length, 1);
  await assert.rejects(createLocalIssue({ resolution, content: { ...content, body: 'changed' }, operationId, authorized: true }), { code: 'operation-mismatch' });
});

test('concurrent local calls never allocate the same file or overwrite existing content', async t => {
  const root = await fixture(t), resolution = local(root);
  const results = await Promise.allSettled(Array.from({ length: 5 }, () => createLocalIssue({ resolution, content, authorized: true })));
  assert.ok(results.some(value => value.status === 'fulfilled'));
  const successful = results.filter(value => value.status === 'fulfilled').map(value => value.value.filePath);
  assert.equal(new Set(successful).size, successful.length);
  for (const result of results.filter(value => value.status === 'rejected')) assert.equal(result.reason.code, 'operation-locked');
  const next = await createLocalIssue({ resolution, content, authorized: true });
  assert.equal(successful.includes(next.filePath), false);
  const original = await readFile(next.filePath, 'utf8');
  await assert.rejects(createLocalIssue({ resolution, content: { ...content, filename: path.basename(next.filePath), body: 'replace' }, authorized: true }), { code: 'file-exists' });
  assert.equal(await readFile(next.filePath, 'utf8'), original);
});

test('local paths reject traversal, symlink escape and missing filename policy', async t => {
  const root = await fixture(t), resolution = local(root);
  await assert.rejects(createLocalIssue({ resolution, content: { ...content, filename: '../escape.md' }, authorized: true }), { code: 'unsafe-path' });
  resolution.local.directory = '../outside';
  await assert.rejects(createLocalIssue({ resolution, content, authorized: true }), { code: 'unsafe-path' });
  resolution.local.directory = 'linked';
  await symlink(root, path.join(root, 'linked'));
  await assert.rejects(createLocalIssue({ resolution, content, authorized: true }), { code: 'unsafe-path' });
  resolution.local.directory = 'issues'; delete resolution.local.filename;
  await assert.rejects(createLocalIssue({ resolution, content, authorized: true }), { code: 'filename-required' });
});

test('journals require an ignored untracked path and do not silently change gitignore', async t => {
  const root = await fixture(t, { ignored: false });
  await assert.rejects(createLocalIssue({ resolution: local(root), content, authorized: true }), { code: 'journal-not-ignored' });
  assert.equal(await readFile(path.join(root, '.gitignore'), 'utf8'), '');
  await writeFile(path.join(root, '.gitignore'), '/data/\n');
  await mkdir(path.join(root, 'data/git-setup'), { recursive: true });
  await writeFile(path.join(root, 'data/git-setup', 'tracked.json'), '{}');
  execFileSync('git', ['-C', root, 'add', '-f', 'data/git-setup/tracked.json']);
  await assert.rejects(createLocalIssue({ resolution: local(root), content, authorized: true }), { code: 'journal-tracked' });
});

test('journal checks reject partial ignore rules and cannot be bypassed with an alternate Git index', async t => {
  const root = await fixture(t);
  await writeFile(path.join(root, '.gitignore'), '/data/git-setup/*.json\n');
  await assert.rejects(createLocalIssue({ resolution: local(root), content, authorized: true }), { code: 'journal-not-ignored' });
  await writeFile(path.join(root, '.gitignore'), '/data/\n');
  await mkdir(path.join(root, 'data/git-setup'), { recursive: true });
  await writeFile(path.join(root, 'data/git-setup/tracked.json'), '{}');
  execFileSync('git', ['-C', root, 'add', '-f', 'data/git-setup/tracked.json']);
  const original = process.env.GIT_INDEX_FILE;
  process.env.GIT_INDEX_FILE = path.join(root, 'alternate-index');
  try { await assert.rejects(createLocalIssue({ resolution: local(root), content, authorized: true }), { code: 'journal-tracked' }); }
  finally { if (original === undefined) delete process.env.GIT_INDEX_FILE; else process.env.GIT_INDEX_FILE = original; }
});

test('local partial content or later edits do not report successful recovery', async t => {
  const root = await fixture(t), resolution = local(root);
  const result = await createLocalIssue({ resolution, content, authorized: true });
  await writeFile(result.filePath, '# partial');
  await assert.rejects(recoverIssue({ resolution, operationId: result.operationId }), { code: 'local-content-mismatch' });
});

test('remote create uses the resolved target once, journals hashes and verifies content', async t => {
  const root = await fixture(t), resolution = remote(root), { client, state } = fakeClient();
  const result = await createRemoteIssue({ resolution, content, client, authorized: true });
  assert.equal(result.url, 'https://github.com/example/project/issues/21');
  assert.equal(state.creates, 1);
  const record = await readFile(path.join(root, 'data/git-setup', `${result.operationId}.json`), 'utf8');
  assert.equal(record.includes(content.title), false); assert.equal(record.includes(content.body), false);
  const again = await createRemoteIssue({ resolution, content, client, authorized: true, operationId: result.operationId });
  assert.equal(again.recovered, true); assert.equal(state.creates, 1);
});

test('ambiguous remote create recovers by marker across calls without repeating POST', async t => {
  const root = await fixture(t), resolution = remote(root), { client, state } = fakeClient({ failCreate: true });
  const operationId = randomUUID();
  await assert.rejects(createRemoteIssue({ resolution, content, operationId, client, authorized: true }), { code: 'ambiguous' });
  const recovered = await recoverIssue({ resolution, operationId, client });
  assert.equal(recovered.status, 'verified'); assert.equal(state.creates, 1);
});

test('one empty query, same title, duplicate markers or edited body cannot authorize repost', async t => {
  const root = await fixture(t), resolution = remote(root), { client, state } = fakeClient({ failCreate: true });
  const operationId = randomUUID();
  await assert.rejects(createRemoteIssue({ resolution, content, operationId, client, authorized: true }));
  const created = state.issues[0];
  state.issues = [issue(content, 99)];
  const empty = await recoverIssue({ resolution, operationId, client });
  assert.equal(empty.status, 'ambiguous'); assert.equal(empty.retryAllowed, false);
  state.issues = [created, issue(created, 23)];
  assert.equal((await recoverIssue({ resolution, operationId, client })).status, 'multiple-matches');
  state.issues = [{ ...created, body: `${created.body}\nChanged` }];
  await assert.rejects(recoverIssue({ resolution, operationId, client }), { code: 'issue-content-mismatch' });
  assert.equal(state.creates, 1);
});

test('disabled issues and resolved target changes block writes and recovery', async t => {
  const root = await fixture(t), resolution = remote(root), { client, state } = fakeClient({ disabled: true });
  await assert.rejects(createRemoteIssue({ resolution, content, client, authorized: true }), { code: 'issues-unavailable' });
  assert.equal(state.creates, 0);
  const enabled = fakeClient();
  const result = await createRemoteIssue({ resolution, content, client: enabled.client, authorized: true });
  resolution.github.repository = 'example/other';
  await assert.rejects(recoverIssue({ resolution, operationId: result.operationId, client: enabled.client }), { code: 'operation-target-changed' });
});

test('update remains at the explicit original issue and rejects pull requests', async t => {
  const root = await fixture(t), resolution = remote(root), { client, state } = fakeClient();
  state.issues = [issue(content)];
  const result = await updateRemoteIssue({ resolution, number: 21, content: { body: 'new body', state: 'closed' }, client, authorized: true });
  assert.equal(result.number, 21); assert.equal(state.creates, 0);
  state.issues[0].pull_request = {};
  await assert.rejects(updateRemoteIssue({ resolution, number: 21, content: { body: 'do not update PR' }, client, authorized: true }), { code: 'invalid-issue-response' });
});

test('remote repository creation has its own authorization and immutable operation record', async t => {
  const root = await fixture(t);
  let creates = 0;
  const repository = { id: 100, full_name: 'example/project', private: true, html_url: 'https://github.com/example/project' };
  const client = { host: 'github.com', createRepository: async () => { creates++; return { status: 'created', repository }; }, getRepository: async () => repository };
  await assert.rejects(createRemoteRepository({ projectRoot: root, owner: 'example', name: 'project', private: true, client }), { code: 'authorization-required' });
  const result = await createRemoteRepository({ projectRoot: root, owner: 'example', name: 'project', private: true, client, authorized: true });
  assert.equal(result.status, 'verified'); assert.equal(creates, 1);
  const recovered = await createRemoteRepository({ projectRoot: root, owner: 'example', name: 'project', private: true, client, authorized: true, operationId: result.operationId });
  assert.equal(recovered.recovered, true); assert.equal(creates, 1);
});
