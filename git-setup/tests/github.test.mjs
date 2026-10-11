import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { authFromEnvironment, createGitHubClient, validateGitHubTarget } from '../scripts/git-setup/github.mjs';

const auth = { method: 'token', token: 'test-token-not-real', host: 'github.com', repository: 'example/project' };
const response = (status, data = {}, headers = {}) => ({ status, data, headers });

test('token is sent only to the bound github.com repository API', async () => {
  const sent = [];
  const client = createGitHubClient({ auth, transport: async request => { sent.push(request); return response(200, { id: 1 }); } });
  await client.getRepository('example/project');
  assert.equal(sent[0].url, 'https://api.github.com/repos/example/project');
  assert.equal(sent[0].headers.Authorization, 'Bearer test-token-not-real');
  assert.equal(sent[0].timeoutMs, 30_000);
  await assert.rejects(client.getRepository('other/project'), { code: 'auth-target-mismatch' });
  assert.throws(() => createGitHubClient({ auth, host: 'github.example.com' }), { code: 'unsupported-host' });
  for (const repository of ['example/project?token=x', '../other', 'example/project.git', 'https://github.com/example/project']) assert.throws(() => validateGitHubTarget({ host: 'github.com', repository }));
  assert.equal(sent.length, 1);
});

test('auth fields are checked as a complete group', () => {
  assert.throws(() => authFromEnvironment({ GIT_SETUP_GITHUB_TOKEN: 'secret' }, { host: 'github.com', repository: 'example/project' }), { code: 'incomplete-auth' });
  assert.deepEqual(authFromEnvironment({ GIT_SETUP_GITHUB_AUTH: 'gh' }, { host: 'github.com', repository: 'example/project' }), { method: 'gh', host: undefined, repository: undefined });
  assert.throws(() => authFromEnvironment({ GIT_SETUP_GITHUB_AUTH: 'gh', GIT_SETUP_GITHUB_TOKEN: 'secret' }, { host: 'github.com', repository: 'example/project' }), { code: 'conflicting-auth' });
});

test('GET retries transient failures three times and honors capped Retry-After', async () => {
  const waits = [], logs = [];
  let count = 0;
  const client = createGitHubClient({ auth, sleep: async ms => waits.push(ms), onRetry: event => logs.push(event), transport: async () => (++count < 3 ? response(429, {}, { 'Retry-After': '999' }) : response(200, { ok: true })) });
  assert.deepEqual(await client.getRepository('example/project'), { ok: true });
  assert.deepEqual(waits, [60_000, 60_000]);
  assert.equal(logs.length, 2);
  assert.equal(JSON.stringify(logs).includes(auth.token), false);
});

test('invalid Retry-After falls back and read 4xx is not retried', async () => {
  let count = 0;
  const waits = [];
  const client = createGitHubClient({ auth, sleep: async ms => waits.push(ms), onRetry: () => {}, transport: async () => ++count === 1 ? response(429, {}, { 'retry-after': 'NaN' }) : response(404) });
  await assert.rejects(client.getRepository('example/project'), { status: 404 });
  assert.deepEqual(waits, [1000]);
  assert.equal(count, 2);
});

test('403 with explicit rate-limit headers is distinguished from permission denial', async () => {
  let count = 0;
  const waits = [];
  const client = createGitHubClient({ auth, now: () => 1000, sleep: async ms => waits.push(ms), onRetry: () => {}, transport: async () => ++count === 1 ? response(403, {}, { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '3' }) : response(200) });
  await client.getRepository('example/project');
  assert.deepEqual(waits, [2000]);
  assert.equal(count, 2);
});

test('write failures are ambiguous and never retried or expose transport secrets', async () => {
  for (const failure of [() => { throw new Error(`request token ${auth.token}`); }, () => response(503), () => response(429)]) {
    let count = 0;
    const client = createGitHubClient({ auth, transport: async () => { count++; return failure(); } });
    await assert.rejects(client.createIssue('example/project', { title: 'T', body: 'B' }, { authorized: true }), error => error.code === 'ambiguous' && !error.message.includes(auth.token));
    assert.equal(count, 1);
  }
});

