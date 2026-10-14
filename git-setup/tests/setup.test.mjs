import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, mkdtemp, writeFile, readFile, readdir, rm, chmod } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { registerRepositories } from './fixtures/repositories.mjs';

const exec = promisify(execFile);
const cli = fileURLToPath(new URL('../scripts/git-setup/cli.mjs', import.meta.url));
const base = resolve(process.env.GIT_SETUP_TEST_ROOT || '../../tmp/git-setup/setup-tests');
async function fixture() {
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'setup-'));
  const env = { ...process.env, GIT_CEILING_DIRECTORIES: root, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_SETUP_|^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE)$/.test(key)) delete env[key];
  const git = async (cwd, ...args) => (await exec('git', ['-C', cwd, '-c', 'commit.gpgsign=false', ...args], { env, timeout: 20000 })).stdout.trim();
  const work = join(root, 'work'), remote = join(root, 'remote.git');
  await mkdir(work);
  await git(root, 'init', '--bare', '--initial-branch=main', remote);
  const run = async (command, ...args) => {
    try {
      const r = await exec(process.execPath, [cli, command, '--project', work, ...args], { env, timeout: 60000 });
      return { exit: 0, ...JSON.parse(r.stdout) };
    } catch (e) { return { exit: e.code, ...JSON.parse(e.stderr || e.stdout) }; }
  };
  const init = async () => { await git(work, 'init', '--initial-branch=main'); await git(work, 'config', 'user.name', 'Setup Test'); await git(work, 'config', 'user.email', 'setup@example.invalid'); };
  const populate = async () => {
    const seed = join(root, 'seed'); await mkdir(seed);
    await git(seed, 'init', '--initial-branch=main');
    await writeFile(join(seed, 'README.md'), 'remote content\n'); await git(seed, 'add', 'README.md');
    await git(seed, '-c', 'user.name=Setup Test', '-c', 'user.email=setup@example.invalid', 'commit', '-m', 'fixture');
    await git(seed, 'push', remote, 'main');
    return seed;
  };
  return { root, work, remote, git, run, init, populate };
}

test('URL-only entry lists every existing remote and requires add or replace even for an identical URL', async () => {
  const f = await fixture(); await f.init();
  await f.git(f.work, 'remote', 'add', 'upstream', f.remote);
  await f.git(f.work, 'remote', 'add', 'archive', f.remote);
  const before = await readFile(join(f.work, '.git/config'), 'utf8');
  const result = await f.run('setup', '--url', f.remote);
  assert.equal(result.status, 'needs_input');
  assert.deepEqual(result.required, ['action', 'name']);
  assert.deepEqual(result.local.remotes.map(r => r.name), ['archive', 'upstream']);
  assert.equal((await f.run('setup', '--url', f.remote, '--apply')).code, 'setup_choice_required');
  assert.equal(await readFile(join(f.work, '.git/config'), 'utf8'), before);
});

test('an empty reachable remote initializes the current project without staging, committing or pushing', async () => {
  const f = await fixture();
  await writeFile(join(f.work, 'notes.txt'), 'keep my notes\n');
  const args = ['--url', f.remote, '--branch', 'main', '--author-name', 'Setup Test', '--author-email', 'setup@example.invalid'];
  const plan = await f.run('setup', ...args);
  assert.equal(plan.status, 'planned');
  assert.equal(plan.kind, 'init');
  assert.deepEqual(await readdir(f.work), ['notes.txt']);
  const result = await f.run('setup', ...args, '--expected-fingerprint', plan.fingerprint, '--apply');
  assert.equal(result.status, 'configured');
  assert.equal(result.committed, false); assert.equal(result.pushed, false);
  assert.match(result.message, /暂未提交和推送/);
  const state = await f.run('inspect');
  assert.equal(state.head, null); assert.equal(state.branch, 'main');
  assert.equal(state.remotes[0].fetch[0], f.remote);
  assert.equal(await f.git(f.work, 'ls-files'), '');
  assert.equal(await f.git(f.remote, 'for-each-ref'), '');
  assert.equal(await readFile(join(f.work, 'notes.txt'), 'utf8'), 'keep my notes\n');
  assert.equal((await f.run('config-check')).code, 'legacy_command_removed');
  await assert.rejects(readFile(join(f.work, '.git-setup/project.json')), { code: 'ENOENT' });
});

