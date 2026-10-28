import { issueInput, inputFile } from './fixtures/issue-input.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, lstat, mkdir, cp, rename } from 'node:fs/promises';
import { join } from 'node:path';
import { repositoryFixture, cli, skill } from './fixtures/repositories.mjs';
import { gitService } from './fixtures/git-service.mjs';

test('public check and auth use the registered independent repository and its primary checkout', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer'), child = await f.repo('outer/children/api');
  await f.configure(outer);
  await assert.rejects(lstat(join(outer, 'vcs/repos.conf')), { code: 'ENOENT' });
  await writeFile(join(outer, 'vcs/repos.conf'), 'children/api\n');
  const checkout = join(f.root, 'api-worktree');
  await f.git(child, 'worktree', 'add', '-b', 'fixture-worktree', checkout);
  const args = ['--project', checkout, '--workspace', outer, '--policy-directory', 'vcs'];
  const checked = await f.run(cli, ['check', ...args]);
  assert.equal(checked.relationship?.registration.kind, 'independent', JSON.stringify(checked));
  assert.equal(checked.relationship.project.root, checkout);
  assert.equal(checked.relationship.project.main, child);
  assert.equal(checked.relationship.management.main, outer);
  const prepared = await f.run(cli, ['credential-prepare', ...args, '--url', 'https://cnb.cool/team/api', '--auth-method', 'cnb-url', '--apply']);
  assert.equal(prepared.status, 'awaiting_credentials', JSON.stringify(prepared));
  assert.equal(prepared.credentialFile, join(outer, '.credentials/repositories/team--api.cnb-url'));
  assert.equal(await readFile(prepared.credentialFile, 'utf8'), '');
  const choice = await f.run(cli, ['credential-info', ...args, '--url', 'https://cnb.cool/team/api']);
  assert.deepEqual(choice.required, ['credentials-filled']);
  const resumedCheck = await f.run(cli, ['check', ...args]);
  assert.equal(resumedCheck.relationship?.management.main, outer, JSON.stringify(resumedCheck));
  await writeFile(join(outer, 'vcs/repos.conf'), '');
  const removed = await f.run(cli, ['credential-info', ...args, '--url', 'https://cnb.cool/team/api']);
  assert.equal(removed.status, 'invalid');
  assert.equal(removed.code, 'WORKSPACE_UNREGISTERED');
});

test('regenerating a registered manager preserves its independent repository list', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer');
  await f.repo('outer/child');
  await f.configure(outer);
  const registry = '# Owned by this project\nchild\n';
  await writeFile(join(outer, 'vcs/repos.conf'), registry);
  const checked = await f.run(cli, ['check', '--project', outer]);
  assert.equal(checked.status, 'checked', JSON.stringify(checked));
  await f.configure(outer);
  assert.equal(await readFile(join(outer, 'vcs/repos.conf'), 'utf8'), registry);
  assert.equal((await f.run(cli, ['check', '--project', join(outer, 'child'), '--workspace', outer,'--policy-directory','vcs'])).relationship.registration.kind, 'independent');
  await assert.rejects(f.git(outer, 'config', '--name-only', '--get-regexp', '^gitsetup.harness-'));
  const operation = await issueInput(outer);
  operation.initiatingProject = join(outer, 'child');
  operation.relationship = { workspace: outer, kind: 'independent', path: 'child', policyDirectory: 'vcs' };
  operation.target.scope = 'outer';
  const resolved = await f.run(cli, ['issue-resolve', '--project', operation.initiatingProject, '--input', await inputFile(f, operation)]);
  assert.equal(resolved.local?.root, outer, JSON.stringify(resolved));
});

test('independent repositories select their own current credential files',async t=>{
 const f=await repositoryFixture(),outer=await f.repo('outer'),first=await f.repo('outer/first'),second=await f.repo('outer/second');
 await f.configure(outer);await writeFile(join(outer,'vcs/repos.conf'),'first\nsecond\n');
 await gitService(t,f,{'team/first':'first-fixture','team/second':'second-fixture'});
 for(const [project,name] of [[first,'first'],[second,'second']]){
  const url='https://cnb.cool/team/'+name+'.git';await f.git(project,'remote','add','origin',url);
  const p=await f.run(cli,['credential-prepare','--project',project,'--workspace',outer,'--policy-directory','vcs','--url',url,'--apply']);
  await writeFile(p.credentialFile,'https://cnb:'+name+'-fixture@cnb.cool/team/'+name);
 }
 await f.configure(outer,{repositories:{first:{sync:null},second:{sync:null}}});
 for(const name of ['first','second'])assert.equal((await f.command(outer,['read','--repository',name,'--remote','origin'])).status,'read');
 await writeFile(join(outer,'vcs/repos.conf'),'second\n');
 assert.equal((await f.command(outer,['read','--repository','first','--remote','origin'])).code,'WORKSPACE_UNREGISTERED');
 assert.equal((await f.command(outer,['read','--repository','second','--remote','origin'])).status,'read');
});

