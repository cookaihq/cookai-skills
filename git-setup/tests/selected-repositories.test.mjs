import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, mkdir, cp, rename, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { repositoryFixture, cli, skill } from './fixtures/repositories.mjs';
import { gitService } from './fixtures/git-service.mjs';

async function facts(f, project) {
  return { config: await f.git(project, 'config', '--local', '--list'), refs: await f.git(project, 'for-each-ref'), index: await f.git(project, 'ls-files', '--stage'), status: await f.git(project, 'status', '--porcelain'), readme: await readFile(join(project, 'README.md'), 'utf8') };
}
async function target(f, name) {
  const path = join(f.root, name + '.git');
  await f.git(f.root, 'init', '--bare', '-q', '-b', 'main', path);
  return path;
}

test('one generated manager entry selects an independent Worktree for read publish and sync', async () => {
  const f = await repositoryFixture(), manager = await f.repo('manager'), child = await f.repo('manager/api'), sibling = await f.repo('manager/web');
  const checkout = join(f.root, 'api-checkout');
  await f.git(child, 'worktree', 'add', '-b', 'feature-api', checkout);
  await writeFile(join(checkout, 'feature.txt'), 'selected Worktree commit\n');
  await f.git(checkout, 'add', '.'); await f.git(checkout, 'commit', '-qm', 'selected checkout');
  const selected = ['--repository', 'api', '--checkout', checkout];
  const upstream = await target(f, 'api-upstream'), archive = await target(f, 'api-archive');
  await f.git(child, 'remote', 'add', 'api-origin', upstream);
  await f.git(child, 'remote', 'add', 'api-backup', archive);
  await f.install(manager);
  await writeFile(join(manager, 'vcs/repos.conf'), 'api\nweb\n');
  await f.install(manager, { repositories: { api: { sync: { source: 'api-origin', targets: ['api-backup'], refs: ['refs/heads/feature-api'] } } } });
  const before = { manager: await facts(f, manager), sibling: await facts(f, sibling), child: await facts(f, child) };
  const entry = join(manager, 'vcs/run.mjs');
  assert.equal((await f.run(entry, [], f.root)).project, manager);
  const checked = await f.run(entry, ['check', ...selected], f.root);
  assert.equal(checked.project, checkout, JSON.stringify(checked));
  assert.equal(checked.installation.root, manager);
  assert.equal(checked.relationship.registration.kind, 'independent');
  assert.equal((await f.run(entry, ['read', ...selected, '--remote', 'api-origin'], manager)).status, 'read');
  const args = ['publish', ...selected, '--remote', 'api-origin', '--ref', 'refs/heads/feature-api'];
  const plan = await f.run(entry, args, f.root);
  assert.equal(plan.status, 'planned', JSON.stringify(plan));
  const published = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint], manager);
  assert.equal(published.complete, true, JSON.stringify(published));
  assert.equal(await f.git(upstream, 'rev-parse', 'feature-api'), await f.git(child, 'rev-parse', 'feature-api'));
  assert.equal(await f.git(archive, 'for-each-ref'), '');
  const sync = await f.run(cli, ['sync', '--project', manager, ...selected]);
  assert.equal(sync.status, 'planned', JSON.stringify(sync));
  const synced = await f.run(entry, ['sync', ...selected, '--apply', '--expected-fingerprint', sync.fingerprint], f.root);
  assert.equal(synced.complete, true, JSON.stringify(synced));
  assert.equal(await f.git(archive, 'rev-parse', 'feature-api'), await f.git(upstream, 'rev-parse', 'feature-api'));
  assert.deepEqual(await facts(f, manager), before.manager);
  assert.deepEqual(await facts(f, sibling), before.sibling);
  assert.deepEqual(await facts(f, child), before.child);
});

