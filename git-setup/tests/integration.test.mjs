import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { access, mkdir, mkdtemp, writeFile, readFile, symlink, readlink } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

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

test('retired configuration and routing commands reject calls and preserve existing user files and symlinks', async () => {
  const { root } = await fixture();
  await writeFile(join(root, 'AGENTS.md'), '# Existing rules\n');
  await symlink('AGENTS.md', join(root, 'CLAUDE.md'));
  await mkdir(join(root, '.git-setup'));
  await writeFile(join(root, '.git-setup/project.json'), '{broken');
  await writeFile(join(root, '.git-setup/issue-routing.md'), 'User-owned old routing\n');
  for (const command of ['config-check', 'config-write', 'install-rules']) {
    const result = runCli(root, command, '--apply');
    assert.equal(result.status, 1);
    assert.equal(JSON.parse(result.stderr).code, 'unknown_command');
  }
  for (const flag of ['--override', '--project-rule', '--rule-file', '--title', '--body-file', '--filename', '--issue-url']) {
    const result = runCli(root, 'issue-create', flag, 'unused');
    assert.equal(result.status, 1);
    assert.equal(JSON.parse(result.stderr).code, 'invalid_argument');
  }
  assert.equal(await readFile(join(root, 'AGENTS.md'), 'utf8'), '# Existing rules\n');
  assert.equal(await readlink(join(root, 'CLAUDE.md')), 'AGENTS.md');
  assert.equal(await readFile(join(root, '.git-setup/project.json'), 'utf8'), '{broken');
  assert.equal(await readFile(join(root, '.git-setup/issue-routing.md'), 'utf8'), 'User-owned old routing\n');
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
