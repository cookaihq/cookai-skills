import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, writeFile, readFile, rm, access } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { inspect, init, addRemote, publish, sync } from '../scripts/git-setup/git.mjs';

const base = resolve(process.env.GIT_SETUP_TEST_ROOT || 'tmp/git-setup/git-tests');
function git(cwd, ...args) {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', LC_ALL: 'C' };
  for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE']) delete env[key];
  const result = spawnSync('git', ['-C', cwd, '-c', 'commit.gpgsign=false', ...args], { encoding: 'utf8', timeout: 20_000, env });
  assert.equal(result.status, 0, `git ${args.join(' ')}: ${result.stderr || result.error}`);
  return result.stdout.trim();
}
before(async () => {
  let existing = dirname(base);
  while (true) {
    try { await access(existing); break; } catch { existing = dirname(existing); }
  }
  const ignored = spawnSync('git', ['-C', existing, 'check-ignore', '--quiet', '--', base], { encoding: 'utf8', timeout: 15_000 });
  assert.equal(ignored.status, 0, '测试输出目录必须被 Git 忽略；设置 GIT_SETUP_TEST_ROOT 到工作区 tmp/git-setup/。');
  await mkdir(base, { recursive: true });
  process.env.GIT_CEILING_DIRECTORIES = base;
});

