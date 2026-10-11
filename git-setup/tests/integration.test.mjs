import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { access, mkdir, mkdtemp, writeFile, readFile, symlink, readlink } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { installRules } from '../scripts/git-setup/integration.mjs';

const base = resolve(process.env.GIT_SETUP_TEST_ROOT || 'tmp/git-setup/integration-tests');
const cli = fileURLToPath(new URL('../scripts/git-setup/cli.mjs', import.meta.url));
function git(cwd, ...args) {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_CEILING_DIRECTORIES: base, LC_ALL: 'C' };
  for (const key of Object.keys(env)) if (/^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG)/.test(key)) delete env[key];
  const result = spawnSync('git', ['-C', cwd, '-c', 'commit.gpgsign=false', ...args], { encoding: 'utf8', timeout: 20_000, env });
  assert.equal(result.status, 0, result.stderr || String(result.error));
  return result.stdout.trim();
}
function runCli(project, ...args) {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (key.startsWith('GIT_SETUP_') && key !== 'GIT_SETUP_TEST_ROOT') delete env[key];
  return spawnSync(process.execPath, [cli, args[0], '--project', project, ...args.slice(1)], { encoding: 'utf8', timeout: 30_000, env });
}
before(async () => {
  let existing = dirname(base);
  while (true) { try { await access(existing); break; } catch { existing = dirname(existing); } }
  assert.equal(spawnSync('git', ['-C', existing, 'check-ignore', '--quiet', '--', base], { timeout: 15_000 }).status, 0);
  await mkdir(base, { recursive: true });
});
async function fixture() {
  const dir = await mkdtemp(join(base, 'rules-'));
  const root = join(dir, 'project');
  await mkdir(root);
  return { root, dir };
}

test('rule planning is read-only and installation preserves existing rule text', async () => {
  const { root } = await fixture();
  const original = '# Project rules\n\nPreserve this rule.\n';
  await writeFile(join(root, 'AGENTS.md'), original);
  const plan = await installRules(root, { files: ['AGENTS.md'] });
  assert.equal(plan.status, 'planned');
  assert.equal(await readFile(join(root, 'AGENTS.md'), 'utf8'), original);
  await assert.rejects(access(join(root, '.git-setup/issue-routing.md')));
  await installRules(root, { files: ['AGENTS.md'], apply: true });
  const once = await readFile(join(root, 'AGENTS.md'), 'utf8');
  assert.ok(once.startsWith(original));
  await installRules(root, { files: ['AGENTS.md'], apply: true });
  assert.equal(await readFile(join(root, 'AGENTS.md'), 'utf8'), once);
});

test('rules sharing one in-project symlink target remain one source', async () => {
  const { root } = await fixture();
  await writeFile(join(root, 'AGENTS.md'), '# Rules\n');
  await symlink('AGENTS.md', join(root, 'CLAUDE.md'));
  await installRules(root, { files: ['AGENTS.md', 'CLAUDE.md'], apply: true });
  assert.equal(await readlink(join(root, 'CLAUDE.md')), 'AGENTS.md');
  const result = await readFile(join(root, 'AGENTS.md'), 'utf8');
  assert.equal(result.match(/git-setup:issue-routing:start/g).length, 1);
});

test('external rule symlinks are rejected before any project rule is changed', async () => {
  const { root, dir } = await fixture();
  const outside = join(dir, 'outside.md');
  await writeFile(outside, '# External rules\n');
  await writeFile(join(root, 'AGENTS.md'), '# Keep\n');
  await symlink(outside, join(root, 'CLAUDE.md'));
  await assert.rejects(installRules(root, { files: ['AGENTS.md', 'CLAUDE.md'], apply: true }), { code: 'external_rule_symlink' });
  assert.equal(await readFile(join(root, 'AGENTS.md'), 'utf8'), '# Keep\n');
  assert.equal(await readFile(outside, 'utf8'), '# External rules\n');
});

test('a routing guide symlink cannot be silently replaced during rule installation', async () => {
  const { root, dir } = await fixture();
  const donor = join(dir, 'donor');
  await mkdir(donor);
  await installRules(donor, { files: ['AGENTS.md'], apply: true });
  const externalGuide = join(donor, '.git-setup/issue-routing.md');
  await mkdir(join(root, '.git-setup'));
  await symlink(externalGuide, join(root, '.git-setup/issue-routing.md'));
  await assert.rejects(installRules(root, { files: ['AGENTS.md'], apply: true }), { code: 'external_rule_symlink' });
  assert.equal(await readlink(join(root, '.git-setup/issue-routing.md')), externalGuide);
  await assert.rejects(access(join(root, 'AGENTS.md')));
});