test('explicit replacement changes only the selected remote and preserves unused legacy files and staged files', async () => {
  const f = await fixture(); await f.init();
  const old = join(f.root, 'old.git'); await f.git(f.root, 'init', '--bare', old);
  await f.git(f.work, 'remote', 'add', 'origin', old);
  await f.git(f.work, 'remote', 'add', 'backup', old);
  await f.git(f.work, 'remote', 'set-url', '--push', 'origin', old);
  await writeFile(join(f.work, 'staged.txt'), 'keep staged\n'); await f.git(f.work, 'add', 'staged.txt');
  const beforeIndex = await f.git(f.work, 'ls-files', '--stage');
  const config = { schemaVersion: 1, remotes: { origin: old, backup: old }, issues: { backend: 'local', target: 'current', local: { directory: 'tickets' } } };
  await mkdir(join(f.work, '.git-setup')); await writeFile(join(f.work, '.git-setup/project.json'), JSON.stringify(config));
  const args = ['--url', f.remote, '--action', 'replace', '--name', 'origin'];
  const plan = await f.run('setup', ...args);
  assert.equal(plan.status, 'planned');
  const result = await f.run('setup', ...args, '--expected-fingerprint', plan.fingerprint, '--apply');
  assert.equal(result.status, 'configured');
  const after = await f.run('inspect');
  assert.deepEqual(after.remotes.find(r => r.name === 'origin').fetch, [f.remote]);
  assert.deepEqual(after.remotes.find(r => r.name === 'origin').push, [f.remote]);
  assert.deepEqual(after.remotes.find(r => r.name === 'backup').fetch, [old]);
  assert.equal(await f.git(f.work, 'ls-files', '--stage'), beforeIndex);
  assert.equal(await readFile(join(f.work, '.git-setup/project.json'), 'utf8'), JSON.stringify(config));
  assert.equal((await f.run('setup', ...args, '--expected-fingerprint', plan.fingerprint, '--apply')).code, 'setup_plan_changed');
});

test('replacement exposes sync dependencies and requires explicit acceptance; config drift invalidates the plan', async () => {
  const f = await fixture(); await f.init();
  const old = join(f.root, 'old.git'); await f.git(f.root, 'init', '--bare', old);
  await f.git(f.work, 'remote', 'add', 'upstream', old);
  await f.git(f.work, 'config', 'branch.main.remote', 'upstream');
  await f.git(f.work, 'config', 'branch.main.merge', 'refs/heads/main');
  await f.git(f.work, 'remote', 'add', 'backup', old);
  await registerRepositories(f.work, []);
  const policyFile = join(f.work, 'fixture-git/config.json');
  const policy = JSON.parse(await readFile(policyFile, 'utf8'));
  policy.sync = { source: 'upstream', targets: ['backup'], refs: ['refs/heads/main'] };
  await writeFile(policyFile, JSON.stringify(policy));
  const args = ['--url', f.remote, '--action', 'replace', '--name', 'upstream'];
  const pending = await f.run('setup', ...args);
  assert.equal(pending.status, 'needs_input');
  assert.deepEqual(pending.required, ['accept-effects']);
  assert.equal(pending.effects.harness[0].sync.role, 'source');
  assert.match(pending.effects.branches.join('\n'), /branch.main.remote upstream/);
  assert.equal((await f.run('setup', ...args, '--apply')).code, 'setup_choice_required');
  const plan = await f.run('setup', ...args, '--accept-effects');
  await f.git(f.work, 'config', 'branch.main.pushRemote', 'upstream');
  assert.equal((await f.run('setup', ...args, '--accept-effects', '--expected-fingerprint', plan.fingerprint, '--apply')).code, 'setup_plan_changed');
  assert.equal(await f.git(f.work, 'remote', 'get-url', 'upstream'), old);
  const next = await f.run('setup', ...args, '--accept-effects');
  assert.equal((await f.run('setup', ...args, '--accept-effects', '--expected-fingerprint', next.fingerprint, '--apply')).status, 'configured');
});

test('missing, unreadable or changing Issue rules and broken legacy files do not participate in ordinary setup', async () => {
  const f = await fixture(); await f.init();
  await mkdir(join(f.work, '.git-setup'));
  await writeFile(join(f.work, '.git-setup/project.json'), '{broken');
  await writeFile(join(f.work, '.git-setup/.project-write.lock'), 'unused lock');
  await writeFile(join(f.work, '.env.git-setup'), 'GIT_SETUP_ISSUE_JSON={broken\n');
  const plan = await f.run('setup', '--url', f.remote);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  assert.equal(plan.issues, undefined);
  await mkdir(join(f.work, 'docs/agents'), { recursive: true });
  const rules = join(f.work, 'docs/agents/issue-tracker.md');
  await writeFile(rules, 'Unrelated Issue rules.'); await chmod(rules, 0);
  try {
    assert.equal((await f.run('setup', '--url', f.remote)).status, 'planned');
    await chmod(rules, 0o644); await writeFile(rules, 'Changed unrelated Issue rules.');
    const changed = await f.run('setup', '--url', f.remote);
    assert.equal(changed.status, 'planned');
    assert.equal((await f.run('setup', '--url', f.remote, '--expected-fingerprint', changed.fingerprint, '--apply')).status, 'configured');
  } finally { await chmod(rules, 0o644); }
  assert.equal(await readFile(join(f.work, '.git-setup/project.json'), 'utf8'), '{broken');
  assert.equal(await readFile(join(f.work, '.git-setup/.project-write.lock'), 'utf8'), 'unused lock');
});