async function fixture() {
  const dir = await mkdtemp(join(base, 'case-'));
  const source = join(dir, 'source.git'), backup = join(dir, 'backup.git'), work = join(dir, 'work');
  await mkdir(work);
  git(dir, 'init', '--bare', '--initial-branch=main', source);
  git(dir, 'init', '--bare', '--initial-branch=main', backup);
  await init(work, { branch: 'main', authorName: 'Git Setup Test', authorEmail: 'git-setup@example.invalid' });
  await writeFile(join(work, 'readme.txt'), 'first\n');
  git(work, 'add', 'readme.txt');
  git(work, 'commit', '-m', 'first');
  git(work, 'tag', '-a', 'v1', '-m', 'first tag');
  await addRemote(work, { name: 'origin', url: source });
  await addRemote(work, { name: 'backup', url: backup });
  git(work, 'push', 'origin', 'refs/heads/main', 'refs/tags/v1');
  return { dir, source, backup, work, config: { version: 1, remotes: { origin: source, backup }, sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main', 'refs/tags/v1'] } } };
}

test('initialization requires explicit identity and refuses an enclosing repository', async () => {
  const dir = await mkdtemp(join(base, 'init-'));
  const work = join(dir, 'work');
  await mkdir(work);
  await assert.rejects(init(work, { branch: 'main' }), { code: 'initialization_inputs' });
  const result = await init(work, { branch: 'main', authorName: 'Test Author', authorEmail: 'test@example.invalid' });
  assert.equal(result.state.head, null);
  assert.equal(result.state.branch, 'main');
  assert.equal(result.state.author.email, 'test@example.invalid');
  await mkdir(join(work, 'nested'));
  await assert.rejects(init(join(work, 'nested'), { branch: 'main', authorName: 'Other', authorEmail: 'other@example.invalid' }), { code: 'inside_parent_repository' });
  await assert.rejects(access(join(work, 'nested', '.git')));
  const broken = join(dir, 'broken');
  await mkdir(join(broken, '.git'), { recursive: true });
  await assert.rejects(init(broken, { branch: 'main', authorName: 'Test', authorEmail: 'test@example.invalid' }), { code: 'invalid_git_directory' });
});

test('adding a remote is idempotent and preserves existing remotes and upstream', async () => {
  const { work, source, backup } = await fixture();
  git(work, 'branch', '--set-upstream-to=origin/main', 'main');
  const before = await inspect(work);
  assert.equal((await addRemote(work, { name: 'backup', url: backup })).status, 'unchanged');
  await assert.rejects(addRemote(work, { name: 'origin', url: backup }), { code: 'remote_conflict' });
  const after = await inspect(work);
  assert.equal(after.upstream, before.upstream);
  assert.equal(after.remotes.find((remote) => remote.name === 'origin').fetch[0], source);
  await assert.rejects(addRemote(work, { name: 'secret', url: 'https://user:secret@example.com/repo.git' }), { code: 'credential_url' });
});

test('sync copies published source objects, preserves local refs/FETCH_HEAD and annotated tags', async () => {
  const { source, backup, work, config } = await fixture();
  const published = git(source, 'rev-parse', 'refs/heads/main');
  await writeFile(join(work, 'readme.txt'), 'local unpublished\n');
  git(work, 'add', 'readme.txt');
  git(work, 'commit', '-m', 'not published');
  git(work, 'tag', 'local-only');
  await writeFile(join(work, '.git', 'FETCH_HEAD'), 'sentinel\n');
  const refs = git(work, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags', 'refs/remotes');
  const indexBefore = await readFile(join(work, '.git', 'index'));
  const authCalls = [];
  const result = await sync(work, config, { authForUrl: async (url) => { authCalls.push(url); return { env: {} }; } });
  assert.equal(result.complete, true);
  assert.equal(git(backup, 'rev-parse', 'refs/heads/main'), published);
  assert.equal(git(backup, 'rev-parse', 'refs/tags/v1'), git(source, 'rev-parse', 'refs/tags/v1'));
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname)'), 'refs/heads/main\nrefs/tags/v1');
  assert.equal(git(work, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags', 'refs/remotes'), refs);
  assert.equal(await readFile(join(work, '.git', 'FETCH_HEAD'), 'utf8'), 'sentinel\n');
  assert.deepEqual(await readFile(join(work, '.git', 'index')), indexBefore);
  assert.equal(git(work, 'for-each-ref', '--format=%(refname)', 'refs/git-setup'), '');
  assert.ok(authCalls.includes(source));
  assert.ok(authCalls.includes(backup));
  const repeat = await sync(work, config);
  assert.ok(repeat.results.every((item) => item.unchanged));
});

test('dry-run performs no fetch or write when source objects are missing locally', async () => {
  const { dir, source, backup, config } = await fixture();
  const controller = join(dir, 'controller');
  await mkdir(controller);
  await init(controller, { branch: 'main', authorName: 'Test', authorEmail: 'test@example.invalid' });
  await addRemote(controller, { name: 'origin', url: source });
  await addRemote(controller, { name: 'backup', url: backup });
  const result = await sync(controller, config, { dryRun: true });
  assert.equal(result.dryRun, true);
  assert.equal(result.complete, false);
  assert.equal(result.audit.status, 'not_performed');
  assert.ok(result.results.every((item) => item.status === 'planned'));
  assert.equal(git(controller, 'for-each-ref', '--format=%(refname)'), '');
  assert.equal(git(controller, 'count-objects', '-v').split('\n')[0], 'count: 0');
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname)'), '');
  await assert.rejects(access(join(controller, '.git', 'FETCH_HEAD')));
});

test('a rejected target does not obscure success and a rerun repairs only missing refs', async () => {
  const { dir, work, backup, config } = await fixture();
  const rejected = join(dir, 'rejected.git');
  git(dir, 'init', '--bare', rejected);
  const hook = join(rejected, 'hooks', 'pre-receive');
  await writeFile(hook, '#!/bin/sh\nexit 1\n', { mode: 0o755 });
  await addRemote(work, { name: 'rejected', url: rejected });
  config.remotes.rejected = rejected;
  config.sync.targets.push('rejected');
  const result = await sync(work, config);
  assert.equal(result.complete, false);
  assert.ok(result.results.filter((item) => item.remote === 'backup').every((item) => item.status === 'verified'));
  assert.ok(result.results.filter((item) => item.remote === 'rejected').every((item) => item.status === 'failed'));
  await rm(hook);
  const repeat = await sync(work, config);
  assert.equal(repeat.complete, true);
  assert.ok(repeat.results.filter((item) => item.remote === 'backup').every((item) => item.unchanged));
  assert.equal(git(rejected, 'rev-parse', 'refs/heads/main'), git(backup, 'rev-parse', 'refs/heads/main'));
});

test('conflicting tags and diverged destination branches are not overwritten', async () => {
  const { backup, work, config } = await fixture();
  git(work, 'push', 'backup', 'refs/heads/main');
  await writeFile(join(work, 'readme.txt'), 'only backup\n');
  git(work, 'add', 'readme.txt');
  git(work, 'commit', '-m', 'backup advances independently');
  git(work, 'tag', '-f', '-a', 'v1', '-m', 'different tag');
  git(work, 'push', 'backup', 'refs/heads/main', 'refs/tags/v1');
  const before = git(backup, 'for-each-ref', '--format=%(refname) %(objectname)');
  const result = await sync(work, config);
  assert.equal(result.complete, false);
  assert.ok(result.results.every((item) => item.status === 'conflict'));
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname) %(objectname)'), before);
});

test('publish uses explicit local refs and does not commit or set upstream', async () => {
  const { work, backup } = await fixture();
  await writeFile(join(work, 'untracked.txt'), 'keep local\n');
  const before = await inspect(work);
  const result = await publish(work, { remote: 'backup', refs: ['refs/heads/main'] });
  assert.equal(result.complete, true);
  assert.equal(result.committed, false);
  assert.equal(git(backup, 'rev-parse', 'refs/heads/main'), before.head);
  const after = await inspect(work);
  assert.equal(after.head, before.head);
  assert.equal(after.upstream, before.upstream);
  assert.deepEqual(after.status, before.status);
  await assert.rejects(publish(work, { remote: 'backup', refs: ['main'] }), { code: 'invalid_ref' });
});

test('LFS pointers make full project completion false even when Git refs match', async () => {
  const { work, config } = await fixture();
  await writeFile(join(work, '.gitattributes'), '*.bin filter=lfs diff=lfs merge=lfs -text\n');
  await writeFile(join(work, 'asset.pointer'), 'version https://git-lfs.github.com/spec/v1\noid sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nsize 5\n');
  git(work, 'add', '.gitattributes', 'asset.pointer');
  git(work, 'commit', '-m', 'LFS example');
  git(work, 'push', 'origin', 'refs/heads/main');
  const result = await sync(work, config);
  assert.equal(result.refsComplete, true);
  assert.equal(result.complete, false);
  assert.ok(result.contentWarnings.some((warning) => warning.code === 'lfs_content'));
});

test('a configured remote change is detected before writing any destination', async () => {
  const { work, backup, config } = await fixture();
  config.remotes.backup = `${backup}.different`;
  await assert.rejects(sync(work, config), { code: 'remote_configuration_changed' });
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname)'), '');
});

test('publication rejects refs that changed after the content review', async () => {
  const { work, backup } = await fixture();
  const expectedRefs = { 'refs/heads/main': git(work, 'rev-parse', 'HEAD') };
  await writeFile(join(work, 'readme.txt'), 'changed after review\n');
  git(work, 'add', 'readme.txt');
  git(work, 'commit', '-m', 'not reviewed');
  await assert.rejects(publish(work, { remote: 'backup', refs: ['refs/heads/main'], expectedRefs }), { code: 'reviewed_refs_changed' });
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname)'), '');
});