test('public relationship checks reject legacy registration, duplicate paths and same-URL clones', async () => {
  const f = await repositoryFixture(), outer = await f.repo('outer'), child = await f.repo('outer/child'), clone = await f.repo('clone');
  for (const repo of [child, clone]) await f.git(repo, 'remote', 'add', 'origin', 'https://cnb.cool/team/shared');
  await f.git(outer, 'config', '--add', 'gitsetup.repository', 'child');
  await mkdir(join(outer, '.git-setup'));
  await writeFile(join(outer, '.git-setup/project.json'), JSON.stringify({ schemaVersion: 1, workspace: { repositories: [{ path: 'child', issues: 'inherit' }] } }));
  const args = ['--workspace', outer,'--policy-directory','vcs'];
  assert.equal((await f.run(cli, ['check', '--project', child, ...args])).code, 'WORKSPACE_UNREGISTERED');
  await f.configure(outer);
  await writeFile(join(outer, 'vcs/repos.conf'), 'child\n');
  assert.equal((await f.run(cli, ['check', '--project', clone, ...args])).code, 'WORKSPACE_UNREGISTERED');
  await writeFile(join(outer, 'vcs/repos.conf'), 'child\nchild\n');
  assert.equal((await f.run(cli, ['credential-prepare', '--project', child, ...args, '--url', 'https://cnb.cool/team/shared', '--auth-method', 'cnb-url', '--apply'])).code, 'WORKSPACE_DUPLICATE');
  await assert.rejects(lstat(join(outer, '.credentials')), { code: 'ENOENT' });
  await writeFile(join(outer, 'vcs/repos.conf'), 'missing\n');
  assert.equal((await f.run(cli, ['check', '--project', child, ...args])).code, 'WORKSPACE_UNREGISTERED');
});

test('a normal repository beneath a modules directory remains an independent repository', async () => {
  const f = await repositoryFixture(), project = await f.repo('modules/api');
  const result = await f.run(cli, ['check', '--project', project]);
  assert.equal(result.relationship?.registration.kind, 'self', JSON.stringify(result));
  const separate = join(f.root, 'storage/modules/api');
  await mkdir(join(f.root, 'storage/modules'), { recursive: true });
  await f.git(project, 'init', '--separate-git-dir', separate);
  const moved = await f.run(cli, ['check', '--project', project]);
  assert.equal(moved.relationship?.registration.kind, 'self', JSON.stringify(moved));
});

test('public submodule checks require registration, gitlink and actual Git metadata together', async () => {
  const f = await repositoryFixture(), source = await f.repo('source'), outer = await f.repo('outer');
  await f.git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'module');
  const child = join(outer, 'module');
  const checked = await f.run(cli, ['check', '--project', child]);
  assert.equal(checked.relationship?.registration.kind, 'submodule', JSON.stringify(checked));
  assert.equal(checked.relationship.management.main, outer);
  assert.equal(checked.relationship.registration.gitlink, await f.git(child, 'rev-parse', 'HEAD'));
  const linked = join(f.root, 'linked');
  await f.git(child, 'worktree', 'add', '-b', 'linked-module', linked);
  const worktree = await f.run(cli, ['credential-prepare', '--project', linked, '--url', 'https://cnb.cool/team/module', '--auth-method', 'cnb-url']);
  assert.equal(worktree.credentialFile, join(outer,'.credentials/repositories/team--module.cnb-url'), JSON.stringify(worktree));
  await f.git(outer, 'rm', '--cached', 'module');
  assert.equal((await f.run(cli, ['check', '--project', child])).code, 'WORKSPACE_SUBMODULE_INVALID');
  await f.git(outer, 'update-index', '--add', '--cacheinfo', `160000,${await f.git(child, 'rev-parse', 'HEAD')},module`);
  await f.git(outer, 'submodule', 'deinit', '-f', 'module');
  assert.equal((await f.run(cli, ['check', '--project', outer, '--workspace', outer])).status, 'checked');
  assert.equal((await f.run(cli, ['check', '--project', source, '--workspace', outer])).code, 'WORKSPACE_UNREGISTERED');
});

test('multiple verified managers require a choice and setup rejects a removed registration before writes', async () => {
  const f = await repositoryFixture(), source = await f.repo('source'), outer = await f.repo('outer');
  await f.git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'middle');
  const middle = join(outer, 'middle');
  await f.git(middle, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'leaf');
  const leaf = join(middle, 'leaf');
  assert.equal((await f.run(cli, ['check', '--project', leaf])).code, 'WORKSPACE_AMBIGUOUS');
  const chosen = await f.run(cli, ['check', '--project', leaf, '--workspace', outer]);
  assert.equal(chosen.relationship?.management.main, outer, JSON.stringify(chosen));
  assert.equal(chosen.relationship.registration.kind, 'submodule-chain');

  const manager = await f.repo('manager'), child = await f.repo('manager/child');
  await f.configure(manager); await writeFile(join(manager, 'vcs/repos.conf'), 'child\n');
  await f.configure(manager);
  const target = join(f.root, 'target.git'); await f.git(f.root, 'init', '--bare', '-q', target);
  const args = ['--project', child, '--workspace', manager, '--policy-directory','vcs','--url', target];
  const plan = await f.run(cli, ['setup', ...args]);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  const before = await readFile(join(child, '.git/config'));
  await writeFile(join(manager, 'vcs/repos.conf'), '');
  const rejected = await f.run(cli, ['setup', ...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal(rejected.code, 'WORKSPACE_UNREGISTERED');
  assert.deepEqual(await readFile(join(child, '.git/config')), before);
});
