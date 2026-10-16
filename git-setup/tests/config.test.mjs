import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, mkdir, lstat } from 'node:fs/promises';
import { join } from 'node:path';
import { inspectProject, loadEnvironment } from '../scripts/git-setup/config.mjs';
import { repositoryFixture } from './fixtures/repositories.mjs';

const group = token => ({ GIT_SETUP_GITHUB_TOKEN: token, GIT_SETUP_GITHUB_HOST: 'github.com', GIT_SETUP_GITHUB_REPOSITORY: 'example/issues' });
const dotenv = values => Object.entries(values).map(([key, value]) => `${key}=${value}\n`).join('');

test('authentication reads the first nonempty group and never mixes values across layers', async () => {
  const f = await repositoryFixture(), root = await f.repo('project');
  await writeFile(join(root, '.env'), dotenv(group('base-fixture')));
  await writeFile(join(root, '.env.local'), dotenv(group('local-fixture')));
  await writeFile(join(root, '.env.git-setup'), dotenv(group('skill-fixture')));
  const loaded = await loadEnvironment(root, { env: {} });
  assert.equal(loaded.values.GIT_SETUP_GITHUB_TOKEN, 'skill-fixture');
  assert.equal(loaded.sources.GIT_SETUP_GITHUB_AUTH, join(root, '.env.git-setup'));
  assert.equal((await loadEnvironment(root, { env: group('process-fixture') })).sources.GIT_SETUP_GITHUB_TOKEN, 'process');
  await assert.rejects(loadEnvironment(root, { env: { GIT_SETUP_GITHUB_TOKEN: 'incomplete' } }), { code: 'CREDENTIAL_GROUP_INCOMPLETE' });
  const gh = await loadEnvironment(root, { env: { GIT_SETUP_GITHUB_AUTH: 'gh' } });
  assert.equal(gh.values.GIT_SETUP_GITHUB_TOKEN, undefined);
  await assert.rejects(loadEnvironment(root, { env: { ...group('fixture'), GIT_SETUP_GITHUB_AUTH: 'gh' } }), { code: 'CREDENTIAL_GROUP_CONFLICT' });
});

test('personal authentication is opt-in, project takes precedence and parent files are not scanned', async () => {
  const f = await repositoryFixture(), root = await f.repo('project'), globalConfigPath = join(f.root, 'personal.env');
  await writeFile(globalConfigPath, dotenv(group('personal-fixture')));
  await writeFile(join(f.root, '.env'), dotenv(group('parent-fixture')));
  assert.deepEqual((await loadEnvironment(root, { env: {}, globalConfigPath })).values, {});
  assert.equal((await loadEnvironment(root, { env: {}, globalConfigPath, useGlobalConfig: true })).values.GIT_SETUP_GITHUB_TOKEN, 'personal-fixture');
  await writeFile(join(root, '.env.local'), dotenv(group('local-fixture')));
  assert.equal((await loadEnvironment(root, { env: {}, globalConfigPath, useGlobalConfig: true })).values.GIT_SETUP_GITHUB_TOKEN, 'local-fixture');
});

test('retired Issue environment values are neither consumed nor executed', async () => {
  const f = await repositoryFixture(), root = await f.repo('project');
  await writeFile(join(root, '.env.git-setup'), 'GIT_SETUP_ISSUE_JSON=$(touch should-never-exist)\nUNRELATED_SECRET=ignored\n');
  assert.deepEqual(await loadEnvironment(root, { env: { GIT_SETUP_ISSUE_JSON: '{broken' } }), { values: {}, sources: {} });
  await assert.rejects(lstat(join(root, 'should-never-exist')), { code: 'ENOENT' });
});

test('inspection resolves ordinary subdirectories without leaking old remote URL credentials', async () => {
  const f = await repositoryFixture(), root = await f.repo('project'), nested = join(root, 'directory');
  await mkdir(nested);
  await f.git(root, 'remote', 'add', 'origin', 'https://fixture-secret@github.com/example/code.git');
  const result = await inspectProject(nested);
  assert.equal(result.root, root); assert.equal(result.requestedPath, nested);
  assert.equal(JSON.stringify(result).includes('fixture-secret'), false);
  assert.match(JSON.stringify(result), /redacted/);
});