test('generated entry keeps submodule Worktree and independent repository credentials and policies separate without the Skill', async t => {
  const f = await repositoryFixture(), manager = await f.repo('manager'), source = await f.repo('module-source');
  await f.git(manager, '-c', 'protocol.file.allow=always', 'submodule', 'add', source, 'module');
  const module = join(manager, 'module'), web = await f.repo('manager/web'), checkout = join(f.root, 'module-checkout');
  await f.git(module, 'worktree', 'add', '-b', 'feature', checkout);
  await f.git(web, 'branch', '-m', 'release');
  const service = await gitService(t, f, { 'team/module': 'module-token', 'team/module-copy': 'module-copy-token', 'team/web': 'web-token', 'team/web-copy': 'web-copy-token' });
  const copy = join(f.root, 'skill-copy'); await cp(skill, copy, { recursive: true });
  const copiedCLI = join(copy, 'scripts/git-setup/cli.mjs');
  await f.install(manager, {}, copiedCLI);
  await writeFile(join(manager, 'vcs/repos.conf'), 'web\n');
  const selections = [
    { key: 'module', project: checkout, branch: 'feature', source: 'module-origin', backup: 'module-archive', args: ['--repository', 'module', '--checkout', checkout] },
    { key: 'web', project: web, branch: 'release', source: 'web-primary', backup: 'web-copy', args: ['--repository', 'web'] },
  ];
  for (const item of selections) for (const [name, suffix] of [[item.source, ''], [item.backup, '-copy']]) await f.git(item.project, 'remote', 'add', name, `https://cnb.cool/team/${item.key}${suffix}.git`);
  await f.install(manager, { repositories: Object.fromEntries(selections.map(item => [item.key, { sync: { source: item.source, targets: [item.backup], refs: [`refs/heads/${item.branch}`] } }])) }, copiedCLI);
  await rename(copy, `${copy}-removed`);
  const entry = join(manager, 'vcs/run.mjs');
  for (const item of selections) {
    for (const suffix of ['', '-copy']) {
      const url = `https://cnb.cool/team/${item.key}${suffix}.git`;
      const prepared = await f.run(entry, ['auth-prepare', ...item.args, '--url', url, '--auth-method', 'cnb-url', '--apply'], f.root);
      assert.equal(prepared.status, 'awaiting_credentials', JSON.stringify(prepared));
      assert.equal(prepared.credentialFile, join(manager, `.credentials/repositories/team--${item.key}${suffix}.cnb-url`));
      await writeFile(prepared.credentialFile, `https://cnb:${item.key}${suffix}-token@cnb.cool/team/${item.key}${suffix}\n`);
      const verified = await f.run(entry, ['auth-verify', ...item.args, '--url', url, '--credentials-filled']);
      assert.equal(verified.status, 'selected', JSON.stringify(verified));
      assert.equal((await f.run(entry, ['auth-verify', ...item.args, '--url', url, '--credentials-filled', '--expected-fingerprint', verified.fingerprint, '--apply'])).status, 'verified');
    }
  }
  const before = { manager: await facts(f, manager), web: await facts(f, web), module: await facts(f, checkout) };
  for (const item of selections) {
    const checked = await f.run(entry, ['check', ...item.args], f.root);
    assert.equal(checked.project, item.project, JSON.stringify(checked));
    assert.equal(checked.relationship.project.main, item.key === 'module' ? module : web);
    assert.equal((await f.run(entry, ['read', ...item.args, '--remote', item.source], manager)).status, 'read');
    const args = ['publish', ...item.args, '--remote', item.source, '--ref', `refs/heads/${item.branch}`];
    const plan = await f.run(entry, args, f.root);
    if (item.key === 'web') service.loseNextReceive('team/web');
    const writesBefore = service.requests.filter(request => request.repository === `team/${item.key}` && request.method === 'POST' && request.path.endsWith('/git-receive-pack')).length;
    const pushed = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint], f.root);
    assert.equal(pushed.complete, true, JSON.stringify(pushed));
    if (item.key === 'web') assert.equal(pushed.results[0].writeReportedSuccess, false);
    assert.equal(service.requests.filter(request => request.repository === `team/${item.key}` && request.method === 'POST' && request.path.endsWith('/git-receive-pack')).length - writesBefore, 1);
    assert.equal(await f.git(service.repositories[`team/${item.key}-copy`], 'for-each-ref'), '');
    const sync = await f.run(entry, ['sync', ...item.args], manager);
    const result = await f.run(entry, ['sync', ...item.args, '--apply', '--expected-fingerprint', sync.fingerprint], f.root);
    assert.equal(result.complete, true, JSON.stringify(result));
    assert.equal(await f.git(service.repositories[`team/${item.key}-copy`], 'rev-parse', item.branch), await f.git(item.project, 'rev-parse', item.branch));
    if (item.key === 'module') assert.deepEqual(await facts(f, web), before.web);
  }
  assert.deepEqual(await facts(f, manager), before.manager);
  assert.deepEqual(await facts(f, checkout), before.module);
  assert.ok(service.requests.some(request => request.repository === 'team/module-copy' && request.method === 'POST'));
  const args = ['publish', ...selections[0].args, '--remote', selections[0].source, '--ref', 'refs/heads/feature'];
  const plan = await f.run(entry, args);
  const credential = join(manager, '.credentials/repositories/team--module.cnb-url');
  await rename(credential, credential + '.missing');
  const beforeRequests = service.requests.length;
  const rejected = await f.run(entry, [...args, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.notEqual(rejected.exit, 0, JSON.stringify(rejected));
  assert.equal(service.requests.length, beforeRequests);
});

