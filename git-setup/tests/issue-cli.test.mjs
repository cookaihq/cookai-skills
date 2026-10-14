import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir, lstat, writeFile, symlink, rm, mkdir, chmod } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { hostname } from 'node:os';
import { repositoryFixture, cli } from './fixtures/repositories.mjs';
import { issueInput, inputFile, evidence, sha256 } from './fixtures/issue-input.mjs';

test('public Issue input creates and recovers a numbered local ticket using the actual rule and template', async () => {
  const f = await repositoryFixture(), project = await f.repo('project');
  const input = await issueInput(project), file = await inputFile(f, input);
  const args = ['--project', project, '--input', file];
  const before = await f.git(project, 'ls-files', '--stage');
  const plan = await f.run(cli, ['issue-resolve', ...args], f.root);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  assert.equal(plan.backend, 'local');
  assert.equal(plan.rules.file, join(project, 'docs/agents/issue-tracker.md'));
  await assert.rejects(lstat(join(project, 'tickets')), { code: 'ENOENT' });
  const created = await f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint], project);
  assert.equal(created.status, 'verified', JSON.stringify(created));
  assert.equal(created.filePath, join(project, 'tickets/01-preserve-the-project-rules.md'));
  assert.equal(await readFile(created.filePath, 'utf8'), '# 01 Preserve the project rules\n\nStatus: open\n\nThe created ticket must retain its status and location.\n\n## Comments\n');
  const recovered = await f.run(cli, ['issue-recover', ...args], f.root);
  assert.equal(recovered.status, 'verified', JSON.stringify(recovered));
  assert.equal(recovered.filePath, created.filePath);
  assert.equal((await readdir(join(project, 'tickets'))).length, 1);
  assert.equal(await f.git(project, 'ls-files', '--stage'), before);
  await assert.rejects(lstat(join(project, '.git-setup')), { code: 'ENOENT' });
  const record = await readFile(join(project, 'data/git-setup', input.operationId + '.json'), 'utf8');
  assert.equal(record.includes(input.content.body), false);
});

test('missing rules distinguish initialization from forbidden reruns and template drift refuses writes', async () => {
  const f = await repositoryFixture(), project = await f.repo('project');
  const input = await issueInput(project), file = await inputFile(f, input), args = ['--project', project, '--input', file];
  const ruleFile = join(project, input.rules.path), original = await readFile(ruleFile);
  await rm(ruleFile);
  const missing = await f.run(cli, ['issue-resolve', ...args]);
  assert.equal(missing.code, 'issue_rules_missing');
  assert.match(missing.message, /\/setup-matt-pocock-skills/);
  await writeFile(join(project, 'AGENTS.md'), 'Do not rerun setup-matt-pocock-skills. Repair existing docs and mappings.\n');
  input.rules.initialization = { mode: 'repair-existing', evidence: await evidence(project, 'AGENTS.md') };
  await writeFile(file, JSON.stringify(input));
  const forbidden = await f.run(cli, ['issue-resolve', ...args]);
  assert.equal(forbidden.code, 'issue_rules_missing');
  assert.match(forbidden.message, /修复/); assert.doesNotMatch(forbidden.message, /\/setup-matt-pocock-skills/);
  await writeFile(ruleFile, original);
  await chmod(ruleFile, 0);
  try { assert.equal((await f.run(cli, ['issue-resolve', ...args])).code, 'issue_document_unreadable'); }
  finally { await chmod(ruleFile, 0o644); }
  const plan = await f.run(cli, ['issue-resolve', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  await writeFile(join(project, input.target.local.template.path), '# changed template\n');
  assert.equal((await f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint])).code, 'issue_document_changed');
  await assert.rejects(lstat(join(project, 'tickets')), { code: 'ENOENT' });
});

