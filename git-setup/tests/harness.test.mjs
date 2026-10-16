import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, writeFile, readFile, readdir, cp, rename, lstat, rm, chmod, symlink, readlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { devNull, hostname } from 'node:os';
import { planHarness, applyHarness, harnessFacts } from '../scripts/git-setup/harness.mjs';
import { projectOperation } from '../scripts/git-setup/project-entry.mjs';
import { entryCheck } from '../scripts/git-setup/entry-check.mjs';
import { digest } from '../scripts/git-setup/runtime-bundle.mjs';

const exec = promisify(execFile);
const skill = fileURLToPath(new URL('../', import.meta.url));
const base = resolve(process.env.GIT_SETUP_TEST_ROOT || '../../tmp/git-setup/harness-tests');
async function fixture() {
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'harness-')), work = join(root, 'work');
  await mkdir(work);
  const env = { ...process.env, GIT_CEILING_DIRECTORIES: root, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_SETUP_|^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG)/.test(key)) delete env[key];
  const git = async (cwd, ...args) => (await exec('git', ['-C', cwd, '-c', 'commit.gpgsign=false', ...args], { env, timeout: 30000 })).stdout.trim();
  await git(work, 'init', '-q', '-b', 'main');
  await git(work, 'config', 'user.name', 'Harness Fixture'); await git(work, 'config', 'user.email', 'harness@example.invalid');
  await git(work, 'config', 'core.hooksPath', devNull);
  await writeFile(join(work, '.gitignore'), 'data/git-setup/\n.credentials/\n');
  await writeFile(join(work, 'README.md'), 'fixture\n'); await git(work, 'add', 'README.md', '.gitignore'); await git(work, 'commit', '-qm', 'fixture');
  const origin = join(root, 'origin.git'), backup = join(root, 'backup.git');
  for (const [name, path] of [['origin', origin], ['backup', backup]]) { await git(root, 'init', '-q', '--bare', '-b', 'main', path); await git(work, 'remote', 'add', name, path); }
  const planFile = join(work, 'data/git-setup/adoption.json');
  const adopt = async input => {
    const plan = await planHarness(work, input);
    const result = await applyHarness(work, input, { expectedFingerprint: plan.fingerprint, planFile });
    assert.equal(result.status, 'configured', JSON.stringify(result));
    return result;
  };
  const run = async (entry, args = [], cwd = work, input = '') => {
    try {
      const pending = exec(process.execPath, [entry, ...args], { env, cwd, timeout: 90000 });
      pending.child.stdin.end(input);
      const result = await pending;
      return { exit: 0, ...JSON.parse(result.stdout) };
    }
    catch (error) { return { exit: error.code, ...JSON.parse(error.stdout || error.stderr) }; }
  };
  return { root, work, env, git, origin, backup, adopt, planFile, run };
}

function behavior(id, evidence, extra = {}) {
  return { id, summary: '现有项目 Git 入口', trigger: '用户手动调用', inputs: '本仓 origin 和 main', writes: '推送目标引用', failure: '失败停止', evidence, classification: 'standard', ...extra };
}

test('public plans accept standard input without creating input or recovery files', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  const before = await readdir(f.work);
  const result = await f.run(cli, ['harness-plan', '--input', '-'], f.work, JSON.stringify({ choice: 'keep' }));
  assert.equal(result.status, 'planned', JSON.stringify(result));
  assert.deepEqual(await readdir(f.work), before);
  const invalid = await f.run(cli, ['harness-plan', '--input', '-'], f.work, 'private malformed input');
  assert.equal(invalid.code, 'invalid_input_file');
  assert.equal(JSON.stringify(invalid).includes('private malformed input'), false);
});

test('keeping existing tools saves the choice without an installation record and rejects pending installations', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  const input = JSON.stringify({ choice: 'keep' });
  const args = ['--input', '-'];
  const before = await readdir(f.work);
  const plan = await f.run(cli, ['harness-plan', ...args], f.work, input);
  const applied = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply'], f.work, input);
  assert.equal(applied.status, 'configured', JSON.stringify(applied));
  assert.equal((await f.run(cli, ['check'])).harness.choice, 'keep');
  assert.deepEqual(await readdir(f.work), before);
  const next = await f.run(cli, ['harness-plan', ...args], f.work, input);
  const config = await readFile(join(f.work, '.git/config'));
  await writeFile(join(f.work, '.git/git-setup-harness.lock'), 'unfinished installation');
  const denied = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', next.fingerprint, '--apply'], f.work, input);
  assert.equal(denied.code, 'harness_recovery_required');
  assert.deepEqual(await readFile(join(f.work, '.git/config')), config);
});

test('one saved adoption plan is sufficient and successful recovery retains no input or file snapshots', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  const input = JSON.stringify({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] });
  const planned = await f.run(cli, ['harness-plan', '--input', '-', '--plan-file', f.planFile], f.work, input);
  assert.equal(planned.status, 'planned', JSON.stringify(planned));
  const applied = await f.run(cli, ['harness-apply', '--plan-file', f.planFile, '--expected-fingerprint', planned.fingerprint, '--apply']);
  assert.equal(applied.complete, true, JSON.stringify(applied));
  const record = JSON.parse(await readFile(f.planFile, 'utf8'));
  assert.equal(record.kind, 'harness-result');
  assert.equal(record.request.input, undefined);
  assert.equal(record.transaction, undefined);
  const recovered = await f.run(cli, ['harness-recover', '--plan-file', f.planFile]);
  assert.equal(recovered.verification, 'historical_receipt_only');
  assert.equal((await f.run(join(f.work, 'vcs/run.mjs'), ['check'])).status, 'checked');
});