test('public selected operations reject changed registry checkout policy remote and refs before target writes', async () => {
  const f = await repositoryFixture(), manager = await f.repo('manager'), child = await f.repo('manager/api'), clone = await f.repo('clone');
  const remote = await target(f, 'origin'), changed = await target(f, 'changed');
  await f.git(child, 'remote', 'add', 'origin', remote); await f.git(clone, 'remote', 'add', 'origin', remote);
  await f.install(manager); await writeFile(join(manager, 'vcs/repos.conf'), 'api\n');
  await f.install(manager, { repositories: { api: { sync: null } } });
  const entry = join(manager, 'vcs/run.mjs');
  const args = ['publish', '--repository', 'api', '--remote', 'origin', '--ref', 'refs/heads/main'];
  const plan = await f.run(entry, args);
  const apply = () => f.run(cli, [...args, '--project', manager, '--apply', '--expected-fingerprint', plan.fingerprint]);
  assert.equal((await f.run(entry, ['check', '--repository', 'api', '--checkout', clone])).code, 'WORKSPACE_CHECKOUT_MISMATCH');
  assert.equal((await f.run(entry, ['check', '--repository', 'missing'])).code, 'WORKSPACE_UNREGISTERED');
  assert.equal((await f.run(entry, ['check', '--checkout', child])).code, 'missing_argument');
  assert.equal((await f.run(cli, [...args, '--project', manager, '--workspace', clone])).code, 'WORKSPACE_CONFLICT');
  await writeFile(join(manager, 'vcs/repos.conf'), '');
  assert.equal((await apply()).code, 'WORKSPACE_UNREGISTERED');
  await writeFile(join(manager, 'vcs/repos.conf'), 'api\n');
  const configFile = join(manager, 'vcs/config.json'), original = await readFile(configFile, 'utf8');
  const policy = JSON.parse(original); policy.repositories.api.targets.extra = { fetch: remote, push: remote };
  await writeFile(configFile, JSON.stringify(policy));
  assert.equal((await apply()).code, 'harness_plan_changed');
  await writeFile(configFile, original);
  await f.git(child, 'remote', 'set-url', 'origin', changed);
  assert.equal((await apply()).code, 'harness_target_changed');
  await f.git(child, 'remote', 'set-url', 'origin', remote);
  await writeFile(join(child, 'new.txt'), 'new commit\n'); await f.git(child, 'add', 'new.txt'); await f.git(child, 'commit', '-qm', 'new ref');
  assert.equal((await apply()).code, 'harness_plan_changed');
  const checkout = join(f.root, 'checkout'); await f.git(child, 'worktree', 'add', '-b', 'selected-checkout', checkout);
  const checkoutArgs = [...args, '--checkout', checkout];
  const checkoutPlan = await f.run(entry, checkoutArgs);
  const gitFile = join(checkout, '.git'); await writeFile(gitFile, `gitdir: ${join(clone, '.git')}\n`);
  assert.equal((await f.run(entry, [...checkoutArgs, '--apply', '--expected-fingerprint', checkoutPlan.fingerprint])).code, 'WORKSPACE_CHECKOUT_MISMATCH');
  assert.equal(await f.git(remote, 'for-each-ref'), ''); assert.equal(await f.git(changed, 'for-each-ref'), '');
});

