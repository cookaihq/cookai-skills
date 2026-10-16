import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { repositoryFixture, cli } from './fixtures/repositories.mjs';
import { issueInput, inputFile } from './fixtures/issue-input.mjs';
import { issueAPI } from './fixtures/issue-api.mjs';

async function fixture(t) {
  const f = await repositoryFixture(), project = await f.repo('source'), target = await f.repo('tickets');
  const api = await issueAPI(t, f, { 'example/tickets': 'fixture-tickets-token' });
  await writeFile(join(project, '.env.git-setup'), 'GIT_SETUP_GITHUB_TOKEN=wrong-source-token\nGIT_SETUP_GITHUB_HOST=github.com\nGIT_SETUP_GITHUB_REPOSITORY=example/source\n');
  await writeFile(join(target, '.env.git-setup'), 'GIT_SETUP_GITHUB_TOKEN=fixture-tickets-token\nGIT_SETUP_GITHUB_HOST=github.com\nGIT_SETUP_GITHUB_REPOSITORY=example/tickets\n');
  const input = await issueInput(project);
  input.target = { scope: 'specified', project: target, backend: 'github', github: { host: 'github.com', repository: 'example/tickets' } };
  const run = async (command, file, extra = []) => f.run(cli, [command, '--project', project, '--input', file, ...extra], f.root);
  const plan = file => run('issue-resolve', file);
  const apply = async (command, file) => { const p = await plan(file); assert.equal(p.status, 'planned', JSON.stringify(p)); return run(command, file, ['--apply', '--expected-fingerprint', p.fingerprint]); };
  return { f, project, target, api, input, run, plan, apply };
}

test('GitHub CLI uses the explicit target identity, recovers a lost create response and checks rules immediately before POST', async t => {
  const { f, project, target, api, input, run, plan, apply } = await fixture(t);
  const file = await inputFile(f, input), preview = await plan(file);
  assert.equal(preview.status, 'planned', JSON.stringify(preview));
  assert.equal(preview.authentication.binding.source, join(target, '.env.git-setup'));
  api.drop = { method: 'POST', path: '/repos/example/tickets/issues' };
  const interrupted = await run('issue-create', file, ['--apply', '--expected-fingerprint', preview.fingerprint]);
  assert.equal(interrupted.status, 'ambiguous', JSON.stringify(interrupted));
  assert.equal(api.issues.length, 1);
  const recovered = await run('issue-recover', file);
  assert.equal(recovered.url, 'https://github.com/example/tickets/issues/1', JSON.stringify(recovered));
  assert.equal((await run('issue-recover', file)).status, 'verified');
  assert.equal(api.requests.filter(item => item.method === 'POST').length, 1);
  assert.ok(api.requests.every(item => item.authorized));
  api.issues[0].body += '\nA later edit';
  assert.equal((await run('issue-recover', file)).code, 'issue-content-mismatch');
  const second = { ...input, operationId: randomUUID() }, nextFile = await inputFile(f, second), next = await plan(nextFile);
  api.beforeResponse = async request => {
    if (request.path === '/repos/example/tickets' && request.method === 'GET') {
      api.beforeResponse = null;
      await writeFile(join(project, input.rules.path), 'Changed rule after preflight.\n');
    }
  };
  const blocked = await run('issue-create', nextFile, ['--apply', '--expected-fingerprint', next.fingerprint]);
  assert.equal(blocked.code, 'issue_document_changed', JSON.stringify(blocked));
  assert.equal(api.requests.filter(item => item.method === 'POST').length, 1);
  const journal = await readFile(join(target, 'data/git-setup', input.operationId + '.json'), 'utf8');
  assert.equal(journal.includes('fixture-tickets-token'), false);
  assert.equal(journal.includes(input.content.body), false);
});

test('GitHub updates and replies recover on the original issue without repeating writes or changing the selected source', async t => {
  const { f, target, api, input, run, plan, apply } = await fixture(t);
  const created = await apply('issue-create', await inputFile(f, input));
  assert.equal(created.status, 'verified', JSON.stringify(created));
  const update = { ...input, action: 'update', operationId: randomUUID(), existing: { url: created.url }, content: { title: 'Reviewed title', state: 'closed' } };
  const updateFile = await inputFile(f, update);
  api.drop = { method: 'PATCH', path: '/repos/example/tickets/issues/1' };
  assert.equal((await apply('issue-update', updateFile)).status, 'ambiguous');
  assert.equal((await run('issue-recover', updateFile)).url, created.url);
  assert.equal((await apply('issue-update', updateFile)).status, 'verified');
  assert.equal(api.requests.filter(item => item.method === 'PATCH').length, 1);
  assert.equal(api.issues[0].title, 'Reviewed title');
  assert.equal(api.issues[0].state, 'closed');
  const comment = { ...input, action: 'comment', operationId: randomUUID(), existing: { url: created.url }, content: { body: 'Reply on the original issue.' } };
  const commentFile = await inputFile(f, comment);
  api.drop = { method: 'POST', path: '/repos/example/tickets/issues/1/comments' };
  assert.equal((await apply('issue-comment', commentFile)).status, 'ambiguous');
  const recovered = await run('issue-recover', commentFile);
  assert.equal(recovered.url, created.url + '#issuecomment-10', JSON.stringify(recovered));
  assert.equal((await run('issue-recover', commentFile)).status, 'verified');
  assert.equal(api.requests.filter(item => item.method === 'POST' && item.path.endsWith('/comments')).length, 1);
  assert.equal(api.comments[0].issue_url, 'https://api.github.com/repos/example/tickets/issues/1');
  assert.match(api.comments[0].body, /^Reply on the original issue\./);
  const invalid = { ...comment, existing: { url: 'https://github.com/example/wrong/issues/1' } };
  assert.equal((await plan(await inputFile(f, invalid))).code, 'issue_existing_invalid');
  api.comments[0].body += 'later edit';
  assert.equal((await run('issue-recover', commentFile)).code, 'issue-content-mismatch');
  await writeFile(join(target, '.env.git-setup'), 'GIT_SETUP_GITHUB_TOKEN=invalid-selected-token\nGIT_SETUP_GITHUB_HOST=github.com\nGIT_SETUP_GITHUB_REPOSITORY=example/tickets\n');
  assert.equal((await run('issue-recover', updateFile)).code, 'http-error');
  assert.equal(api.requests.filter(item => item.method === 'PATCH').length, 1);
});