test('public adoption preserves an extension already placed inside the selected Harness and runs it independently', async () => {
  const f = await fixture(), copy = join(f.root, 'skill-copy');
  await cp(skill, copy, { recursive: true });
  const cli = join(copy, 'scripts/git-setup/cli.mjs');
  const directory = 'tools/automation/git', path = `${directory}/extensions/record.mjs`;
  const content = "import { appendFile } from 'node:fs/promises';\nlet raw=''; for await(const c of process.stdin) raw+=c;\nconst input=JSON.parse(raw); await appendFile('data/git-setup/extension-count', input.event+'\\n');\n";
  await mkdir(join(f.work, directory, 'extensions'), { recursive: true });
  await writeFile(join(f.work, path), content, { mode: 0o640 });
  const extension = { id: 'record', path, sha256: digest(content), event: 'after-publish', description: '发布成功后记录事件', timeoutMs: 5000 };
  const input = { choice: 'standard', directory, ruleFiles: ['AGENTS.md'], evidenceFiles: [path],
    comparison: [behavior('record', [path], { classification: 'unique', decision: 'retain', extension: 'record' })], extensions: [extension] };
  const inputFile = join(f.root, 'adoption-input.json'); await writeFile(inputFile, JSON.stringify(input));
  const args = ['--project', f.work, '--input', inputFile, '--plan-file', f.planFile];
  const plan = await f.run(cli, ['harness-plan', ...args], f.root);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  assert.ok(plan.changes.preserved.includes(path));
  const applied = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply'], f.root);
  assert.equal(applied.complete, true, JSON.stringify(applied));
  assert.equal(await readFile(join(f.work, path), 'utf8'), content);
  assert.equal((await lstat(join(f.work, path))).mode & 0o777, 0o640);
  await rename(copy, `${copy}-unavailable`);
  const entry = join(f.work, directory, 'run.mjs');
  assert.equal((await f.run(entry, ['check'], f.root)).status, 'checked');
  const publishArgs = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--operation-id', 'inside-extension-001'];
  const preview = await f.run(entry, publishArgs, f.root);
  assert.equal(preview.status, 'planned', JSON.stringify(preview));
  const result = await f.run(entry, [...publishArgs, '--expected-fingerprint', preview.fingerprint, '--apply'], f.root);
  assert.equal(result.complete, true, JSON.stringify(result));
  assert.equal(await f.git(f.origin, 'rev-parse', 'main'), await f.git(f.work, 'rev-parse', 'main'));
  assert.equal((await f.run(entry, [...publishArgs, '--expected-fingerprint', preview.fingerprint, '--apply'], f.root)).recovered, true);
  assert.equal(await readFile(join(f.work, 'data/git-setup/extension-count'), 'utf8'), 'after-publish\n');
});

test('public upgrade preserves omitted policy selections and exact project files while explicit removal only disables the extension', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs'), directory = 'vcs';
  const path = 'vcs/extensions/record.mjs', content = "import { appendFile } from 'node:fs/promises';\nfor await(const c of process.stdin) {}\nawait appendFile('data/git-setup/upgrade-extension-count', 'ran\\n');\n";
  await mkdir(join(f.work, 'vcs/extensions'), { recursive: true }); await writeFile(join(f.work, path), content);
  const extension = { id: 'record', path, sha256: digest(content), event: 'after-publish', description: '记录发布事件', timeoutMs: 5000 };
  const comparison = [behavior('record', [path], { classification: 'unique', decision: 'retain', extension: 'record' })];
  const inputFile = join(f.root, 'upgrade-input.json');
  const args = ['--project', f.work, '--input', inputFile, '--plan-file', f.planFile];
  async function adopt(input) {
    await writeFile(inputFile, JSON.stringify(input));
    const plan = await f.run(cli, ['harness-plan', ...args]);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    const result = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply']);
    assert.equal(result.complete, true, JSON.stringify(result));
    return plan;
  }
  await adopt({ choice: 'standard', directory, ruleFiles: ['AGENTS.md'], evidenceFiles: [path], comparison, extensions: [extension], sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } });
  const configPath = join(f.work, 'vcs/config.json');
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  const configText = JSON.stringify(config, null, 4)+'\n\n';
  await writeFile(configPath, configText); await chmod(configPath, 0o640);
  const registry = '# No independent repositories selected yet\n';
  await writeFile(join(f.work, 'vcs/repos.conf'), registry, { mode: 0o640 });
  const resource = Buffer.from([0, 255, 1, 254, 13, 10]);
  await writeFile(join(f.work, 'vcs/extensions/resource.bin'), resource, { mode: 0o640 });
  const beforeFiles = await readdir(join(f.work, 'vcs/extensions'));
  const plan = await adopt({ choice: 'standard', comparison });
  for (const name of ['vcs/config.json', 'vcs/repos.conf', path, 'vcs/extensions/resource.bin']) assert.ok(plan.changes.preserved.includes(name), name);
  assert.equal(await readFile(configPath, 'utf8'), configText);
  assert.equal((await lstat(configPath)).mode & 0o777, 0o640);
  assert.equal(await readFile(join(f.work, 'vcs/repos.conf'), 'utf8'), registry);
  assert.deepEqual(await readFile(join(f.work, 'vcs/extensions/resource.bin')), resource);
  assert.equal((await lstat(join(f.work, 'vcs/extensions/resource.bin'))).mode & 0o777, 0o640);
  const entry = join(f.work, 'vcs/run.mjs');
  const publication = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--operation-id', 'upgrade-extension-001'];
  const preview = await f.run(entry, publication, f.root);
  assert.equal((await f.run(entry, [...publication, '--expected-fingerprint', preview.fingerprint, '--apply'], f.root)).complete, true);
  assert.equal(await readFile(join(f.work, 'data/git-setup/upgrade-extension-count'), 'utf8'), 'ran\n');
  await adopt({ choice: 'standard', extensions: [], comparison: [behavior('record', [path], { classification: 'unique', decision: 'drop' })] });
  assert.deepEqual((JSON.parse(await readFile(configPath, 'utf8'))).extensions, []);
  assert.deepEqual(await readdir(join(f.work, 'vcs/extensions')), beforeFiles);
  assert.equal(await readFile(join(f.work, path), 'utf8'), content);
  const noExtensionArgs = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main'];
  const withoutExtension = await f.run(entry, noExtensionArgs);
  assert.equal((await f.run(entry, [...noExtensionArgs, '--expected-fingerprint', withoutExtension.fingerprint, '--apply'])).complete, true);
  assert.equal(await readFile(join(f.work, 'data/git-setup/upgrade-extension-count'), 'utf8'), 'ran\n');
});

