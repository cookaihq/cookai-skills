import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { access, mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { auditHistory, auditSnapshot } from '../scripts/git-setup/audit.mjs';

const base = resolve(process.env.GIT_SETUP_TEST_ROOT || 'tmp/git-setup/audit-tests');
function git(cwd, ...args) {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_CEILING_DIRECTORIES: base, LC_ALL: 'C' };
  for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE']) delete env[key];
  const result = spawnSync('git', ['-C', cwd, '-c', 'commit.gpgsign=false', ...args], { encoding: 'utf8', timeout: 20_000, env });
  assert.equal(result.status, 0, result.stderr || String(result.error));
  return result.stdout.trim();
}
before(async () => {
  let existing = dirname(base);
  while (true) { try { await access(existing); break; } catch { existing = dirname(existing); } }
  assert.equal(spawnSync('git', ['-C', existing, 'check-ignore', '--quiet', '--', base], { timeout: 15_000 }).status, 0);
  await mkdir(base, { recursive: true });
});
async function fixture() {
  const root = await mkdtemp(join(base, 'audit-'));
  git(root, 'init', '--initial-branch=main');
  git(root, 'config', 'user.name', 'Audit Test');
  git(root, 'config', 'user.email', 'audit@example.invalid');
  return root;
}
async function commit(root, content, message = 'fixture') {
  await writeFile(join(root, 'entry.txt'), content);
  git(root, 'add', 'entry.txt');
  git(root, 'commit', '-m', message);
  return git(root, 'rev-parse', 'HEAD');
}

test('audit returns the exact reviewed ref snapshot for clean history', async () => {
  const root = await fixture();
  const head = await commit(root, 'ordinary content\n');
  const result = auditHistory(root, ['refs/heads/main']);
  assert.equal(result.complete, true);
  assert.equal(result.status, 'checked');
  assert.equal(result.snapshot['refs/heads/main'], head);
});

test('audit detects deleted historical credentials without returning their value', async () => {
  const root = await fixture();
  const token = `ghp_${'a'.repeat(40)}`;
  await commit(root, `${token}\n`, 'secret fixture');
  await commit(root, 'removed from latest tree\n', 'clean latest tree');
  const result = auditHistory(root, ['refs/heads/main']);
  assert.equal(result.complete, false);
  assert.ok(result.findings.some((finding) => finding.kind === 'github_token'));
  assert.ok(!JSON.stringify(result).includes(token));
});

test('Git replace cannot hide the original blob that will be published', async () => {
  const root = await fixture();
  const token = `ghp_${'b'.repeat(40)}`;
  await commit(root, `${token}\n`, 'original secret blob');
  const original = git(root, 'rev-parse', 'HEAD:entry.txt');
  await writeFile(join(root, 'replacement.txt'), 'harmless replacement\n');
  const replacement = git(root, 'hash-object', '-w', 'replacement.txt');
  git(root, 'replace', original, replacement);
  assert.equal(git(root, 'cat-file', 'blob', original), 'harmless replacement');
  const result = auditHistory(root, ['refs/heads/main']);
  assert.equal(result.complete, false, 'Git object replacements must not hide original published content');
  assert.ok(result.findings?.some((finding) => finding.kind === 'github_token') || result.status === 'incomplete');
  assert.ok(!JSON.stringify(result).includes(token));
});

test('grafted history cannot omit secret-bearing original parents from the audit', async () => {
  const root = await fixture();
  await commit(root, `ghp_${'c'.repeat(40)}\n`, 'secret parent');
  const head = await commit(root, 'clean tip\n', 'clean tip');
  await writeFile(join(root, '.git', 'info', 'grafts'), `${head}\n`);
  const result = auditHistory(root, ['refs/heads/main']);
  assert.equal(result.complete, false, 'A local graft must not conceal reachable original parents');
  assert.ok(result.findings?.some((finding) => finding.kind === 'github_token') || result.status === 'incomplete');
});

test('snapshot audit uses exact source objects instead of local same-name refs', async () => {
  const root = await fixture();
  const source = await commit(root, `ghp_${'e'.repeat(40)}\n`, 'source secret');
  git(root, 'checkout', '--orphan', 'clean');
  await commit(root, 'unrelated clean local branch\n', 'clean replacement branch');
  git(root, 'branch', '-f', 'main', 'HEAD');
  assert.equal(auditHistory(root, ['refs/heads/main']).complete, true);
  const audit = auditSnapshot(root, { 'refs/heads/main': source });
  assert.equal(audit.complete, false);
  assert.equal(audit.snapshot['refs/heads/main'], source);
  assert.ok(audit.findings.some((finding) => finding.kind === 'github_token'));
  assert.throws(() => auditSnapshot(root, { 'refs/heads/main': 'HEAD' }), { code: 'invalid_snapshot' });
  assert.throws(() => auditSnapshot(root, { 'refs/heads/main': '-all' }), { code: 'invalid_snapshot' });
  assert.throws(() => auditSnapshot(root, { 'refs/git-setup/arbitrary': source }), { code: 'invalid_ref' });
});