test('a source change during sync prevents reporting the old snapshot as complete', async () => {
  const { work, backup, config } = await fixture();
  await writeFile(join(work, 'readme.txt'), 'new source version\n');
  git(work, 'add', 'readme.txt');
  git(work, 'commit', '-m', 'source will change');
  let advanced = false;
  const result = await sync(work, config, { authForUrl: async (url) => {
    if (url === backup && !advanced) { advanced = true; git(work, 'push', 'origin', 'refs/heads/main'); }
    return { env: {} };
  } });
  assert.equal(result.complete, false);
  assert.equal(result.sourceStable, false);
  assert.equal(result.status, 'source_changed_or_unreadable');
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname)'), '');
});

test('submodule gitlinks are reported as content not transferred', async () => {
  const { work, config } = await fixture();
  const commit = git(work, 'rev-parse', 'HEAD');
  git(work, 'update-index', '--add', '--cacheinfo', `160000,${commit},vendor/module`);
  git(work, 'commit', '-m', 'submodule gitlink');
  git(work, 'push', 'origin', 'refs/heads/main');
  const result = await sync(work, config);
  assert.equal(result.refsComplete, true);
  assert.equal(result.complete, false);
  assert.ok(result.contentWarnings.some((warning) => warning.code === 'submodule_content'));
});

test('removed historical LFS pointers and submodules still prevent full completion', async () => {
  const { work, config } = await fixture();
  const commit = git(work, 'rev-parse', 'HEAD');
  await writeFile(join(work, 'old.pointer'), 'version https://git-lfs.github.com/spec/v1\noid sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\nsize 2\n');
  git(work, 'add', 'old.pointer');
  git(work, 'update-index', '--add', '--cacheinfo', `160000,${commit},vendor/old-module`);
  git(work, 'commit', '-m', 'historical external content');
  git(work, 'rm', '--cached', 'vendor/old-module');
  git(work, 'rm', 'old.pointer');
  git(work, 'commit', '-m', 'remove external content');
  git(work, 'push', 'origin', 'refs/heads/main');
  const result = await sync(work, config);
  assert.equal(result.refsComplete, true);
  assert.equal(result.complete, false);
  assert.ok(result.contentWarnings.some((warning) => warning.code === 'submodule_content'));
  assert.ok(result.contentWarnings.some((warning) => warning.code === 'lfs_content'));
});

test('sync audits the published source snapshot and blocks every target on historical secrets', async () => {
  const { dir, work, source, backup, config } = await fixture();
  const token = `ghp_${'f'.repeat(40)}`;
  await writeFile(join(work, 'entry.txt'), `${token}\n`);
  git(work, 'add', 'entry.txt');
  git(work, 'commit', '-m', 'source contains secret');
  git(work, 'rm', 'entry.txt');
  git(work, 'commit', '-m', 'secret removed from tip');
  git(work, 'push', 'origin', 'refs/heads/main');
  const sourceSha = git(source, 'rev-parse', 'refs/heads/main');
  const second = join(dir, 'second.git');
  git(dir, 'init', '--bare', second);
  await addRemote(work, { name: 'second', url: second });
  config.remotes.second = second;
  config.sync.targets.push('second');
  git(work, 'push', 'backup', 'refs/tags/v1:refs/tags/keep');
  const before = git(backup, 'for-each-ref', '--format=%(refname) %(objectname)');
  // Move the local same-name branch to clean history; sync must still audit the remote SHA.
  git(work, 'reset', '--hard', 'refs/tags/v1');
  const result = await sync(work, config);
  assert.equal(result.status, 'audit_blocked');
  assert.equal(result.complete, false);
  assert.equal(result.audit.snapshot['refs/heads/main'], sourceSha);
  assert.ok(result.results.every((entry) => entry.written === false && entry.status === 'blocked'));
  assert.equal(git(backup, 'for-each-ref', '--format=%(refname) %(objectname)'), before);
  assert.equal(git(second, 'for-each-ref', '--format=%(refname)'), '');
  assert.equal(git(work, 'for-each-ref', '--format=%(refname)', 'refs/git-setup'), '');
  assert.ok(!JSON.stringify(result).includes(token));
});