test('a populated remote is cloned into the selected empty directory and preserves its shared settings', async () => {
  const f = await fixture(); const seed = await f.populate();
  const config = { schemaVersion: 1, issues: { backend: 'local', target: 'current', local: { directory: 'tickets' } } };
  await mkdir(join(seed, '.git-setup')); await writeFile(join(seed, '.git-setup/project.json'), JSON.stringify(config));
  await f.git(seed, 'add', '.git-setup'); await f.git(seed, '-c', 'user.name=Setup Test', '-c', 'user.email=setup@example.invalid', 'commit', '-m', 'config');
  await f.git(seed, 'push', f.remote, 'main');
  const plan = await f.run('setup', '--url', f.remote);
  assert.equal(plan.kind, 'clone');
  assert.deepEqual(await readdir(f.work), []);
  const cloned = await f.run('setup', '--url', f.remote, '--expected-fingerprint', plan.fingerprint, '--apply');
  assert.equal(cloned.status, 'needs_project_rules');
  assert.equal(await readFile(join(f.work, '.git-setup/project.json'), 'utf8'), JSON.stringify(config));
  // The Agent reads the newly cloned rules, then continues its already selected origin operation.
  const followup = ['--url', f.remote, '--action', 'add', '--name', 'origin', '--author-name', 'Setup Test', '--author-email', 'setup@example.invalid'];
  const continuation = await f.run('setup', ...followup);
  const result = await f.run('setup', ...followup, '--expected-fingerprint', continuation.fingerprint, '--apply');
  assert.equal(result.status, 'configured');
  assert.equal(result.local.root, f.work);
  assert.equal(result.local.head, await f.git(seed, 'rev-parse', 'HEAD'));
  assert.equal(await f.git(f.work, 'config', '--local', 'user.email'), 'setup@example.invalid');
  assert.equal(await readFile(join(f.work, 'README.md'), 'utf8'), 'remote content\n');
  assert.equal(await readFile(join(f.work, '.git-setup/project.json'), 'utf8'), JSON.stringify(config));
  assert.match(result.message, /本次暂未提交和推送。/);
});

test('directory contents, unavailable remotes and tags-only histories never fall through to init or unsafe clone', async () => {
  const f = await fixture(); await f.populate();
  await writeFile(join(f.work, '.hidden'), 'keep');
  assert.equal((await f.run('setup', '--url', f.remote)).status, 'needs_input');
  assert.deepEqual(await readdir(f.work), ['.hidden']);
  assert.equal((await f.run('setup', '--url', join(f.root, 'missing.git'))).status, 'error');
  const tags = await fixture(); await tags.populate();
  const sha = await tags.git(tags.remote, 'rev-parse', 'main');
  await tags.git(tags.remote, 'update-ref', 'refs/tags/v1', sha);
  await tags.git(tags.remote, 'update-ref', '-d', 'refs/heads/main');
  const plan = await tags.run('setup', '--url', tags.remote);
  assert.equal(plan.status, 'needs_input');
  assert.equal(plan.remote.empty, false);
  assert.deepEqual(plan.required, ['branch']);
  assert.deepEqual(await readdir(tags.work), []);
});

test('partial configuration writes preserve the selected operation and can be completed after inspection', async () => {
  const f = await fixture(); await f.init();
  await writeFile(join(f.work, '.gitignore'), 'data/git-setup/\n');
  await writeFile(join(f.work, '.git/config.lock'), 'another writer');
  const planFile = join(f.work, 'data/git-setup/connect.json');
  const args = ['--url', f.remote, '--action', 'add', '--name', 'origin', '--plan-file', planFile];
  const plan = await f.run('setup', ...args);
  assert.equal(JSON.parse(await readFile(planFile, 'utf8')).result.fingerprint, plan.fingerprint);
  const result = await f.run('setup', ...args, '--expected-fingerprint', plan.fingerprint, '--apply');
  assert.equal(result.status, 'incomplete'); assert.equal(result.exit, 2);
  assert.equal(result.failedStep, 'remote');
  assert.equal(await f.git(f.work, 'remote'), '');
  const record = JSON.parse(await readFile(planFile, 'utf8'));
  assert.equal(record.request.action, 'add'); assert.equal(record.result.status, 'incomplete');
  await rm(join(f.work, '.git/config.lock'));
  const next = await f.run('setup', ...args);
  assert.equal((await f.run('setup', ...args, '--expected-fingerprint', next.fingerprint, '--apply')).status, 'configured');
  // An unrelated new invocation still asks for the remote decision.
  assert.equal((await f.run('setup', '--url', f.remote)).status, 'needs_input');
});