test('an empty GitHub query or a same-title item cannot cause a repeated creation', async t => {
  const { f, api, input, run, apply } = await fixture(t);
  const file = await inputFile(f, input);
  api.drop = { method: 'POST', path: '/repos/example/tickets/issues' };
  assert.equal((await apply('issue-create', file)).status, 'ambiguous');
  api.hiddenIssues = true;
  assert.equal((await run('issue-recover', file)).status, 'ambiguous');
  assert.equal((await apply('issue-create', file)).status, 'ambiguous');
  api.hiddenIssues = false;
  api.fullPages = true;
  const beforePages = api.requests.filter(item => item.method === 'GET' && item.path.endsWith('/issues')).length;
  assert.equal((await run('issue-recover', file)).status, 'ambiguous');
  assert.equal(api.requests.filter(item => item.method === 'GET' && item.path.endsWith('/issues')).length - beforePages, 20);
  api.fullPages = false;
  api.issues.push({ ...api.issues[0], number: 99, html_url: 'https://github.com/example/tickets/issues/99', body: 'Same title, different body.' });
  assert.equal((await run('issue-recover', file)).url, 'https://github.com/example/tickets/issues/1');
  assert.equal(api.requests.filter(item => item.method === 'POST').length, 1);
});

test('GitHub replies accept canonical repository casing while rejecting another issue or comment identity', async t => {
  const { f, api, input, run, apply } = await fixture(t);
  const created = await apply('issue-create', await inputFile(f, input));
  assert.equal(created.status, 'verified', JSON.stringify(created));
  input.target.github.repository = 'Example/Tickets';
  const comment = { ...input, action: 'comment', operationId: randomUUID(), existing: { url: created.url }, content: { body: 'Reply with canonical repository casing.' } };
  const file = await inputFile(f, comment);
  const result = await apply('issue-comment', file);
  assert.equal(result.status, 'verified', JSON.stringify(result));
  assert.equal(result.url, 'https://github.com/example/tickets/issues/1#issuecomment-10');
  assert.equal((await run('issue-recover', file)).status, 'verified');
  api.comments[0].issue_url = 'https://api.github.com/repos/example/tickets/issues/2';
  assert.equal((await run('issue-recover', file)).code, 'comment-mismatch');
  api.comments[0].issue_url = 'https://api.github.com/repos/example/tickets/issues/1';
  api.comments[0].html_url = 'https://github.com/example/tickets/issues/1#issuecomment-11';
  assert.equal((await run('issue-recover', file)).code, 'comment-mismatch');
  assert.equal(api.requests.filter(item => item.method === 'POST' && item.path.endsWith('/comments')).length, 1);
});

test('post-write rule or identity failure preserves the known result and operation for read-only recovery', async t => {
  const { f, project, target, api, input, run, apply } = await fixture(t);
  const file = await inputFile(f, input), rulesFile = join(project, input.rules.path), original = await readFile(rulesFile);
  api.beforeResponse = async request => {
    if (request.method === 'GET' && request.path === '/repos/example/tickets/issues/1') {
      api.beforeResponse = null;
      await writeFile(rulesFile, 'Rule changed after the external write.\n');
    }
  };
  const result = await apply('issue-create', file);
  assert.equal(result.status, 'review_required', JSON.stringify(result));
  assert.equal(result.exit, 2);
  assert.equal(result.operationId, input.operationId);
  assert.equal(result.url, 'https://github.com/example/tickets/issues/1');
  assert.equal(result.knownResult.status, 'verified');
  assert.equal(result.finalCheck.code, 'issue_document_changed');
  assert.equal(result.retryAllowed, false);
  await writeFile(rulesFile, original);
  const credentialsFile = join(target, '.env.git-setup'), credentials = await readFile(credentialsFile);
  api.beforeResponse = async request => {
    if (request.method === 'GET' && request.path === '/repos/example/tickets/issues/1') {
      api.beforeResponse = null;
      await writeFile(credentialsFile, 'GIT_SETUP_GITHUB_TOKEN=invalid-selected-token\nGIT_SETUP_GITHUB_HOST=github.com\nGIT_SETUP_GITHUB_REPOSITORY=example/tickets\n');
    }
  };
  const failedRecovery = await run('issue-recover', file);
  assert.equal(failedRecovery.status, 'review_required', JSON.stringify(failedRecovery));
  assert.equal(failedRecovery.knownResult.status, 'verified');
  assert.equal(failedRecovery.operationId, input.operationId);
  assert.equal(failedRecovery.url, result.url);
  assert.equal(failedRecovery.finalCheck.code, 'http-error');
  await writeFile(credentialsFile, credentials);
  assert.equal((await run('issue-recover', file)).status, 'verified');
  assert.equal(api.requests.filter(item => item.method === 'POST').length, 1);
});
