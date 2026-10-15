import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, writeFile, readFile, cp, rename, lstat, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { repositoryFixture, skill } from './fixtures/repositories.mjs';

test('public adoption registers explicit project commands without running them during ordinary publish or sync', async () => {
  const f = await repositoryFixture(), project = await f.repo('manager');
  const copy = join(f.root, 'installer'); await cp(skill, copy, { recursive: true });
  const cli = join(copy, 'scripts/git-setup/cli.mjs');
  const origin = join(f.root, 'origin.git'), mirror = join(f.root, 'mirror.git');
  for (const [name, path] of [['origin', origin], ['mirror', mirror]]) {
    await f.git(f.root, 'init', '--bare', '-q', '-b', 'main', path);
    await f.git(project, 'remote', 'add', name, path);
  }
  const path = 'vcs/extensions/command.mjs', content = "import { writeFile } from 'node:fs/promises'; await writeFile(new URL('../../data/git-setup/explicit-ran', import.meta.url), 'called explicitly\\n'); console.log(JSON.stringify({status:'called'}));\n";
  await mkdir(join(project, 'vcs/extensions'), { recursive: true });
  await writeFile(join(project, path), content);
  const extension = { id: 'project-command', path, sha256: createHash('sha256').update(content).digest('hex'), event: 'explicit', description: '只在 Agent 明确调用项目命令时写记录' };
  const comparison = [{ id: 'project-command', summary: extension.description, trigger: '明确执行项目命令', inputs: '命令自己的参数', writes: '测试记录', failure: '保留记录并停止', evidence: [path], classification: 'unique', decision: 'retain', extension: extension.id }];
  const input = { choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'], evidenceFiles: [path], comparison, extensions: [extension], sync: { source: 'origin', targets: ['mirror'], refs: ['refs/heads/main'] } };
  const file = join(f.root, 'input.json'), receipt = join(project, 'data/git-setup/adopt.json');
  await writeFile(file, JSON.stringify(input));
  const args = ['--project', project, '--input', file, '--plan-file', receipt];
  const plan = await f.run(cli, ['harness-plan', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  assert.deepEqual(plan.config.extensions, [extension]);
  await writeFile(join(project, path), content+'// changed after review\n');
  assert.equal((await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply'])).exit, 1);
  await assert.rejects(lstat(join(project, 'vcs/run.mjs')), { code: 'ENOENT' });
  await writeFile(join(project, path), content);
  const applied = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--apply']);
  assert.equal(applied.complete, true, JSON.stringify(applied));
  await rename(copy, `${copy}-removed`);
  const entry = join(project, 'vcs/run.mjs');
  const listed = await f.run(entry, ['project-entries']);
  assert.equal(listed.status, 'project_entries_verified', JSON.stringify(listed));
  assert.equal(listed.entries[0].path, path);
  assert.equal(listed.entries[0].sha256, extension.sha256);
  assert.equal(listed.network, 'not_performed');
  for (const args of [['publish', '--remote', 'origin', '--ref', 'refs/heads/main'], ['sync']]) {
    const plan = await f.run(entry, args);
    const result = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
    assert.equal(result.complete, true, JSON.stringify(result));
    assert.deepEqual(result.extensions, []);
    await assert.rejects(lstat(join(project, 'data/git-setup/explicit-ran')), { code: 'ENOENT' });
  }
  assert.equal(await f.git(mirror, 'rev-parse', 'main'), await f.git(project, 'rev-parse', 'main'));
  assert.equal((await f.run(join(project, path), [])).status, 'called');
  assert.equal(await readFile(join(project, 'data/git-setup/explicit-ran'), 'utf8'), 'called explicitly\n');
  await writeFile(join(project, path), content+'// later change\n');
  assert.equal((await f.run(entry, ['project-entries'])).code, 'harness_extension_changed');
  await writeFile(join(project, path), content);

  await rename(`${copy}-removed`, copy);
  // An interrupted upgrade must retain the original explicit registration and file.
  await writeFile(file, JSON.stringify({ choice: 'standard', comparison, sync: null }));
  const upgrade = await f.run(cli, ['harness-plan', ...args]);
  assert.deepEqual(upgrade.config.extensions, [extension]);
  await writeFile(join(project, '.git/config.lock'), 'busy\n');
  const stopped = await f.run(cli, ['harness-apply', ...args, '--expected-fingerprint', upgrade.fingerprint, '--apply']);
  assert.equal(stopped.code, 'setup_config_busy', JSON.stringify(stopped));
  await rm(join(project, '.git/config.lock'));
  const ready = await f.run(cli, ['harness-recover', '--project', project, '--plan-file', receipt]);
  assert.equal(ready.status, 'recovery_ready', JSON.stringify(ready));
  const recovered = await f.run(cli, ['harness-recover', '--project', project, '--plan-file', receipt, '--apply', '--expected-fingerprint', ready.fingerprint]);
  assert.equal(recovered.complete, true, JSON.stringify(recovered));
  assert.deepEqual(JSON.parse(await readFile(join(project, 'vcs/config.json'), 'utf8')).extensions, [extension]);
  assert.equal(await readFile(join(project, path), 'utf8'), content);
  assert.equal((await f.run(entry, ['project-entries'])).status, 'project_entries_verified');
});