test('CLI publish checks historical content before any remote write', async () => {
  const { root, dir } = await fixture();
  git(root, 'init', '--initial-branch=main');
  git(root, 'config', 'user.name', 'Integration Test');
  git(root, 'config', 'user.email', 'integration@example.invalid');
  const token = `ghp_${'d'.repeat(40)}`;
  await writeFile(join(root, 'entry.txt'), `${token}\n`);
  git(root, 'add', 'entry.txt');
  git(root, 'commit', '-m', 'historical secret');
  await writeFile(join(root, 'entry.txt'), 'clean latest tree\n');
  git(root, 'add', 'entry.txt');
  git(root, 'commit', '-m', 'remove secret');
  const remote = join(dir, 'remote.git');
  git(dir, 'init', '--bare', remote);
  git(root, 'remote', 'add', 'origin', remote);
  const result = runCli(root, 'publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--apply');
  assert.equal(result.status, 2, result.stderr);
  assert.equal(JSON.parse(result.stdout).status, 'audit_blocked');
  assert.equal(git(remote, 'for-each-ref', '--format=%(refname)'), '');
  assert.ok(!`${result.stdout}${result.stderr}`.includes(token));
});

test('CLI inherited current target keeps the initiating child as the local destination', async () => {
  const { root } = await fixture();
  git(root, 'init', '--initial-branch=main');
  const child = join(root, 'child');
  await mkdir(child);
  git(child, 'init', '--initial-branch=main');
  await mkdir(join(root, '.git-setup'));
  await writeFile(join(root, '.gitignore'), '/data/git-setup/\n');
  await writeFile(join(child, '.gitignore'), '/data/git-setup/\n');
  await writeFile(join(root, '.git-setup/project.json'), JSON.stringify({
    schemaVersion: 1,
    issues: { backend: 'local', target: 'current', local: { directory: 'issues', filename: '{number}-{slug}.md', numbering: { start: 1, width: 3 } } },
    workspace: { repositories: [{ path: 'child', issues: 'inherit' }] },
  }));
  const body = join(root, 'request.md');
  await writeFile(body, 'Created from the child project.\n');
  const result = runCli(child, 'issue-create', '--workspace', root, '--title', 'Child issue', '--body-file', body, '--operation-id', 'integration-child-current', '--apply');
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const output = JSON.parse(result.stdout);
  assert.equal(output.backend, 'local');
  assert.ok(output.filePath.startsWith(join(child, 'issues')));
  assert.match(output.message, /保存到本地/);
  assert.match(await readFile(output.filePath, 'utf8'), /Created from the child project/);
  await assert.rejects(access(join(root, 'issues')));
});

test('CLI refuses a changed destination after the Agent previewed the Issue target', async () => {
  const { root } = await fixture();
  git(root, 'init', '--initial-branch=main');
  await mkdir(join(root, '.git-setup'));
  await writeFile(join(root, '.gitignore'), '/data/git-setup/\n');
  const configPath = join(root, '.git-setup/project.json');
  const config = { schemaVersion: 1, issues: { backend: 'local', target: 'current', local: { directory: 'issues', filename: '{slug}.md' } } };
  await writeFile(configPath, JSON.stringify(config));
  const preview = runCli(root, 'issue-resolve');
  assert.equal(preview.status, 0, preview.stderr);
  const fingerprint = JSON.parse(preview.stdout).fingerprint;
  config.issues.local.directory = 'other-issues';
  await writeFile(configPath, JSON.stringify(config));
  const body = join(root, 'request.md');
  await writeFile(body, 'Keep the reviewed destination.\n');
  const stale = runCli(root, 'issue-create', '--expected-fingerprint', fingerprint, '--title', 'Reviewed target', '--body-file', body, '--operation-id', 'reviewed-destination-001', '--apply');
  assert.equal(stale.status, 1);
  assert.equal(JSON.parse(stale.stderr).code, 'configuration_changed');
  await assert.rejects(access(join(root, 'data')));
  await assert.rejects(access(join(root, 'other-issues')));
  const fresh = JSON.parse(runCli(root, 'issue-resolve').stdout);
  const created = runCli(root, 'issue-create', '--expected-fingerprint', fresh.fingerprint, '--title', 'Reviewed target', '--body-file', body, '--operation-id', 'reviewed-destination-001', '--apply');
  assert.equal(created.status, 0, created.stderr);
  assert.match(await readFile(JSON.parse(created.stdout).filePath, 'utf8'), /Keep the reviewed destination/);
});
