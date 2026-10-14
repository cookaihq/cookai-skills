import { readFile, lstat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { inspect } from './git.mjs';
import { inspectAuth } from './auth.mjs';
import { readHarnessConfig, assertHarnessTargets, repositoryPolicy } from './harness-config.mjs';
import { selectRepository } from './repository-relationship.mjs';
import { packageFiles, digest, safePath, runtimeFail as fail } from './runtime-bundle.mjs';

// No project rule discovery or comparison with the installing Skill in this path.
export async function projectCheck(root, { directory, command = 'check', remote, repository = '.', checkout } = {}) {
  const installed = await readHarnessConfig(root, directory);
  if (!installed.exists) fail('harness_config_missing', '未找到已明确登记的项目 Harness；提供实际安装位置，不回退固定目录。');
  const selection = await selectRepository(root, { repository, checkout, harnessDirectory: installed.directory });
  const policy = repositoryPolicy(installed.config, selection.key);
  const selected = { ...installed, config: policy, fingerprint: digest(policy) };
  const installation = { root, path: join(root, selected.directory), directory: selected.directory };
  const local = await inspect(selection.project.root);
  if (!local.initialized || local.bare || local.insideParent || resolve(selection.project.root) !== local.root) fail('harness_runtime_invalid', '操作检出必须指向实际 Git 工作区根目录。');
  const path = installation.path;
  let hashes;
  try {
    await safePath(root, join(path, 'manifest.json'));
    if (!(await lstat(join(path, 'manifest.json'))).isFile()) throw new Error('manifest');
    const manifest = JSON.parse(await readFile(join(path, 'manifest.json'), 'utf8'));
    if (manifest.standard !== 'git-setup' || manifest.schemaVersion !== 1 || manifest.directory !== selected.directory || !manifest.files || Array.isArray(manifest.files)) throw new Error('manifest');
    hashes = Object.fromEntries(Object.entries(await packageFiles(join(path, 'runtime'))).map(([name, sha]) => [`runtime/${name}`, sha]));
    await safePath(root, join(path, 'run.mjs'));
    hashes['run.mjs'] = digest(await readFile(join(path, 'run.mjs')));
    const expected = Object.entries(manifest.files).filter(([name]) => name === 'run.mjs' || name.startsWith('runtime/'));
    if (!hashes['runtime/scripts/git-setup/project-entry.mjs'] || expected.length !== Object.keys(hashes).length || expected.some(([name, sha]) => hashes[name] !== sha)) throw new Error('runtime');
  } catch { fail('harness_runtime_invalid', '项目运行包与清单不符；修复实际项目文件，不回退 Skill。'); }
  const names = command === 'check' ? Object.keys(selected.config.targets)
    : command === 'sync' ? [selected.config.sync?.source, ...(selected.config.sync?.targets || [])].filter(Boolean) : [remote];
  const operationRoot = selection.project.root;
  await assertHarnessTargets(operationRoot, selected.config, names);
  const authentication = [];
  for (const name of names) {
    const target = local.remotes.find(item => item.name === name);
    const urls = command === 'read' ? target.fetch : command === 'publish' ? target.push : [...target.fetch, ...target.push];
    for (const url of new Set(urls)) authentication.push({ url, ...await inspectAuth(operationRoot, url, { workspace: root, harnessDirectory: installed.directory }) });
  }
  return { status: 'checked', project: operationRoot, installation, repository: selection.key, relationship: selection.relationship, local, selected, authentication,
    fingerprint: digest({ installation, relationship: selection.relationship.fingerprint, repository: selection.key, branch: local.branch, author: local.author, runtime: hashes, config: selected.fingerprint, targets: local.remotes.filter(item => names.includes(item.name)), authentication }),
    network: 'not_performed', actualPush: 'not_performed' };
}