test('public upgrades accept changed root targets only when refreshTargets is explicitly true', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await f.adopt({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] });
  const configPath = join(f.work, 'vcs/config.json'), before = await readFile(configPath, 'utf8');
  await f.git(f.work, 'remote', 'set-url', 'origin', f.backup);
  const inputFile = join(f.root, 'refresh-targets.json'), args = ['--input', inputFile, '--plan-file', f.planFile];
  await writeFile(inputFile, JSON.stringify({ choice: 'standard' }));
  const preserved = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(preserved.status, 'planned', JSON.stringify(preserved));
  assert.equal((await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', preserved.fingerprint, '--apply'])).complete, true);
  assert.equal(await readFile(configPath, 'utf8'), before);
  const entry = join(f.work, 'vcs/run.mjs');
  assert.equal((await f.run(entry, ['check'])).code, 'harness_target_changed');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard', refreshTargets: true }));
  const refreshed = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(refreshed.status, 'planned', JSON.stringify(refreshed));
  assert.ok(refreshed.changes.modified.includes('vcs/config.json'));
  assert.equal((await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', refreshed.fingerprint, '--apply'])).complete, true);
  assert.equal((await f.run(entry, ['check'])).status, 'checked');
  for (const input of [{ choice: 'keep', refreshTargets: true }, { choice: 'standard', refreshTargets: 'true' }]) {
    await writeFile(inputFile, JSON.stringify(input));
    const rejected = await f.run(cli, ['harness-plan', ...args]);
    assert.notEqual(rejected.exit, 0, JSON.stringify(rejected));
  }
});

test('public adoption refuses a target directory with unknown empty directories', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await mkdir(join(f.work, 'vcs/user-empty'), { recursive: true });
  const inputFile = join(f.root, 'unknown-directory.json');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] }));
  const rejected = await f.run(cli, ['harness-plan', '--input', inputFile]);
  assert.equal(rejected.code, 'harness_existing_modified', JSON.stringify(rejected));
  assert.equal((await lstat(join(f.work, 'vcs/user-empty'))).isDirectory(), true);
});

test('public adoption refuses existing manifests that parse as falsy JSON without replacing project files', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await mkdir(join(f.work, 'vcs/extensions'), { recursive: true });
  const content = 'keep the original project resource\n';
  await writeFile(join(f.work, 'vcs/extensions/resource.txt'), content);
  const inputFile = join(f.root, 'invalid-manifest.json');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] }));
  for (const value of ['null', 'false', '0', '""']) {
    await writeFile(join(f.work, 'vcs/manifest.json'), value);
    const rejected = await f.run(cli, ['harness-plan', '--input', inputFile]);
    assert.equal(rejected.code, 'harness_existing_modified', JSON.stringify(rejected));
    assert.equal(await readFile(join(f.work, 'vcs/extensions/resource.txt'), 'utf8'), content);
    await assert.rejects(lstat(join(f.work, 'vcs/run.mjs')), { code: 'ENOENT' });
  }
});

test('public upgrade refuses stale project file contents permissions additions and removals', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await f.adopt({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] });
  await assert.rejects(lstat(join(f.work, 'vcs/extensions')), { code: 'ENOENT' });
  await mkdir(join(f.work, 'vcs/extensions'));
  const resource = join(f.work, 'vcs/extensions/resource.bin'), bytes = Buffer.from([0, 1, 255]);
  await writeFile(resource, bytes, { mode: 0o640 });
  const inputFile = join(f.root, 'stale-project-files.json');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard' }));
  const args = ['--input', inputFile, '--plan-file', f.planFile];
  const plan = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  const manifest = await readFile(join(f.work, 'vcs/manifest.json'));
  const added = join(f.work, 'vcs/extensions/added.txt');
  for (const [change, undo] of [
    [() => writeFile(resource, 'changed'), () => writeFile(resource, bytes)],
    [() => chmod(resource, 0o600), () => chmod(resource, 0o640)],
    [() => writeFile(added, 'new project resource'), () => rm(added)],
    [() => rm(resource), () => writeFile(resource, bytes, { mode: 0o640 })],
  ]) {
    await change();
    const denied = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply']);
    assert.equal(denied.code, 'harness_plan_changed', JSON.stringify(denied));
    assert.deepEqual(await readFile(join(f.work, 'vcs/manifest.json')), manifest);
    await assert.rejects(lstat(join(f.work, '.git/git-setup-harness.lock')), { code: 'ENOENT' });
    await undo();
  }
  const applied = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply']);
  assert.equal(applied.complete, true, JSON.stringify(applied));
  assert.deepEqual(await readFile(resource), bytes);
  for (const name of ['unknown.txt', 'runtime/unknown.mjs']) {
    const path = join(f.work, 'vcs', name); await writeFile(path, 'not generator-owned\n');
    const rejected = await f.run(cli, ['harness-plan', ...args]);
    assert.equal(rejected.code, 'harness_existing_modified', JSON.stringify(rejected));
    assert.equal(await readFile(path, 'utf8'), 'not generator-owned\n');
    await rm(path);
  }
});

test('public upgrade accepts the first phase manifest format and preserves its edited project configuration', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await f.adopt({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] });
  const manifestPath = join(f.work, 'vcs/manifest.json'), configPath = join(f.work, 'vcs/config.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  // A v1 format fixture, including its formerly generated config hash.
  manifest.schemaVersion = 1;
  manifest.files['config.json'] = digest(await readFile(configPath));
  await writeFile(manifestPath, JSON.stringify(manifest));
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  config.sync = { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] };
  const edited = JSON.stringify(config, null, 4) + '\n';
  await writeFile(configPath, edited); await chmod(configPath, 0o640);
  const inputFile = join(f.root, 'v1-upgrade.json'); await writeFile(inputFile, JSON.stringify({ choice: 'standard' }));
  const args = ['--input', inputFile, '--plan-file', f.planFile];
  const plan = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  assert.equal((await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply'])).complete, true);
  assert.equal(await readFile(configPath, 'utf8'), edited);
  assert.equal((await lstat(configPath)).mode & 0o777, 0o640);
  assert.equal((await f.run(join(f.work, 'vcs/run.mjs'), ['check'], f.root)).status, 'checked');
});