test('interrupted multi-repository installation rejects changed child targets before recovery', async () => {
  const f = await repositoryFixture(), manager = await f.repo('manager'), child = await f.repo('manager/api');
  const remote = await target(f, 'origin'), other = await target(f, 'other');
  await f.git(child, 'remote', 'add', 'origin', remote);
  await f.install(manager); await writeFile(join(manager, 'vcs/repos.conf'), 'api\n');
  const input = join(f.root, 'choice.json');
  await writeFile(input, JSON.stringify({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'], repositories: { api: { sync: null } } }));
  const args = ['--project', manager, '--input', input], record = join(manager, 'data/git-setup/interrupted.json');
  const plan = await f.run(cli, ['harness-plan', ...args]);
  const lock = join(manager, '.git/config.lock'); await writeFile(lock, 'fixture lock');
  const stopped = await f.run(cli, ['harness-apply', ...args, '--plan-file', record, '--expected-fingerprint', plan.fingerprint, '--apply']);
  assert.equal(stopped.code, 'setup_config_busy', JSON.stringify(stopped));
  await rm(lock);
  await f.git(child, 'remote', 'set-url', 'origin', other);
  const recovery = await f.run(cli, ['harness-recover', '--project', manager, '--plan-file', record]);
  assert.equal(recovery.code, 'harness_recovery_changed', JSON.stringify(recovery));
});

test('selected sync reads back partial results and stops later targets when the published source changes', async () => {
  const f = await repositoryFixture(), manager = await f.repo('manager'), child = await f.repo('manager/api');
  const origin = await target(f, 'origin'), first = await target(f, 'first'), second = await target(f, 'second');
  for (const [name, url] of Object.entries({ origin, first, second })) await f.git(child, 'remote', 'add', name, url);
  await f.git(child, 'push', 'origin', 'refs/heads/main');
  const initial = await f.git(child, 'rev-parse', 'main');
  await f.git(child, 'push', 'first', 'refs/heads/main:refs/heads/keep');
  await f.install(manager); await writeFile(join(manager, 'vcs/repos.conf'), 'api\n');
  await f.install(manager, { repositories: { api: { sync: { source: 'origin', targets: ['first', 'second'], refs: ['refs/heads/main'] } } } });
  const entry = join(manager, 'vcs/run.mjs'), args = ['sync', '--repository', 'api'];
  const perform = async () => {
    const preview = await f.run(entry, args);
    assert.equal(preview.status, 'planned', JSON.stringify(preview));
    return f.run(entry, [...args, '--expected-fingerprint', preview.fingerprint, '--apply']);
  };
  const hook = join(second, 'hooks/pre-receive');
  await writeFile(hook, '#!/bin/sh\nexit 1\n', { mode: 0o755 });
  const partial = await perform();
  assert.equal(partial.complete, false, JSON.stringify(partial));
  assert.equal(partial.results.find(item => item.remote === 'first').status, 'verified');
  assert.equal(partial.results.find(item => item.remote === 'second').status, 'failed');
  assert.equal(await f.git(first, 'rev-parse', 'main'), initial);
  assert.equal(await f.git(second, 'for-each-ref'), '');
  await rm(hook);
  const recovered = await perform();
  assert.equal(recovered.complete, true, JSON.stringify(recovered));
  assert.equal(recovered.results.find(item => item.remote === 'first').unchanged, true);
  assert.equal(await f.git(second, 'rev-parse', 'main'), initial);
  await writeFile(join(child, 'next.txt'), 'published change\n'); await f.git(child, 'add', '.'); await f.git(child, 'commit', '-qm', 'next');
  await f.git(child, 'push', 'origin', 'refs/heads/main');
  const reviewed = await f.git(child, 'rev-parse', 'main');
  await writeFile(join(child, 'next.txt'), 'future source\n'); await f.git(child, 'add', '.'); await f.git(child, 'commit', '-qm', 'future');
  const future = await f.git(child, 'rev-parse', 'main');
  await f.git(child, 'push', 'origin', 'refs/heads/main:refs/heads/future');
  const quote = text => `'${text.replaceAll("'", "'\\''")}'`;
  await writeFile(join(first, 'hooks/pre-receive'), `#!/bin/sh\nunset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_QUARANTINE_PATH\ngit -C ${quote(origin)} update-ref refs/heads/main ${quote(future)}\n`, { mode: 0o755 });
  const changed = await perform();
  assert.equal(changed.complete, false, JSON.stringify(changed));
  assert.equal(changed.sourceStable, false);
  assert.equal(changed.status, 'source_changed_or_unreadable');
  assert.equal(changed.results.find(item => item.remote === 'first').actual, reviewed);
  assert.equal(changed.results.find(item => item.remote === 'second').status, 'failed');
  assert.equal(await f.git(first, 'rev-parse', 'main'), reviewed);
  assert.equal(await f.git(second, 'rev-parse', 'main'), initial);
  assert.equal(await f.git(first, 'rev-parse', 'keep'), initial);
  assert.equal(await f.git(first, 'for-each-ref', '--format=%(refname)'), 'refs/heads/keep\nrefs/heads/main');
});