test('write authorization is enforced before any transport call', async () => {
  let count = 0;
  const client = createGitHubClient({ auth, transport: async () => { count++; return response(201); } });
  await assert.rejects(client.createIssue('example/project', { title: 'T' }), { code: 'authorization-required' });
  await assert.rejects(client.createRepository({ owner: 'example', name: 'project', private: true }), { code: 'authorization-required' });
  assert.equal(count, 0);
});

test('redirects are refused so credentials cannot follow another origin', async () => {
  let count = 0;
  const client = createGitHubClient({ auth, transport: async () => { count++; return response(301, {}, { location: 'https://elsewhere.invalid/' }); } });
  await assert.rejects(client.getRepository('example/project'), { code: 'redirect-refused' });
  assert.equal(count, 1);
});

test('read deadline constrains both timeout and retry backoff', async () => {
  let sent;
  const client = createGitHubClient({ auth, now: () => 1000, transport: async request => { sent = request; return response(503); }, onRetry: () => {} });
  await assert.rejects(client.listIssues('example/project', { deadline: 1500 }), { code: 'budget-exhausted' });
  assert.equal(sent.timeoutMs, 500);
});

test('repository creation does not infer absence from access failure or modify existing repo', async () => {
  const seen = [];
  const client = createGitHubClient({ auth, transport: async request => { seen.push(request); return response(200, { id: 7, private: true }); } });
  assert.equal((await client.createRepository({ owner: 'example', name: 'project', private: false, authorized: true })).status, 'already-exists');
  assert.equal(seen.length, 1);
  assert.equal(seen[0].method, 'GET');
  const rejected = createGitHubClient({ auth, transport: async () => response(403) });
  await assert.rejects(rejected.createRepository({ owner: 'example', name: 'project', private: true, authorized: true }), { status: 403 });
});

test('explicit organization creation uses org endpoint with no automatic README', async () => {
  const seen = [];
  const client = createGitHubClient({ auth, transport: async request => {
    seen.push(request);
    if (request.path === '/repos/example/project') return response(404);
    if (request.path === '/users/example') return response(200, { type: 'Organization' });
    return response(201, { id: 9, full_name: 'example/project' });
  } });
  const result = await client.createRepository({ owner: 'example', name: 'project', private: true, authorized: true });
  assert.equal(result.status, 'created');
  assert.equal(seen[2].path, '/orgs/example/repos');
  assert.deepEqual(seen[2].body, { name: 'project', private: true, auto_init: false });
});

test('personal repository owner must match authenticated user', async () => {
  const client = createGitHubClient({ auth, transport: async request => request.path.startsWith('/repos/') ? response(404) : response(200, request.path === '/user' ? { login: 'another-user' } : { type: 'User' }) });
  await assert.rejects(client.createRepository({ owner: 'example', name: 'project', private: true, authorized: true }), { code: 'owner-mismatch' });
});

test('gh transport probes actual executable and sends body through stdin', async () => {
  const calls = [];
  const spawnImpl = (binary, args, options) => {
    const process = new EventEmitter();
    process.stdin = new PassThrough(); process.stdout = new PassThrough(); process.stderr = new PassThrough();
    let input = '';
    process.stdin.on('data', chunk => { input += chunk; });
    process.stdin.on('finish', () => {
      calls.push({ binary, args, options, input });
      process.stdout.end(args[0] === '--version' ? 'gh version 2.80.0' : 'HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n{"number":17}');
      process.emit('close', 0);
    });
    return process;
  };
  const client = createGitHubClient({ auth: { method: 'gh' }, spawnImpl });
  assert.deepEqual(await client.createIssue('example/project', { title: 'safe', body: 'body' }, { authorized: true }), { number: 17 });
  assert.deepEqual(calls[0].args, ['--version']);
  assert.equal(calls[1].args.includes('github.com'), true);
  assert.equal(calls[1].args.includes('--input'), true);
  assert.deepEqual(JSON.parse(calls[1].input), { title: 'safe', body: 'body' });
  assert.equal(calls[1].args.includes('body'), false);
  assert.equal(calls[1].options.env.GH_TOKEN, '');
  assert.equal(calls[1].options.env.GITHUB_TOKEN, '');
});
