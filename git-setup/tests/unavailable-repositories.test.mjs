import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { repositoryFixture } from './fixtures/repositories.mjs';

test('selected initialized repositories remain usable when unrelated registrations are unavailable, and upgrades retain their policies', async () => {
  const f = await repositoryFixture(), seed = await f.repo('seed'), outer = await f.repo('outer');
  for (const name of ['api', 'unused']) await f.git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', seed, name);
  await f.configure(outer, { repositories: { api: { sync: null }, unused: { sync: null } } });
  const before = JSON.parse(await readFile(join(outer, 'vcs/config.json'), 'utf8'));
  await f.git(outer, 'submodule', 'deinit', '-f', 'unused');
  const checked = await f.command(outer, ['check', '--repository', 'api']);
  assert.equal(checked.status, 'checked', JSON.stringify(checked));
  assert.equal((await f.command(outer, ['check', '--repository', 'unused'])).code, 'WORKSPACE_SUBMODULE_UNINITIALIZED');
  await f.configure(outer);
  assert.deepEqual(JSON.parse(await readFile(join(outer, 'vcs/config.json'), 'utf8')).repositories, before.repositories);
  await writeFile(join(outer, 'unused/.git'), 'gitdir: missing-metadata\n');
  assert.notEqual((await f.command(outer, ['check', '--repository', 'api'])).status, 'checked');
});