test('planned project layouts register a portable entry and run from another directory without the Skill', async () => {
  for (const directory of ['vcs', 'tools/automation/project-git']) {
    const f = await fixture(), copy = join(f.root, 'layout-skill-copy');
    await cp(skill, copy, { recursive: true });
    const cli = join(copy, 'scripts/git-setup/cli.mjs');
    const rules = '# Project rules\n\nKeep existing author and files.\n';
    await writeFile(join(f.work, 'AGENTS.md'), rules);
    const inputFile = join(f.root, 'layout-choice.json');
    await writeFile(inputFile, JSON.stringify({ choice: 'standard', directory, ruleFiles: ['AGENTS.md'],
      sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } }));
    const before = await readFile(join(f.work, '.git/config'));
    const args = ['--project', f.work, '--input', inputFile];
    const plan = await f.run(cli, ['harness-plan', ...args], f.root);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    assert.ok(plan.changes.generated.includes(`${directory}/run.mjs`));
    assert.ok(plan.changes.modified.includes('AGENTS.md'));
    assert.equal(await readFile(join(f.work, 'AGENTS.md'), 'utf8'), rules);
    assert.deepEqual(await readFile(join(f.work, '.git/config')), before);
    await assert.rejects(lstat(join(f.work, directory)), { code: 'ENOENT' });
    const applied = await f.run(cli, ['harness-apply', ...args, '--plan-file', f.planFile, '--expected-fingerprint', plan.fingerprint, '--apply'], f.root);
    assert.equal(applied.complete, true, JSON.stringify(applied));
    const registered = await readFile(join(f.work, 'AGENTS.md'), 'utf8');
    assert.ok(registered.startsWith(rules));
    assert.ok(registered.includes(`${directory}/run.mjs`));
    await rename(copy, `${copy}-unavailable`);
    const entry = join(f.work, directory, 'run.mjs');
    assert.equal((await f.run(entry, [], f.root)).status, 'checked');
    assert.equal((await f.run(entry, ['read', '--remote', 'origin'], f.root)).status, 'read');
    const publishArgs = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main'];
    const publication = await f.run(entry, publishArgs, f.root);
    const published = await f.run(entry, [...publishArgs, '--apply', '--expected-fingerprint', publication.fingerprint], f.root);
    assert.equal(published.complete, true, JSON.stringify(published));
    assert.equal(await f.git(f.backup, 'for-each-ref'), '');
    const synchronization = await f.run(entry, ['sync'], f.root);
    const synced = await f.run(entry, ['sync', '--apply', '--expected-fingerprint', synchronization.fingerprint], f.root);
    assert.equal(synced.complete, true, JSON.stringify(synced));
    assert.equal(await f.git(f.origin, 'rev-parse', 'main'), await f.git(f.backup, 'rev-parse', 'main'));
    assert.ok((await readFile(join(f.work, directory, 'README.md'), 'utf8')).includes(`${directory}/run.mjs`));
    assert.equal((await readFile(entry, 'utf8')).includes(f.work), false);
    await assert.rejects(lstat(join(f.work, 'scripts')), { code: 'ENOENT' });
  }
});

test('daily public operations ignore unrelated rules while retaining project runtime and target checks', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await f.git(f.work, 'remote', 'add', 'archive', f.origin);
  await f.adopt({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'], sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } });
  const entry = join(f.work, 'vcs/run.mjs');
  const args = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main'];
  const preview = await f.run(cli, args);
  assert.equal(preview.status, 'planned', JSON.stringify(preview));
  await writeFile(join(f.work, 'AGENTS.md'), '# Updated unrelated prose\n');
  await f.git(f.work, 'remote', 'set-url', 'archive', f.backup);
  await symlink('missing-issue-rules.md', join(f.work, 'CLAUDE.md'));
  const checked = await f.run(entry, ['read', '--remote', 'origin']);
  assert.equal(checked.status, 'read', JSON.stringify(checked));
  assert.equal(checked.agentReview, undefined);
  assert.equal(checked.harness?.choice, undefined);
  assert.equal((await f.run(cli, ['inspect'])).initialized, true);
  const applied = await f.run(cli, [...args, '--apply', '--expected-fingerprint', preview.fingerprint]);
  assert.equal(applied.complete, true, JSON.stringify(applied));
  assert.equal(await f.git(f.origin, 'rev-parse', 'main'), await f.git(f.work, 'rev-parse', 'main'));
  const directSync = await f.run(cli, ['sync']), projectSync = await f.run(entry, ['sync']);
  assert.equal(directSync.status, 'planned', JSON.stringify(directSync));
  assert.equal(directSync.fingerprint, projectSync.fingerprint);
  await writeFile(join(f.work, 'vcs/config.json'), '{broken');
  assert.equal((await f.run(entry, ['read', '--remote', 'origin'])).code, 'harness_config_invalid');
});

test('explicit rule entities survive registration and changed links reject the saved public plan', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  const original = '# Existing rules\nPreserve this content.\n';
  await writeFile(join(f.work, 'AGENTS.md'), original);
  await symlink('AGENTS.md', join(f.work, 'CLAUDE.md'));
  const input = { choice: 'standard', directory: 'tools/git', ruleFiles: ['AGENTS.md', 'CLAUDE.md'] };
  const file = join(f.root, 'rules-choice.json'); await writeFile(file, JSON.stringify(input));
  const args = ['--input', file, '--plan-file', f.planFile];
  const preview = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(preview.status, 'planned', JSON.stringify(preview));
  assert.deepEqual(preview.changes.modified, ['AGENTS.md']);
  await writeFile(join(f.work, 'OTHER.md'), original);
  await rm(join(f.work, 'CLAUDE.md')); await symlink('OTHER.md', join(f.work, 'CLAUDE.md'));
  assert.equal((await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', preview.fingerprint, '--apply'])).code, 'harness_plan_changed');
  await assert.rejects(lstat(join(f.work, 'tools')), { code: 'ENOENT' });
  await rm(join(f.work, 'CLAUDE.md')); await symlink('AGENTS.md', join(f.work, 'CLAUDE.md'));
  assert.equal((await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', preview.fingerprint, '--apply'])).complete, true);
  assert.equal(await readlink(join(f.work, 'CLAUDE.md')), 'AGENTS.md');
  assert.ok((await readFile(join(f.work, 'AGENTS.md'), 'utf8')).startsWith(original));
  const first = await f.run(cli), second = await f.run(cli);
  assert.equal(first.harness.choice, 'standard', JSON.stringify(first));
  assert.equal(first.fingerprint, second.fingerprint);
  await writeFile(join(f.work, 'AGENTS.md'), (await readFile(join(f.work, 'AGENTS.md'), 'utf8')) + '\nChanged project instruction.\n');
  assert.equal((await f.run(cli)).harness.choiceChanged, true);
  await rm(join(f.work, 'tools/git/README.md'));
  assert.equal((await f.run(join(f.work, 'tools/git/run.mjs'))).status, 'checked', 'daily entry does not require generated prose');
});

test('public installation and synchronization never consume legacy project JSON or invent a layout', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await mkdir(join(f.work, '.git-setup'));
  const legacy = join(f.work, '.git-setup/project.json');
  await writeFile(legacy, '{invalid legacy JSON');
  const inputFile = join(f.root, 'no-legacy-choice.json');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard' }));
  assert.equal((await f.run(cli, ['harness-plan', '--input', inputFile])).code, 'harness_layout_required');
  assert.equal((await f.run(cli, ['sync'])).code, 'harness_config_missing');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] }));
  const preview = await f.run(cli, ['harness-plan', '--input', inputFile]);
  assert.equal(preview.status, 'planned', JSON.stringify(preview));
  const applied = await f.run(cli, ['harness-apply', '--input', inputFile, '--plan-file', f.planFile, '--expected-fingerprint', preview.fingerprint, '--apply']);
  assert.equal(applied.complete, true, JSON.stringify(applied));
  assert.equal(await readFile(legacy, 'utf8'), '{invalid legacy JSON');
  await writeFile(legacy, JSON.stringify({ schemaVersion: 1, sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } }));
  const synced = await f.run(cli, ['sync']);
  assert.equal(synced.status, 'error', JSON.stringify(synced));
  assert.equal(JSON.parse(await readFile(join(f.work, 'vcs/config.json'), 'utf8')).sync, null);
  assert.equal(await f.git(f.backup, 'for-each-ref'), '');
});

