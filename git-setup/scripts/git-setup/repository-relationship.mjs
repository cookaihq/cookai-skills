import { readFile, lstat, realpath } from 'node:fs/promises';
import { join, resolve, relative, dirname, sep } from 'node:path';
import { mainCheckout, localGit, isGitDirectory, authFail as fail } from './repository-facts.mjs';
import { resolveHarnessDirectory, portablePath } from './harness-location.mjs';
import { digest, safePath } from './runtime-bundle.mjs';

async function optionalFile(file) {
  try {
    const info = await lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 262144) fail('WORKSPACE_REGISTRY_INVALID', '仓库登记必须是普通小文件，不得经符号链接读取。');
    return await readFile(file, 'utf8');
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

/** Registration is authoritative; bindings only remember which manager to revalidate. */
export async function registeredRepositories(manager, { harnessDirectory, registrySourceDirectory } = {}) {
  const outer = typeof manager === 'string' ? await mainCheckout(manager) : manager;
  if (!outer.isGit) fail('WORKSPACE_INVALID', '管理仓必须是实际 Git 仓库。');
  const directory = await resolveHarnessDirectory(outer.root, harnessDirectory);
  const entries = [], paths = new Set(), commonDirs = new Set();
  const add = async (repositoryPath, kind, evidence, moduleName) => {
    portablePath(repositoryPath);
    if (paths.has(repositoryPath)) fail('WORKSPACE_DUPLICATE', '仓库路径重复登记；子模块不得同时写入独立仓清单。');
    paths.add(repositoryPath);
    const candidate = resolve(outer.root, repositoryPath);
    await safePath(outer.root, candidate);
    let gitlink = null;
    if (kind === 'submodule') {
      const raw = await localGit(outer.root, ['ls-files', '--stage', '-z', '--', repositoryPath]);
      const records = raw.split('\0').filter(Boolean);
      const match = records.length === 1 && /^160000 ([a-f0-9]+) 0\t(.+)$/.exec(records[0]);
      if (!match || match[2] !== repositoryPath) fail('WORKSPACE_SUBMODULE_INVALID', '子模块缺少唯一的 Git index gitlink 登记。');
      gitlink = match[1];
    }
    let repository;
    try { repository = await mainCheckout(candidate); }
    catch (error) {
      if (error.code !== 'PROJECT_UNAVAILABLE') throw error;
    }
    if (!repository?.isGit || repository.root !== candidate) {
      // A missing checkout is a normal registration state. Corrupt or linked Git
      // metadata is never treated as an uninitialized checkout.
      try { await lstat(join(candidate, '.git')); fail('WORKSPACE_REPOSITORY_INVALID', '登记路径存在无法验证的 Git 元数据。'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      entries.push({ path: repositoryPath, kind, evidence, moduleName: moduleName || null, gitlink, repository: null,
        unavailable: kind === 'submodule' ? 'WORKSPACE_SUBMODULE_UNINITIALIZED' : 'WORKSPACE_REPOSITORY_MISSING' });
      return;
    }
    if (repository.commonDir === outer.commonDir || commonDirs.has(repository.commonDir)) fail('WORKSPACE_DUPLICATE', '重复登记同一仓库或把管理仓自身列为子仓。');
    commonDirs.add(repository.commonDir);
    if (kind === 'submodule') {
      const parent = repository.superprojectRoot && await mainCheckout(repository.superprojectRoot);
      const expected = resolve(outer.root, (await localGit(outer.root, ['rev-parse', '--git-path', `modules/${moduleName}`])).trim());
      let moduleDir;
      try { moduleDir = await realpath(expected); } catch { fail('WORKSPACE_SUBMODULE_INVALID', '子模块的 Git 元数据登记缺失。'); }
      if (!parent || parent.commonDir !== outer.commonDir || moduleDir !== repository.commonDir) fail('WORKSPACE_SUBMODULE_INVALID', '子模块的 .gitmodules、gitlink 和实际 Git 元数据不一致。');
    } else if (repository.superprojectRoot) fail('WORKSPACE_SUBMODULE_DUPLICATE', '子模块只能以 Git 登记为准，不能作为独立仓登记。');
    entries.push({ path: repositoryPath, kind, evidence, moduleName: moduleName || null, gitlink, repository });
  };
  const modules = await optionalFile(join(outer.root, '.gitmodules'));
  if (modules !== null) {
    const raw = await localGit(outer.root, ['config', '-z', '--file', '.gitmodules', '--get-regexp', String.raw`^submodule\..*\.path$`], true);
    for (const record of (raw || '').split('\0').filter(Boolean)) {
      const split = record.indexOf('\n'), key = record.slice(0, split), value = record.slice(split + 1);
      if (split < 0) fail('WORKSPACE_REGISTRY_INVALID', '.gitmodules 路径字段无效。');
      await add(value, 'submodule', join(outer.root, '.gitmodules'), key.slice(10, -5));
    }
  }
  let registry = null;
  if (directory) {
    // Recovery may read the already verified staged/backup package while the
    // original directory is between atomic renames. Evidence keeps its real destination.
    const source = registrySourceDirectory || join(outer.root, directory);
    const file = join(source, 'repos.conf');
    await safePath(outer.root, file);
    registry = await optionalFile(file);
    if (registry !== null) {
      let manifest;
      try { manifest = JSON.parse(await readFile(join(source, 'manifest.json'), 'utf8')); }
      catch { fail('WORKSPACE_HARNESS_INVALID', '独立仓登记缺少有效的 Harness 清单。'); }
      if (manifest.standard !== 'git-setup' || manifest.directory !== directory) fail('WORKSPACE_HARNESS_INVALID', '独立仓清单不属于指定 Harness。');
      for (const line of registry.split(/\r?\n/)) {
        const value = line.trim();
        if (!value || value.startsWith('#')) continue;
        await add(value, 'independent', join(outer.root, directory, 'repos.conf'));
      }
    }
  }
  return { outer, directory, entries, fingerprint: digest({ root: outer.root, commonDir: outer.commonDir, modules, registry, entries }) };
}

async function metadataSuperproject(repository) {
  if (repository.superprojectRoot) return repository.superprojectRoot;
  if (!repository.isGit) return null;
  // Git stops reporting --show-superproject-working-tree after a gitlink is removed.
  // Its module storage still points to the manager; validate that metadata, never directory nesting.
  const marker = `${sep}modules${sep}`, index = repository.commonDir.lastIndexOf(marker);
  if (index < 0) return null;
  const gitDir = repository.commonDir.slice(0, index);
  if (!await isGitDirectory(gitDir)) return null;
  const core = (await localGit(repository.root, ['--git-dir', gitDir, 'config', '--local', '--get', 'core.worktree'], true))?.trim();
  const backlink = await optionalFile(join(gitDir, 'gitdir'));
  const candidate = core ? resolve(gitDir, core) : backlink ? dirname(backlink.trim()) : gitDir.endsWith(`${sep}.git`) ? dirname(gitDir) : null;
  if (!candidate) fail('WORKSPACE_SUBMODULE_INVALID', '子模块元数据不能定位原管理检出。');
  const outer = await mainCheckout(candidate);
  if (outer.gitDir !== gitDir) fail('WORKSPACE_SUBMODULE_INVALID', '子模块保存位置与管理仓 Git 元数据不一致。');
  return outer.root;
}

export async function resolveRepository(project, options = {}) {
  const repository = await mainCheckout(project);
  const candidates = [];
  let cursor = repository;
  const seen = new Set([repository.commonDir]);
  while (true) {
    const parent = await metadataSuperproject(cursor);
    if (!parent) break;
    const outer = await mainCheckout(parent);
    if (seen.has(outer.commonDir)) fail('WORKSPACE_CYCLE', '仓库管理关系出现循环。');
    seen.add(outer.commonDir);
    const registration = await registeredRepositories(outer, {});
    if (!registration.entries.some(entry => entry.repository?.commonDir === cursor.commonDir)) fail('WORKSPACE_UNREGISTERED', 'Git 外仓没有有效登记当前仓库。');
    candidates.push(registration); cursor = outer;
  }
  const hint = options.expectedRelationship;
  const explicit = options.workspace || options.credentialWorkspace || hint?.workspace || options.expectedOwner?.main;
  if (options.workspace && options.credentialWorkspace && (await mainCheckout(options.workspace)).commonDir !== (await mainCheckout(options.credentialWorkspace)).commonDir) fail('WORKSPACE_CONFLICT', '操作管理仓与凭证管理仓必须使用同一已确认关系。');
  let management = repository, registration = { kind: 'self', path: '.', evidence: 'git', gitlink: null }, directory = null;
  if (explicit) {
    const chosen = await mainCheckout(explicit);
    if (chosen.commonDir !== repository.commonDir || chosen.main !== repository.main) {
      if (!chosen.isGit) fail('WORKSPACE_INVALID', '所选管理仓不可用。');
      let selected = candidates.find(item => item.outer.commonDir === chosen.commonDir);
      selected ||= await registeredRepositories(chosen, { harnessDirectory: options.harnessDirectory || hint?.directory, registrySourceDirectory: options.registrySourceDirectory });
      const entry = selected.entries.find(item => item.repository?.commonDir === repository.commonDir);
      if (!entry && !candidates.includes(selected)) fail('WORKSPACE_UNREGISTERED', '指定管理仓未登记当前仓库；路径嵌套、同一 URL 或旧本机绑定均不能替代登记。');
      management = selected.outer; directory = selected.directory;
      registration = entry ? { kind: entry.kind, path: entry.path, evidence: entry.evidence, gitlink: entry.gitlink } : { kind: 'submodule-chain', path: relative(management.root, repository.main), evidence: candidates.map(item => item.fingerprint) };
    }
  } else if (candidates.length) {
    if (candidates.length !== 1) fail('WORKSPACE_AMBIGUOUS', `多个已验证管理仓，需要明确选择：${candidates.map(item => item.outer.root).join('、')}`);
    management = candidates[0].outer; directory = candidates[0].directory;
    const entry = candidates[0].entries.find(item => item.repository?.commonDir === repository.commonDir);
    registration = { kind: entry.kind, path: entry.path, evidence: entry.evidence, gitlink: entry.gitlink };
  }
  if (options.expectedOwner && (management.main !== options.expectedOwner.main || (options.expectedOwner.commonDir !== null && management.commonDir !== options.expectedOwner.commonDir))) fail('auth_owner_changed', '已绑定的凭证主检出发生变化；修复原关系，不切换来源。');
  const result = { initiatingProject: repository.requestedPath, project: repository, management, harness: { root: management.root, directory }, registration };
  // Only the selected relationship is relevant; unrelated remote URL changes are not dependencies.
  result.fingerprint = digest({ project: [repository.root, repository.main, repository.commonDir], management: [management.root, management.main, management.commonDir], directory, registration });
  return result;
}

export const relationshipSelection = relation => ({ workspace: relation.management.root, directory: relation.harness.directory });

/** Select one registered repository, then verify an optional checkout by common directory. */
export async function selectRepository(manager, { repository = '.', checkout, harnessDirectory, registrySourceDirectory } = {}) {
  const outer = await mainCheckout(manager);
  if (!outer.isGit || outer.requestedPath !== outer.root) fail('WORKSPACE_INVALID', 'Harness 所属项目必须是实际 Git 检出根目录。');
  let selected = outer;
  if (repository !== '.') {
    portablePath(repository);
    const registered = await registeredRepositories(outer, { harnessDirectory, registrySourceDirectory });
    const entry = registered.entries.find(item => item.path === repository);
    if (!entry) fail('WORKSPACE_UNREGISTERED', '所选仓库没有登记在这个管理仓；不回退操作外仓。');
    if (!entry.repository) fail(entry.unavailable, `所选登记仓库不可用：${repository}`);
    selected = entry.repository;
  }
  if (checkout !== undefined) {
    const requested = await mainCheckout(resolve(outer.root, checkout));
    if (requested.root !== requested.requestedPath || requested.commonDir !== selected.commonDir || requested.main !== selected.main) fail('WORKSPACE_CHECKOUT_MISMATCH', '指定检出不属于已选仓库；相同 URL 或目录嵌套不构成依据。');
    selected = requested;
  }
  const relationship = await resolveRepository(selected.root, { workspace: outer.root, harnessDirectory, registrySourceDirectory });
  return { key: repository, relationship, project: selected, manager: outer };
}