test('local updates and replies keep the original item and recover without overwriting later user edits', async () => {
  const f = await repositoryFixture(), project = await f.repo('project');
  const input = await issueInput(project);
  const run = async (operation, command, apply = false) => {
    const file = await inputFile(f, operation), args = ['--project', project, '--input', file];
    if (!apply) return f.run(cli, [command, ...args]);
    const plan = await f.run(cli, ['issue-resolve', ...args]);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    return f.run(cli, [command, ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  };
  const created = await run(input, 'issue-create', true);
  assert.equal(created.status, 'verified', JSON.stringify(created));
  const original = await readFile(created.filePath, 'utf8');
  await rm(join(project, input.target.local.template.path));
  delete input.target.local.template;
  const update = { ...input, action: 'update', operationId: randomUUID(), existing: { path: 'tickets/01-preserve-the-project-rules.md', sha256: sha256(original) }, content: { document: original.replace('Status: open', 'Status: done') } };
  const updated = await run(update, 'issue-update', true);
  assert.equal(updated.status, 'verified', JSON.stringify(updated));
  assert.equal(updated.filePath, created.filePath);
  assert.equal((await run(update, 'issue-recover')).status, 'verified');
  const beforeComment = await readFile(created.filePath, 'utf8');
  const comment = { ...input, action: 'comment', operationId: randomUUID(), existing: { path: update.existing.path, sha256: sha256(beforeComment) }, content: { body: 'Verified through the project entry.' } };
  assert.equal((await run(comment, 'issue-comment', true)).status, 'verified');
  const after = await readFile(created.filePath, 'utf8');
  assert.equal(after, beforeComment + '\nVerified through the project entry.\n');
  assert.equal((await run(comment, 'issue-recover')).status, 'verified');
  assert.equal((await readdir(join(project, 'tickets'))).filter(name => name.endsWith('.md')).length, 1);
  await writeFile(created.filePath, after + 'A later user edit.\n');
  assert.equal((await run(comment, 'issue-recover')).code, 'local-content-mismatch');
  assert.equal((await run(update, 'issue-update')).code, 'local-content-mismatch');
  assert.equal(await readFile(created.filePath, 'utf8'), after + 'A later user edit.\n');
});

test('local creation accepts the complete document interpreted from prose rules without a renderer template', async () => {
  const f = await repositoryFixture(), project = await f.repo('project'), input = await issueInput(project);
  await rm(join(project, input.target.local.template.path));
  delete input.target.local.template;
  const document = '# Preserve the project rules\n\nStatus: ready-for-agent\n\nKeep `{body}` as literal example text.\n\n## Comments\n';
  input.content = { title: 'Preserve the project rules', document };
  const file = await inputFile(f, input), args = ['--project', project, '--input', file];
  const plan = await f.run(cli, ['issue-resolve', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  const created = await f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(created.status, 'verified', JSON.stringify(created));
  assert.equal(await readFile(created.filePath, 'utf8'), document);
  assert.equal((await f.run(cli, ['issue-recover', ...args])).status, 'verified');
  assert.equal((await readdir(join(project, 'tickets'))).length, 1);
});

test('Worktree rule mapping uses the declared shared entity and changed evidence invalidates plans and recovery', async () => {
  const f = await repositoryFixture(), main = await f.repo('main'), other = await f.repo('other');
  const input = await issueInput(main), checkout = join(f.root, 'checkout');
  await f.git(main, 'worktree', 'add', '-b', 'issue-checkout', checkout);
  const declaration = 'docs-anchor maps to the same repository main checkout docs directory.\n';
  await writeFile(join(checkout, 'AGENTS.md'), declaration);
  await symlink(join(main, 'docs'), join(checkout, 'docs-anchor'));
  input.initiatingProject = checkout; input.relationship.workspace = checkout;
  input.rules.project = checkout; input.rules.path = 'docs-anchor/agents/issue-tracker.md';
  input.rules.mapping = { from: 'docs-anchor', to: 'docs', declaration: await evidence(checkout, 'AGENTS.md') };
  input.target.local.template.path = 'docs-anchor/agents/issue-template.md';
  const file = await inputFile(f, input), args = ['--project', checkout, '--input', file];
  const plan = await f.run(cli, ['issue-resolve', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  assert.equal(plan.rules.file, join(main, 'docs/agents/issue-tracker.md'));
  await issueInput(other);
  await rm(join(checkout, 'docs-anchor')); await symlink(join(other, 'docs'), join(checkout, 'docs-anchor'));
  assert.equal((await f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint])).code, 'issue_mapping_changed');
  await assert.rejects(lstat(join(main, 'tickets')), { code: 'ENOENT' });
  await rm(join(checkout, 'docs-anchor')); await symlink(join(main, 'docs'), join(checkout, 'docs-anchor'));
  const created = await f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(created.status, 'verified', JSON.stringify(created));
  assert.equal(created.filePath, join(main, 'tickets/01-preserve-the-project-rules.md'));
  const ruleFile = join(main, 'docs/agents/issue-tracker.md');
  const changed = (await readFile(ruleFile, 'utf8')) + 'Add review evidence.\n';
  await writeFile(ruleFile, changed);
  assert.equal((await f.run(cli, ['issue-recover', ...args])).code, 'issue_document_changed');
  input.rules.sha256 = sha256(changed); await writeFile(file, JSON.stringify(input));
  assert.equal((await f.run(cli, ['issue-recover', ...args])).code, 'operation-mismatch');
  assert.equal((await readdir(join(main, 'tickets'))).length, 1);
  await assert.rejects(lstat(join(checkout, 'tickets')), { code: 'ENOENT' });
});

test('public preview rejects malformed content, filename, numbering and template before any journal or ticket is written', async () => {
  const f = await repositoryFixture(), project = await f.repo('project'), input = await issueInput(project);
  const variants = [
    { ...input, content: { title: '', body: 'body' } },
    { ...input, content: { title: 'Title', body: 'body', filename: 'bypass.md' } },
    { ...input, target: { ...input.target, local: { ...input.target.local, filename: '../{slug}.md' } } },
    { ...input, target: { ...input.target, local: { ...input.target.local, numbering: { start: 0, width: 2 } } } },
  ];
  for (const variant of variants) {
    const result = await f.run(cli, ['issue-resolve', '--project', project, '--input', await inputFile(f, variant)]);
    assert.equal(result.exit, 1, JSON.stringify(result));
  }
  await writeFile(join(project, input.target.local.template.path), '# Template without requested content\n');
  input.target.local.template = await evidence(project, input.target.local.template.path);
  assert.equal((await f.run(cli, ['issue-resolve', '--project', project, '--input', await inputFile(f, input)])).code, 'invalid-template');
  await assert.rejects(lstat(join(project, 'data')), { code: 'ENOENT' });
  await assert.rejects(lstat(join(project, 'tickets')), { code: 'ENOENT' });
});

test('dead-owner lock recovery serializes concurrent public creates and partial local writes stay incomplete', async () => {
  const f = await repositoryFixture(), project = await f.repo('project'), input = await issueInput(project);
  const operations = await Promise.all([input, { ...input, operationId: randomUUID() }].map(async value => {
    const file = await inputFile(f, value), args = ['--project', project, '--input', file];
    const plan = await f.run(cli, ['issue-resolve', ...args]);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    return { value, args, plan };
  }));
  await mkdir(join(project, 'data/git-setup'), { recursive: true });
  const lock = join(project, 'data/git-setup/local-numbering.lock');
  await writeFile(lock, JSON.stringify({ pid: 2147483647, lockId: randomUUID(), host: 'another-host.invalid' }));
  const blocked = await f.run(cli, ['issue-create', ...operations[0].args, '--apply', '--expected-fingerprint', operations[0].plan.fingerprint]);
  assert.equal(blocked.code, 'operation-locked', JSON.stringify(blocked));
  await writeFile(lock, JSON.stringify({ pid: 2147483647, lockId: randomUUID(), host: hostname() }));
  const results = await Promise.all(operations.map(({ args, plan }) => f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint])));
  assert.ok(results.some(result => result.status === 'verified'), JSON.stringify(results));
  for (const result of results) if (result.status !== 'verified') assert.equal(result.code, 'operation-locked', JSON.stringify(result));
  const files = await readdir(join(project, 'tickets'));
  assert.equal(files.length, results.filter(result => result.status === 'verified').length);
  assert.equal(new Set(files.map(name => name.split('-')[0])).size, files.length);
  const index = results.findIndex(result => result.status === 'verified'), result = results[index];
  await writeFile(result.filePath, '# Partial or later user content\n');
  assert.equal((await f.run(cli, ['issue-recover', ...operations[index].args])).code, 'local-content-mismatch');
  assert.equal(await readFile(result.filePath, 'utf8'), '# Partial or later user content\n');
});

test('rules explicitly choose outer, specified or submodule-local storage and references never redirect a new ticket', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer'), child = await f.repo('outer/child'), specified = await f.repo('specified');
  await f.install(outer); await writeFile(join(outer, 'vcs/repos.conf'), 'child\n');
  const input = await issueInput(outer);
  input.initiatingProject = child;
  input.relationship = { workspace: outer, kind: 'independent', path: 'child' };
  input.target.scope = 'outer';
  const run = async value => {
    const args = ['--project', value.initiatingProject, '--input', await inputFile(f, value)];
    const plan = await f.run(cli, ['issue-resolve', ...args]);
    assert.equal(plan.status, 'planned', JSON.stringify(plan));
    return f.run(cli, ['issue-create', ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  };
  const centralized = await run(input);
  assert.equal(centralized.status, 'verified', JSON.stringify(centralized));
  assert.ok(centralized.filePath.startsWith(outer + '/tickets/'));
  const original = await readFile(centralized.filePath, 'utf8');
  const referenced = await run({ ...input, operationId: randomUUID(), reference: centralized.filePath, target: { ...input.target, scope: 'specified', project: specified } });
  assert.equal(referenced.status, 'verified', JSON.stringify(referenced));
  assert.ok(referenced.filePath.startsWith(specified + '/tickets/'));
  assert.equal(await readFile(centralized.filePath, 'utf8'), original);
  await f.git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', specified, 'module');
  const module = join(outer, 'module'), independent = await issueInput(module);
  independent.relationship = { workspace: outer, kind: 'submodule', path: 'module' };
  const created = await run(independent);
  assert.equal(created.status, 'verified', JSON.stringify(created));
  assert.ok(created.filePath.startsWith(module + '/tickets/'));
  await assert.rejects(lstat(join(child, 'tickets')), { code: 'ENOENT' });
});