test('public publish stops when the registered installation disappears instead of bypassing its plan', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  await f.adopt({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] });
  await rename(join(f.work, 'vcs'), join(f.root, 'moved-harness'));
  const denied = await f.run(cli, ['publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--apply']);
  assert.equal(denied.code, 'harness_config_missing', JSON.stringify(denied));
  assert.equal(await f.git(f.origin, 'for-each-ref'), '');
});

test('no-argument check is read-only, reports config origins and does not fabricate an auth choice', async () => {
  const f = await fixture();
  await f.git(f.work, 'remote', 'set-url', 'origin', 'https://github.com/example/project.git');
  await f.git(f.work, 'config', 'credential.https://github.com.helper', 'unselected-fixture-helper');
  await mkdir(join(f.work, '.github/workflows'), { recursive: true });
  await writeFile(join(f.work, '.github/workflows/check.yml'), 'on: push\njobs: {}\n');
  const before = await readFile(join(f.work, '.git/config'));
  const cli = join(skill, 'scripts/git-setup/cli.mjs');
  const result = await f.run(cli);
  assert.equal(result.status, 'checked');
  assert.ok(result.configuration.some(field => field.key === 'user.email' && field.scope === 'local' && field.origin.endsWith('/config')));
  assert.equal(result.authentication.find(item => item.url.includes('github')).status, 'needs_input');
  assert.equal(result.authenticationPolicy.observedHelpersAreSelections, false);
  assert.deepEqual(result.authenticationPolicy.githubHttpsChoices, ['gh', 'github-token']);
  assert.match(result.configuration.find(field => field.key === 'credential.https://github.com.helper').authenticationEvidence, /not_an_authentication_choice/);
  assert.equal(JSON.stringify(result).includes('unselected-fixture-helper'), false);
  assert.equal(result.harness.automation.localHooks.status, 'disabled');
  assert.equal(result.harness.automation.remoteCI.status, 'not_verified');
  assert.ok(result.harness.candidates.some(item => item.path.endsWith('.github/workflows/check.yml')));
  assert.equal(result.network, 'not_performed');
  assert.deepEqual(await readFile(join(f.work, '.git/config')), before);
  await assert.rejects(lstat(join(f.work, '.credentials')), { code: 'ENOENT' });
  await assert.rejects(lstat(join(f.work, 'data')), { code: 'ENOENT' });
});

