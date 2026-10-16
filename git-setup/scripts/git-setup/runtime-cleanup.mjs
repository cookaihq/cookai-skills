import { readFile, readdir, lstat, open, rm } from 'node:fs/promises';
import { join, resolve, dirname } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { inspectProject, localGit } from './repository-facts.mjs';
import { digest, packageFiles, safePath, runtimeFail as fail } from './runtime-bundle.mjs';

const exec = promisify(execFile);
async function exists(path) {
  try { await lstat(path); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

// No process arguments or configuration values leave this module.
async function processes() {
  try {
    const options = { encoding: 'utf8', timeout: 15000, maxBuffer: 8 * 1024 * 1024 };
    let rows;
    if (process.platform === 'win32') {
      const script = '$ErrorActionPreference="Stop"; @(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress';
      const output = await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], options);
      rows = JSON.parse(output.stdout).map(row => ({ pid: row.ProcessId, name: row.Name, args: row.CommandLine || '' }));
    } else {
      const output = await exec('ps', ['-axww', '-o', 'pid=', '-o', 'ucomm=', '-o', 'args='], options);
      rows = output.stdout.trim().split('\n').map(line => {
        const match = line.match(/^\s*(\d+)\s+(\S+)\s*(.*)$/);
        if (!match) throw new Error('unreadable process');
        return { pid: Number(match[1]), name: match[2], args: match[3] };
      });
    }
    if (!rows.length) throw new Error('empty process list');
    return rows.filter(row => row.pid !== process.pid);
  } catch { fail('runtime_cleanup_processes', '无法完整检查运行进程；保留旧运行包。'); }
}

async function snapshot(project, held = new Set()) {
  const state = await inspectProject(project);
  if (!state.isGit) fail('runtime_cleanup_project', '清理需要实际 Git 仓库。');
  const common = state.commonDir, parent = join(common, 'git-setup');
  await safePath(common, parent);
  // Git 2.29 has porcelain output but not worktree list -z. Quoted paths may
  // contain escaped newlines; reject unfamiliar escapes instead of splitting
  // or guessing the checkout path.
  const worktrees = (await localGit(state.root, ['-c', 'core.quotePath=false', 'worktree', 'list', '--porcelain'])).split('\n').filter(line => line.startsWith('worktree ')).map(line => {
    const path = line.slice(9);
    try { return path.startsWith('"') ? JSON.parse(path) : path; }
    catch { fail('runtime_cleanup_configuration', '无法解析 Worktree 路径；保留旧运行包。'); }
  });
  if (!worktrees.length) fail('runtime_cleanup_configuration', '没有可核验的 Worktree；保留旧运行包。');
  const locks = new Set([join(common, 'config.lock')]), configuration = [], references = [];
  for (const root of worktrees) {
    const item = await inspectProject(root);
    if (!item.isGit || item.root !== root || item.commonDir !== common) fail('runtime_cleanup_configuration', '登记的 Worktree 无法核验；保留旧运行包。');
    locks.add(join(item.gitDir, 'config.worktree.lock'));
    const records = (await localGit(root, ['config', '--show-origin', '--null', '--list', '--includes'])).split('\0');
    if (records.pop() !== '' || records.length % 2) fail('runtime_cleanup_configuration', '无法核验 Git 配置来源；保留旧运行包。');
    const values = [];
    for (let index = 0; index < records.length; index += 2) {
      const origin = records[index];
      if (!origin.startsWith('file:')) fail('runtime_cleanup_configuration', 'Git 配置来源不是可锁定文件；保留旧运行包。');
      const source = resolve(root, origin.slice(5));
      await safePath(dirname(source), source);
      if (!(await lstat(source)).isFile()) fail('runtime_cleanup_configuration', 'Git 配置来源不是普通文件；保留旧运行包。');
      locks.add(`${source}.lock`);
      values.push(records[index + 1]);
    }
    const raw = values.join('\0');
    // Inactive includeIf branches may reference an old runtime. Do not guess them.
    if (raw.split('\0').some(entry => /^include(?:if\..+)?\.path\n/i.test(entry))) fail('runtime_cleanup_configuration', 'Git 配置包含 include/includeIf；无法穷举所有条件引用，保留旧运行包。');
    references.push(raw);
    configuration.push({ root, gitDir: item.gitDir, sha256: digest(records) });
  }
  for (const path of locks) if (!held.has(path) && await exists(path)) fail('runtime_cleanup_busy', 'Git 配置正在修改或有未完成锁；保留旧运行包。');
  if (await exists(join(common, 'git-setup-harness.lock'))) fail('runtime_cleanup_busy', '项目脚本安装尚未完成；先按原记录恢复。');
  const entries = await exists(parent) ? await readdir(parent, { withFileTypes: true }) : [];
  if (entries.length > 1000) fail('runtime_cleanup_budget', '运行包数量超过检查预算；未执行删除。');
  if (entries.some(entry => entry.name.startsWith('.install-'))) fail('runtime_cleanup_busy', '发现运行包安装残留；先核对安装结果。');
  const rows = await processes();
  // A Git process can have read the old helper before a configuration update,
  // but not have launched Node yet. Conservatively wait for every Git process.
  if (rows.some(row => /^git(?:-|\.exe$|$)/i.test(row.name) || (/^(?:node|nodejs)(?:\.exe)?$/i.test(row.name) && !row.args))) fail('runtime_cleanup_busy', '仍有 Git 进程或无法核验的 Node 进程；结束相关操作后重新检查。');
  // Older installers created the package before taking config.lock. Do not
  // collect that package while an older public or generated auth entry runs.
  if (rows.some(row => /(?:cli|run)\.mjs["']?\s+(?:setup|auth-prepare|auth-verify)\b/.test(row.args)
    || (row.args.includes('credential-helper.mjs') && !entries.some(entry => /^[a-f0-9]{64}$/.test(entry.name) && row.args.includes(entry.name))))) fail('runtime_cleanup_busy', '仍有鉴权安装或无法定位运行包的 helper 进程；保留旧运行包。');
  const candidates = [], retained = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const id = entry.name, path = join(parent, id);
    let hashes;
    try {
      if (!entry.isDirectory() || !/^[a-f0-9]{64}$/.test(id)) throw new Error('unknown');
      hashes = await packageFiles(path);
      const names = Object.keys(hashes);
      if (!hashes['scripts/git-setup/credential-helper.mjs'] || !hashes.LICENSE || !hashes['package.json']
        || names.some(name => !['package.json', 'LICENSE'].includes(name) && !/^scripts\/git-setup\/[a-z][a-z0-9-]*\.mjs$/.test(name))
        || JSON.parse(await readFile(join(path, 'package.json'), 'utf8')).name !== 'git-setup') throw new Error('unknown');
      const sorted = Object.fromEntries(Object.entries(hashes).sort(([a], [b]) => a.localeCompare(b)));
      const legacy = Object.fromEntries(['package.json', 'LICENSE', ...names.filter(name => name.startsWith('scripts/')).sort()].map(name => [name, hashes[name]]));
      if (![digest(sorted), digest(legacy)].includes(id)) throw new Error('modified');
    } catch { retained.push({ id, path, reason: 'modified_or_unknown' }); continue; }
    if (references.some(raw => raw.includes(id))) retained.push({ id, path, reason: 'referenced' });
    else if (rows.some(row => row.args.includes(id))) retained.push({ id, path, reason: 'active_process' });
    else candidates.push({ id, path });
  }
  const report = { status: 'planned', project: state.root, commonDir: common, worktrees, candidates, retained };
  return { ...report, fingerprint: digest({ ...report, configuration }), locks: [...locks].sort() };
}