test('identical replacement is a no-op for Git config; changed old addresses invalidate approval', async () => {
  const f = await fixture(); await f.init(); await f.git(f.work, 'remote', 'add', 'upstream', f.remote);
  const before = await readFile(join(f.work, '.git/config'));
  const args = ['--url', f.remote, '--action', 'replace', '--name', 'upstream'];
  const plan = await f.run('setup', ...args);
  assert.equal((await f.run('setup', ...args, '--expected-fingerprint', plan.fingerprint, '--apply')).status, 'configured');
  assert.deepEqual(await readFile(join(f.work, '.git/config')), before);
  const next = await f.run('setup', ...args);
  await f.git(f.work, 'remote', 'set-url', 'upstream', join(f.root, 'changed.git'));
  assert.equal((await f.run('setup', ...args, '--expected-fingerprint', next.fingerprint, '--apply')).code, 'setup_plan_changed');
});

test('a file appearing after a clone plan prevents checkout, and invalid legacy JSON does not block continuation', async () => {
  const f = await fixture(); const seed = await f.populate();
  const plan = await f.run('setup', '--url', f.remote);
  await writeFile(join(f.work, 'local.txt'), 'keep');
  assert.equal((await f.run('setup', '--url', f.remote, '--expected-fingerprint', plan.fingerprint, '--apply')).code, 'setup_choice_required');
  assert.deepEqual(await readdir(f.work), ['local.txt']);
  await rm(join(f.work, 'local.txt'));
  await mkdir(join(seed, '.git-setup')); await writeFile(join(seed, '.git-setup/project.json'), '{broken');
  await f.git(seed, 'add', '.git-setup'); await f.git(seed, '-c', 'user.name=Setup Test', '-c', 'user.email=setup@example.invalid', 'commit', '-m', 'invalid config'); await f.git(seed, 'push', f.remote, 'main');
  const next = await f.run('setup', '--url', f.remote);
  const result = await f.run('setup', '--url', f.remote, '--expected-fingerprint', next.fingerprint, '--apply');
  assert.equal(result.status, 'needs_project_rules');
  const continuation = ['--url', f.remote, '--action', 'add', '--name', 'origin'];
  const resumed = await f.run('setup', ...continuation);
  assert.equal(resumed.status, 'planned');
  assert.equal((await f.run('setup', ...continuation, '--expected-fingerprint', resumed.fingerprint, '--apply')).status, 'configured');
  assert.equal(await readFile(join(f.work, '.git-setup/project.json'), 'utf8'), '{broken');
  assert.equal(await readFile(join(f.work, 'README.md'), 'utf8'), 'remote content\n');
});

test('ordinary subdirectories cannot redirect setup to the parent repository', async () => {
  const f = await fixture(); await f.init(); const nested = join(f.work, 'nested'); await mkdir(nested);
  const { setup } = await import('../scripts/git-setup/setup.mjs');
  await assert.rejects(setup(nested, { url: f.remote }), { code: 'setup_project_required' });
  assert.equal(await f.git(f.work, 'remote'), '');
});