test('fresh projects get a self-contained entry that publishes and syncs after the test Skill installation moves', async () => {
  const f = await fixture(), copy = join(f.root, 'skill-copy');
  await cp(skill, copy, { recursive: true });
  const cli = join(copy, 'scripts/git-setup/cli.mjs');
  const input = { choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'], sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } };
  const inputFile = join(f.root, 'choice.json'); await writeFile(inputFile, JSON.stringify(input));
  const plan = await f.run(cli, ['harness-plan', '--input', inputFile]);
  assert.equal(plan.status, 'planned');
  const applied = await f.run(cli, ['harness-apply', '--input', inputFile, '--plan-file', f.planFile, '--expected-fingerprint', plan.fingerprint, '--apply']);
  assert.equal(applied.status, 'configured', JSON.stringify(applied));
  const modules = await readdir(join(f.work, 'scripts/git-setup/runtime/scripts/git-setup'));
  for (const name of ['cli.mjs', 'issues.mjs', 'issue-rules.mjs', 'issue-operation.mjs', 'setup.mjs', 'harness.mjs', 'harness-transaction.mjs']) assert.equal(modules.includes(name), false, name);
  await rename(copy, `${copy}-unavailable`);
  const entry = join(f.work, 'scripts/git-setup/run.mjs');
  assert.equal((await f.run(entry)).status, 'checked');
  assert.equal((await f.run(entry, ['auth-info'])).exit, 0);
  assert.equal((await f.run(entry, ['read', '--remote', 'origin'])).status, 'read');
  const publishArgs = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main'];
  const publishing = await f.run(entry, publishArgs);
  assert.equal(publishing.status, 'planned');
  const published = await f.run(entry, [...publishArgs, '--apply', '--expected-fingerprint', publishing.fingerprint]);
  assert.equal(published.complete, true, JSON.stringify(published));
  assert.equal(await f.git(f.backup, 'for-each-ref'), '', 'publish must not implicitly sync');
  const syncing = await f.run(entry, ['sync']);
  const synced = await f.run(entry, ['sync', '--apply', '--expected-fingerprint', syncing.fingerprint]);
  assert.equal(synced.complete, true, JSON.stringify(synced));
  assert.equal(await f.git(f.origin, 'rev-parse', 'main'), await f.git(f.backup, 'rev-parse', 'main'));
  await assert.rejects(lstat(join(f.work, '.git-setup')), { code: 'ENOENT' });
  assert.equal((await readFile(join(f.work, 'scripts/git-setup/manifest.json'), 'utf8')).includes(copy), false);
});

test('ordinary Git uses the repository helper after the installing Skill copy moves; no fallback on removal', async () => {
  const f = await fixture(), copy = join(f.root, 'auth-skill-copy');
  await cp(skill, copy, { recursive: true });
  const script = `
    const a = await import(${JSON.stringify(pathToFileURL(join(copy, 'scripts/git-setup/auth.mjs')).href)});
    const fs = await import('node:fs/promises');
    const p = await a.prepareAuth(${JSON.stringify(f.work)}, 'https://github.com/example/project.git', { authMethod: 'github-token', apply: true });
    await fs.writeFile(p.credentialFile, 'fixture-local-only-token\\n');
    const s = await a.verifySelection(await a.authSelection(${JSON.stringify(f.work)}, 'https://github.com/example/project.git', { credentialsFilled: true }), { apiTransport: async () => ({status:200, data:{login:'fixture', id:123}, headers:{}}) });
    await a.installAuth(${JSON.stringify(f.work)}, s);
  `;
  await exec(process.execPath, ['--input-type=module', '-e', script], { env: f.env, timeout: 60000 });
  const config = await readFile(join(f.work, '.git/config'), 'utf8');
  assert.equal(config.includes(copy), false);
  assert.equal(config.includes('fixture-local-only-token'), false);
  assert.ok(config.includes('/git-setup/'));
  const installed = (await readdir(join(f.work, '.git/git-setup'))).filter(name => /^[a-f0-9]{64}$/.test(name));
  assert.equal(installed.length, 1);
  const modules = await readdir(join(f.work, '.git/git-setup', installed[0], 'scripts/git-setup'));
  for (const name of ['cli.mjs', 'issues.mjs', 'project-entry.mjs', 'harness.mjs', 'harness-transaction.mjs']) assert.equal(modules.includes(name), false, name);
  await rename(copy, `${copy}-unavailable`);
  const fill = async () => new Promise(done => {
    const child = spawn('git', ['-C', f.work, 'credential', 'fill'], { env: { ...f.env, GIT_ASKPASS: 'echo forbidden-fallback' }, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = ''; child.stdout.on('data', chunk => { stdout += chunk; }); child.stderr.resume();
    const timer = setTimeout(() => child.kill('SIGKILL'), 20000);
    child.on('close', code => { clearTimeout(timer); done({ code, matched: stdout.includes('password=fixture-local-only-token'), fallback: stdout.includes('forbidden-fallback') }); });
    child.stdin.end('protocol=https\nhost=github.com\npath=example/project.git\n\n');
  });
  assert.deepEqual(await fill(), { code: 0, matched: true, fallback: false });
  await rename(join(f.work, '.credentials/repositories/example--project.github-token'), join(f.work, '.credentials/repositories/removed.github-token'));
  const failed = await fill(); assert.notEqual(failed.code, 0); assert.equal(failed.fallback, false);
});

test('keep is independent of adopting standard; subsequent entry checks detect changed scripts and CI', async () => {
  const f = await fixture(); await mkdir(join(f.work, 'scripts'));
  const legacy = join(f.work, 'scripts/legacy.sh'); await writeFile(legacy, '#!/bin/sh\ngit push origin main\n');
  const input = { choice: 'keep', comparison: [behavior('legacy', ['scripts/legacy.sh'])] };
  await f.adopt(input);
  assert.equal((await harnessFacts(f.work)).choice, 'keep');
  await assert.rejects(lstat(join(f.work, 'scripts/git-setup')), { code: 'ENOENT' });
  await writeFile(legacy, '#!/bin/sh\ngit push origin refs/tags/v1\n');
  assert.equal((await entryCheck(f.work)).harness.choiceChanged, true);
  await mkdir(join(f.work, '.github/workflows'), { recursive: true }); await writeFile(join(f.work, '.github/workflows/publish.yml'), 'run: scripts/legacy.sh\n');
  assert.ok((await entryCheck(f.work)).harness.candidates.some(file => file.evidence.endsWith('publish.yml')));
});

test('adoption requires explicit unique-function choices, preserves Issue config and only rewrites planned old entries', async () => {
  const f = await fixture(); await mkdir(join(f.work, 'scripts'));
  await writeFile(join(f.work, 'scripts/legacy.sh'), '#!/bin/sh\ngit push origin main\n');
  await mkdir(join(f.work, '.git-setup'));
  const legacy = '{"schemaVersion":1,"issues":{"backend":"local","target":"current","local":{"directory":"tickets"}}}\n';
  await writeFile(join(f.work, '.git-setup/project.json'), legacy);
  const comparison = [behavior('old-publish', ['scripts/legacy.sh'], { classification: 'unique' })];
  await assert.rejects(planHarness(f.work, { choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'], comparison }), { code: 'harness_behavior_unresolved' });
  const content = '#!/bin/sh\nnode scripts/git-setup/run.mjs "$@"\n';
  await f.adopt({ choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'], comparison: [{ ...comparison[0], decision: 'drop' }], edits: [{ path: 'scripts/legacy.sh', content }] });
  assert.equal(await readFile(join(f.work, '.git-setup/project.json'), 'utf8'), legacy);
  assert.equal(await readFile(join(f.work, 'scripts/legacy.sh'), 'utf8'), content);
  assert.equal((await harnessFacts(f.work)).choice, 'standard');
});

test('plan drift, modified runtime, and a retargeted remote stop before writes without using legacy sync', async () => {
  const f = await fixture(); const input = { choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'] };
  const plan = await planHarness(f.work, input);
  await f.git(f.work, 'remote', 'set-url', 'origin', f.backup);
  await assert.rejects(applyHarness(f.work, input, { expectedFingerprint: plan.fingerprint, planFile: f.planFile }), { code: 'harness_plan_changed' });
  await assert.rejects(lstat(join(f.work, 'scripts')), { code: 'ENOENT' });
  await f.git(f.work, 'remote', 'set-url', 'origin', f.origin); await f.adopt(input);
  await mkdir(join(f.work, '.git-setup'));
  await writeFile(join(f.work, '.git-setup/project.json'), JSON.stringify({ schemaVersion: 1, sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } }));
  const config = join(f.work, 'scripts/git-setup/config.json'), saved = await readFile(config);
  await rm(config);
  assert.equal((await f.run(join(skill, 'scripts/git-setup/cli.mjs'), ['sync', '--apply'])).code, 'harness_config_missing');
  await writeFile(config, saved);
  await f.git(f.work, 'remote', 'set-url', 'origin', f.backup);
  await assert.rejects(projectOperation(f.work, 'publish', { remote: 'origin', refs: ['refs/heads/main'] }), { code: 'harness_target_changed' });
  await f.git(f.work, 'remote', 'set-url', 'origin', f.origin);
  await writeFile(join(f.work, 'scripts/git-setup/runtime/scripts/git-setup/git.mjs'), "import { writeFileSync } from 'node:fs'; writeFileSync('unexpected-import', 'executed');\n");
  const denied = await f.run(join(f.work, 'scripts/git-setup/run.mjs'), ['read', '--remote', 'origin']);
  assert.equal(denied.code, 'harness_runtime_invalid');
  await assert.rejects(lstat(join(f.work, 'unexpected-import')), { code: 'ENOENT' });
  await assert.rejects(projectOperation(f.work, 'read', { remote: 'origin' }), { code: 'harness_runtime_invalid' });
  assert.equal(await f.git(f.origin, 'for-each-ref'), '');
});

test('selected extension runs once after verified Git success and recovery never duplicates it', async () => {
  const f = await fixture(); await mkdir(join(f.work, 'scripts/project-release'), { recursive: true });
  const path = 'scripts/project-release/record.mjs';
  const content = "import { appendFile } from 'node:fs/promises';\nlet raw=''; for await(const c of process.stdin) raw+=c;\nconst input=JSON.parse(raw); await appendFile('data/git-setup/extension-count', input.event+'\\n');\n";
  await writeFile(join(f.work, path), content);
  await mkdir(join(f.work, '.githooks'));
  await writeFile(join(f.work, '.githooks/pre-push'), '#!/bin/sh\nprintf "legacy\\n" >> data/git-setup/legacy-count\n');
  await chmod(join(f.work, '.githooks/pre-push'), 0o755);
  await f.git(f.work, 'config', 'core.hooksPath', '.githooks');
  const extension = { id: 'record-release', path, sha256: digest(content), event: 'after-publish', description: '在确认发布完成后记录一次事件', timeoutMs: 5000 };
  await f.adopt({ choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'], evidenceFiles: [path], comparison: [behavior('record-release', [path, '.githooks/pre-push', '@git-hooks/pre-push'], { classification: 'unique', decision: 'retain', extension: extension.id })], extensions: [extension], edits: [{ path: '.githooks/pre-push', content: '#!/bin/sh\nexit 0\n' }] });
  const upgrade = await planHarness(f.work, { choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'], comparison: [behavior('reviewed', [path, '.githooks/pre-push', '@git-hooks/pre-push'])] });
  assert.deepEqual(upgrade.config.extensions, [extension]);
  const entry = join(f.work, 'scripts/git-setup/run.mjs');
  const args = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--operation-id', 'publish-operation-001'];
  const plan = await f.run(entry, args);
  const result = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(result.complete, true, JSON.stringify(result));
  assert.equal(result.extensions[0].status, 'completed');
  const recovered = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(recovered.recovered, true);
  assert.equal(recovered.verification, 'historical_receipt_only');
  assert.equal(recovered.actualPush, 'not_performed');
  const receipt = JSON.parse(await readFile(join(f.work, 'data/git-setup/harness-publish-operation-001.json'), 'utf8'));
  assert.equal(receipt.request.operationId, 'publish-operation-001');
  assert.equal(await readFile(join(f.work, 'data/git-setup/extension-count'), 'utf8'), 'after-publish\n');
  await assert.rejects(lstat(join(f.work, 'data/git-setup/legacy-count')), { code: 'ENOENT' });
});

test('public recovery preserves internal project files across the backup and stage window and refuses later changes', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  const path = 'vcs/extensions/record.mjs', content = 'for await (const chunk of process.stdin) {}\n';
  await mkdir(join(f.work, 'vcs/extensions'), { recursive: true });
  await writeFile(join(f.work, path), content, { mode: 0o640 });
  const comparison = [behavior('record', [path], { classification: 'unique', decision: 'retain', extension: 'record' })];
  await f.adopt({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'], evidenceFiles: [path], comparison,
    extensions: [{ id: 'record', path, sha256: digest(content), event: 'after-publish', description: '记录发布', timeoutMs: 5000 }] });
  const inputFile = join(f.root, 'recover-extension.json');
  await writeFile(inputFile, JSON.stringify({ choice: 'standard', comparison, sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } }));
  const args = ['--input', inputFile, '--plan-file', f.planFile];
  const plan = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  await writeFile(join(f.work, '.git/config.lock'), 'fixture busy\n');
  const interrupted = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply']);
  assert.equal(interrupted.code, 'setup_config_busy', JSON.stringify(interrupted));
  await rm(join(f.work, '.git/config.lock'));
  // Reconstruct a persisted rename window from the actual staged package and backup.
  // This tests new-process recovery, not SIGKILL injection at every filesystem call.
  const stage = join(f.work, '.git-setup-stage-1f807067-afce-45c3-a2c8-f3a98a542893');
  await rename(join(f.work, 'vcs'), stage);
  const record = JSON.parse(await readFile(f.planFile, 'utf8'));
  record.transaction.stage = stage;
  await writeFile(f.planFile, JSON.stringify(record));
  const recoverArgs = ['harness-recover', '--plan-file', f.planFile];
  const recovery = await f.run(cli, recoverArgs);
  assert.equal(recovery.status, 'recovery_ready', JSON.stringify(recovery));
  const staged = join(stage, 'extensions/record.mjs'), original = join(interrupted.backup, 'extensions/record.mjs');
  await chmod(staged, 0o600);
  assert.equal((await f.run(cli, [...recoverArgs, '--apply', '--expected-fingerprint', recovery.fingerprint])).code, 'harness_recovery_changed');
  assert.equal((await lstat(staged)).mode & 0o777, 0o600);
  await chmod(staged, 0o640);
  await writeFile(original, `${content}// later change\n`);
  assert.equal((await f.run(cli, recoverArgs)).code, 'harness_recovery_changed');
  assert.equal(await readFile(original, 'utf8'), `${content}// later change\n`);
  await writeFile(original, content);
  const ready = await f.run(cli, recoverArgs);
  assert.equal(ready.status, 'recovery_ready', JSON.stringify(ready));
  const resumed = await f.run(cli, [...recoverArgs, '--apply', '--expected-fingerprint', ready.fingerprint]);
  assert.equal(resumed.complete, true, JSON.stringify(resumed));
  assert.equal(await readFile(join(f.work, path), 'utf8'), content);
  assert.equal((await lstat(join(f.work, path))).mode & 0o777, 0o640);
  assert.equal((await f.run(join(f.work, 'vcs/run.mjs'), ['check'], f.root)).status, 'checked');
  await assert.rejects(lstat(stage), { code: 'ENOENT' });
  await assert.rejects(lstat(interrupted.backup), { code: 'ENOENT' });
});

test('interrupted public adoption retains evidence and resumes only verified original changes', async () => {
  const f = await fixture(), cli = join(skill, 'scripts/git-setup/cli.mjs');
  const input = { choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'] };
  await f.adopt(input);
  const oldManifest = await readFile(join(f.work, 'vcs/manifest.json'));
  input.sync = { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] };
  const file = join(f.root, 'recovery-input.json'); await writeFile(file, JSON.stringify(input));
  const args = ['--input', file, '--plan-file', f.planFile];
  const plan = await f.run(cli, ['harness-plan', ...args]);
  await writeFile(join(f.work, '.git/config.lock'), 'fixture busy\n');
  const interrupted = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply']);
  assert.equal(interrupted.status, 'incomplete', JSON.stringify(interrupted));
  assert.equal(interrupted.code, 'setup_config_busy');
  assert.ok(interrupted.completed.includes('generated'));
  assert.deepEqual(await readFile(join(interrupted.backup, 'manifest.json')), oldManifest);
  const lockBefore = await readFile(join(f.work, '.git/git-setup-harness.lock'));
  await rm(join(f.work, '.git/config.lock'));
  const recordBefore = await readFile(f.planFile);
  const overwrite = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(overwrite.code, 'harness_recovery_required', JSON.stringify(overwrite));
  assert.deepEqual(await readFile(f.planFile), recordBefore);
  const recovery = await f.run(cli, ['harness-recover', '--plan-file', f.planFile]);
  assert.equal(recovery.status, 'recovery_ready', JSON.stringify(recovery));
  assert.deepEqual(await readFile(f.planFile), recordBefore);
  const originalRules = await readFile(join(f.work, 'AGENTS.md'));
  await writeFile(join(f.work, 'AGENTS.md'), 'user changes after interruption\n');
  const denied = await f.run(cli, ['harness-recover', '--plan-file', f.planFile, '--expected-fingerprint', recovery.fingerprint, '--apply']);
  assert.equal(denied.code, 'harness_recovery_changed', JSON.stringify(denied));
  assert.equal(await readFile(join(f.work, 'AGENTS.md'), 'utf8'), 'user changes after interruption\n');
  await writeFile(join(f.work, 'AGENTS.md'), originalRules);
  // Reproduce persisted checkpoints after backup cleanup and a recovery crash.
  // The public recovery must infer these states from files, not receipt success.
  await rm(interrupted.backup, { recursive: true });
  const transaction = JSON.parse(recordBefore).transaction;
  const deadPid = Number((await exec(process.execPath, ['-e', 'process.stdout.write(String(process.pid))'], { env: f.env })).stdout);
  const successor = join(f.work, `.git/git-setup-harness.lock.recovery-${transaction.owner.id}`);
  const owner = { id: 'b9ed51dd-3dd7-4990-b32b-810d958fa12d', pid: process.pid, host: hostname(), transaction: transaction.owner.id };
  await writeFile(successor, JSON.stringify(owner));
  assert.equal((await f.run(cli, ['harness-recover', '--plan-file', f.planFile])).code, 'harness_recovery_busy');
  await writeFile(successor, JSON.stringify({ ...owner, pid: deadPid }));
  const afterCleanup = await f.run(cli, ['harness-recover', '--plan-file', f.planFile]);
  assert.equal(afterCleanup.status, 'recovery_ready', JSON.stringify(afterCleanup));
  assert.equal((await f.run(cli, ['harness-recover', '--plan-file', f.planFile, '--expected-fingerprint', recovery.fingerprint, '--apply'])).code, 'harness_recovery_changed');
  const resumed = await f.run(cli, ['harness-recover', '--plan-file', f.planFile, '--expected-fingerprint', afterCleanup.fingerprint, '--apply']);
  assert.equal(resumed.complete, true, JSON.stringify(resumed));
  await assert.rejects(lstat(interrupted.backup), { code: 'ENOENT' });
  await assert.rejects(lstat(join(f.work, '.git/git-setup-harness.lock')), { code: 'ENOENT' });
  assert.equal((await f.run(cli)).harness.choice, 'standard');
  await assert.rejects(lstat(successor), { code: 'ENOENT' });
  // Reconstruct the crash window before lock release and result compaction.
  const beforeCompaction = JSON.parse(recordBefore);
  beforeCompaction.result.complete = true;
  beforeCompaction.result.status = 'configured';
  await writeFile(f.planFile, JSON.stringify(beforeCompaction));
  await writeFile(join(f.work, '.git/git-setup-harness.lock'), lockBefore);
  const unlock = await f.run(cli, ['harness-recover', '--plan-file', f.planFile]);
  assert.equal(unlock.status, 'recovery_ready', JSON.stringify(unlock));
  assert.equal((await f.run(cli, ['harness-recover', '--plan-file', f.planFile, '--expected-fingerprint', unlock.fingerprint, '--apply'])).complete, true);
  const receipt = await readFile(f.planFile);
  const nextPlan = await f.run(cli, ['harness-plan', '--input', file]);
  await writeFile(join(f.work, '.git/git-setup-harness.lock'), 'fixture competing apply\n');
  const busy = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', nextPlan.fingerprint, '--apply']);
  assert.equal(busy.status, 'incomplete');
  assert.deepEqual(await readFile(f.planFile), receipt);
});

test('extension failure is recorded as uncertain and does not retry on the next invocation', async () => {
  const f = await fixture(); await mkdir(join(f.work, 'scripts/extensions'), { recursive: true });
  const path = 'scripts/extensions/fail.mjs', content = "process.stdin.resume(); process.exitCode = 1;\n";
  await writeFile(join(f.work, path), content);
  const extension = { id: 'failure', path, sha256: digest(content), event: 'after-publish', description: '测试失败后的停止行为', timeoutMs: 5000 };
  await f.adopt({ choice: 'standard', directory: 'scripts/git-setup', ruleFiles: ['AGENTS.md'], evidenceFiles: [path], comparison: [behavior('failure', [path], { classification: 'unique', decision: 'retain', extension: 'failure' })], extensions: [extension] });
  const entry = join(f.work, 'scripts/git-setup/run.mjs');
  const args = ['publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--operation-id', 'failure-operation-001'];
  const plan = await f.run(entry, args);
  const result = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(result.status, 'extension_uncertain'); assert.equal(result.gitComplete, true);
  assert.equal((await f.run(entry, [...args, '--apply'])).code, 'harness_extension_uncertain');
});
