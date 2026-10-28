import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { repositoryFixture, cli } from './fixtures/repositories.mjs';
import { gitService } from './fixtures/git-service.mjs';
import { issueAPI } from './fixtures/issue-api.mjs';
import { issueInput, inputFile } from './fixtures/issue-input.mjs';

test('CLI setup, read, publish, sync and Issue operations use each target project Token without source arguments', async t => {
  const f = await repositoryFixture(), project = await f.repo('project');
  const targets = { 'example/project': 'fixture-project-token', 'example/mirror': 'fixture-mirror-token', 'example/tickets': 'fixture-issue-token' };
  const git = await gitService(t, f, targets, { hosts: ['github.com'] });
  const api = await issueAPI(t, f, targets);
  const run = (command, ...args) => f.run(cli, [command, '--project', project, ...args]);
  const urls = Object.fromEntries(Object.keys(targets).map(name => [name, `https://github.com/${name}.git`]));
  const files = {};
  for (const [name, value] of Object.entries(targets)) {
    const prepared = await run('credential-prepare', '--url', urls[name], '--apply');
    assert.equal(prepared.status, 'awaiting_credentials', JSON.stringify(prepared));
    assert.equal(prepared.method, 'github-token');
    files[name] = prepared.credentialFile;
    await writeFile(prepared.credentialFile, value);
  }
  // Ambient tokens and a global helper must not replace the project sources.
  f.env.GH_TOKEN = 'fixture-global-token';
  f.env.GITHUB_TOKEN = 'fixture-global-token';
  const globalHelper = join(f.root, 'global-helper-called');
  await f.git(project, 'config', 'credential.helper', `!f() { touch '${globalHelper.replaceAll('\\', '/')}'; }; f`);
  const configBefore = await f.git(project, 'config', '--local', '--get-all', 'credential.helper');

  const plan = await run('setup', '--url', urls['example/project']);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  const setup = await run('setup', '--url', urls['example/project'], '--apply', '--expected-fingerprint', plan.fingerprint);
  assert.equal(setup.status, 'configured', JSON.stringify(setup));
  const read = await run('read', '--remote', 'origin');
  assert.equal(read.status, 'read', JSON.stringify(read));
  assert.equal(read.authentication[0].source.file, files['example/project']);

  const publishArgs = ['--remote', 'origin', '--ref', 'refs/heads/main'];
  const publication = await run('publish', ...publishArgs);
  assert.equal(publication.status, 'planned', JSON.stringify(publication));
  const published = await run('publish', ...publishArgs, '--apply', '--expected-fingerprint', publication.fingerprint);
  assert.equal(published.complete, true, JSON.stringify(published));
  const head = await f.git(project, 'rev-parse', 'HEAD');
  assert.equal(await f.git(git.repositories['example/project'], 'rev-parse', 'refs/heads/main'), head);
  await f.git(project, 'remote', 'add', 'backup', urls['example/mirror']);
  const syncInput = join(f.root, 'sync.json');
  await writeFile(syncInput, JSON.stringify({ sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } }));
  const preview = await run('sync', '--input', syncInput);
  assert.equal(preview.status, 'planned', JSON.stringify(preview));
  const synced = await run('sync', '--input', syncInput, '--apply', '--expected-fingerprint', preview.fingerprint);
  assert.equal(synced.complete, true, JSON.stringify(synced));
  assert.deepEqual(new Set(synced.authentication.map(item => item.source.file)), new Set([files['example/project'], files['example/mirror']]));
  assert.equal(await f.git(git.repositories['example/mirror'], 'rev-parse', 'refs/heads/main'), head);

  const input = await issueInput(project);
  input.target = { scope: 'current', project, backend: 'github', github: { host: 'github.com', repository: 'example/tickets' } };
  const applyIssue = async (command, operation) => {
    const file = await inputFile(f, operation), resolved = await run('issue-resolve', '--input', file);
    assert.equal(resolved.status, 'planned', JSON.stringify(resolved));
    assert.equal(resolved.authentication.source.file, files['example/tickets']);
    const result = await run(command, '--input', file, '--apply', '--expected-fingerprint', resolved.fingerprint);
    assert.equal(result.status, 'verified', JSON.stringify(result));
    assert.equal((await run('issue-recover', '--input', file)).status, 'verified');
    return result;
  };
  const created = await applyIssue('issue-create', input);
  await applyIssue('issue-update', { ...input, action: 'update', operationId: randomUUID(), existing: { url: created.url }, content: { title: 'Updated with project Token' } });
  await applyIssue('issue-comment', { ...input, action: 'comment', operationId: randomUUID(), existing: { url: created.url }, content: { body: 'Comment with project Token' } });
  assert.equal(api.issues.length, 1); assert.equal(api.comments.length, 1);
  assert.ok(api.requests.every(request => request.authorized));

  await rm(files['example/mirror']);
  const blockedSync = await run('sync', '--input', syncInput);
  assert.equal(blockedSync.preview.complete, false);
  assert.equal(blockedSync.preview.results[0].status, 'failed');
  assert.ok(blockedSync.preview.results[0].error.includes(files['example/mirror']));
  assert.deepEqual(blockedSync.entryCheck.authentication.find(item => item.source?.file === files['example/mirror']).required, ['credential-prepare']);
  await writeFile(files['example/tickets'], 'fixture-invalid-token');
  const blockedIssue = await run('issue-resolve', '--input', await inputFile(f, input));
  assert.equal(blockedIssue.code, 'http-error');
  assert.ok(blockedIssue.message.includes(files['example/tickets']));
  assert.equal(await f.git(project, 'config', '--local', '--get-all', 'credential.helper'), configBefore);
  await assert.rejects(readFile(globalHelper), { code: 'ENOENT' });
  await assert.rejects(readFile(join(project, '.credentials/repositories/sources.local.md')), { code: 'ENOENT' });
  for (const result of [setup, read, published, synced, created, blockedSync, blockedIssue]) {
    for (const value of [...Object.values(targets), 'fixture-global-token', 'fixture-invalid-token']) assert.ok(!JSON.stringify(result).includes(value));
  }
});