test('child replacement exposes the actual manager policy and another Worktree without consuming Issue defaults', async () => {
  const f = await fixture(); await f.init();
  await writeFile(join(f.work, 'README.md'), 'fixture'); await f.git(f.work, 'add', '.'); await f.git(f.work, 'commit', '-m', 'fixture');
  const old = join(f.root, 'old.git'); await f.git(f.root, 'init', '--bare', old);
  await f.git(f.work, 'remote', 'add', 'origin', old);
  await f.git(f.root, 'init', '--initial-branch=main');
  await registerRepositories(f.root, ['work']);
  const policyPath = join(f.root, 'fixture-git/config.json'), policy = JSON.parse(await readFile(policyPath, 'utf8'));
  policy.repositories = { work: { targets: { origin: { fetch: old, push: old } }, sync: null } };
  await writeFile(policyPath, JSON.stringify(policy));
  const linked = join(f.root, 'linked'); await f.git(f.work, 'worktree', 'add', '--detach', linked);
  const args = ['--url', f.remote, '--workspace', f.root, '--action', 'replace', '--name', 'origin'];
  const pending = await f.run('setup', ...args);
  assert.equal(pending.status, 'needs_input', JSON.stringify(pending));
  assert.equal(pending.effects.harness[0].path, policyPath);
  assert.equal(pending.effects.harness[0].repository, 'work');
  assert.equal(pending.effects.otherWorktrees[0].project, linked);
  assert.equal(pending.effects.issues, undefined);
  const plan = await f.run('setup', ...args, '--accept-effects');
  policy.repositories.work.targets.origin.push = f.remote;
  await writeFile(policyPath, JSON.stringify(policy));
  assert.equal((await f.run('setup', ...args, '--accept-effects', '--expected-fingerprint', plan.fingerprint, '--apply')).code, 'setup_plan_changed');
  assert.equal(await f.git(f.work, 'remote', 'get-url', 'origin'), old);
});

test('remote movement after a clone plan is rejected before local writes', async () => {
  const f = await fixture(); const seed = await f.populate();
  const plan = await f.run('setup', '--url', f.remote);
  await writeFile(join(seed, 'README.md'), 'new content\n'); await f.git(seed, 'add', 'README.md');
  await f.git(seed, '-c', 'user.name=Setup Test', '-c', 'user.email=setup@example.invalid', 'commit', '-m', 'move'); await f.git(seed, 'push', f.remote, 'main');
  assert.equal((await f.run('setup', '--url', f.remote, '--expected-fingerprint', plan.fingerprint, '--apply')).code, 'setup_plan_changed');
  assert.deepEqual(await readdir(f.work), []);
});

test('custom refs are not an empty remote and a requested existing branch resolves missing remote HEAD', async () => {
  const f = await fixture(); await f.populate();
  await f.git(f.remote, 'symbolic-ref', 'HEAD', 'refs/heads/missing');
  assert.deepEqual((await f.run('setup', '--url', f.remote)).required, ['branch']);
  assert.equal((await f.run('setup', '--url', f.remote, '--branch', 'main')).kind, 'clone');
  const sha = await f.git(f.remote, 'rev-parse', 'main');
  await f.git(f.remote, 'update-ref', 'refs/changes/1', sha); await f.git(f.remote, 'update-ref', '-d', 'refs/heads/main');
  const result = await f.run('setup', '--url', f.remote);
  assert.equal(result.remote.empty, false); assert.deepEqual(result.required, ['branch']);
});

test('recovery records reject tracked or nonignored paths and never save credential-bearing URLs', async () => {
  const f = await fixture(); await f.init();
  const planFile = join(f.work, 'data/git-setup/plan.json');
  assert.equal((await f.run('setup', '--url', f.remote, '--plan-file', planFile)).code, 'setup_record_path');
  await writeFile(join(f.work, '.gitignore'), 'data/git-setup/\n');
  const unsafe = await f.run('setup', '--url', 'https://user:password@example.invalid/repo.git', '--plan-file', planFile);
  assert.equal(unsafe.code, 'credential_url');
  await assert.rejects(readFile(planFile), { code: 'ENOENT' });
  assert.equal(await f.git(f.work, 'remote'), '');
});

test('continuing an interrupted init fills the previously chosen author without changing a conflicting identity', async () => {
  const f = await fixture(); await f.git(f.work, 'init', '--initial-branch=main');
  await f.git(f.work, 'config', 'user.name', 'Setup Test');
  const args = ['--url', f.remote, '--branch', 'main', '--author-name', 'Setup Test', '--author-email', 'setup@example.invalid'];
  const plan = await f.run('setup', ...args);
  const result = await f.run('setup', ...args, '--expected-fingerprint', plan.fingerprint, '--apply');
  assert.equal(result.status, 'configured');
  assert.equal(await f.git(f.work, 'config', '--local', 'user.email'), 'setup@example.invalid');
  await f.git(f.work, 'config', 'user.email', 'changed@example.invalid');
  const nextArgs = [...args, '--action', 'replace', '--name', 'origin'];
  const next = await f.run('setup', ...nextArgs);
  const blocked = await f.run('setup', ...nextArgs, '--expected-fingerprint', next.fingerprint, '--apply');
  assert.equal(blocked.status, 'incomplete'); assert.equal(blocked.error.code, 'setup_author_changed');
  assert.equal(await f.git(f.work, 'config', '--local', 'user.email'), 'changed@example.invalid');
});