export async function cleanupRuntimes(project, { apply = false, expectedFingerprint } = {}) {
  const before = await snapshot(project);
  if (!apply) { const { locks, ...report } = before; return report; }
  if (!expectedFingerprint || expectedFingerprint !== before.fingerprint) fail('runtime_cleanup_changed', '运行包、引用或运行进程发生变化；重新检查清理计划。');
  const held = new Set(), removed = [], owner = randomUUID();
  try {
    for (const path of before.locks) {
      await safePath(dirname(path), path);
      try {
        const lock = await open(path, 'wx', 0o600);
        try { await lock.writeFile(owner); held.add(path); } finally { await lock.close(); }
      }
      catch (error) { if (error.code === 'EEXIST') fail('runtime_cleanup_busy', 'Git 配置已被其他操作锁定；未删除其锁。'); throw error; }
    }
    const current = await snapshot(project, held);
    if (current.fingerprint !== expectedFingerprint) fail('runtime_cleanup_changed', '取得锁后事实已变化；重新检查清理计划。');
    for (const item of current.candidates) {
      // Recheck package integrity and active processes immediately before removal.
      const latest = await snapshot(project, held);
      if (!latest.candidates.some(candidate => candidate.id === item.id)) fail('runtime_cleanup_changed', '删除前运行包状态已变化；停止剩余清理。');
      await rm(item.path, { recursive: true });
      removed.push(item.path);
    }
    return { status: 'cleaned', complete: true, removed, retained: current.retained };
  } catch (error) {
    error.message = `${error.message}${removed.length ? ` 已删除 ${removed.length} 个此前核验的运行包；其余保留。` : ''}`;
    throw error;
  } finally {
    for (const path of held) {
      try { if (await readFile(path, 'utf8') === owner) await rm(path); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }
}
