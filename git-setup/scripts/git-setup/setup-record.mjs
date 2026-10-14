import { mkdir, realpath, readFile, lstat, open, rename, rm } from 'node:fs/promises';
import { resolve, dirname, relative, join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { inspectProject } from './config.mjs';

const exec = promisify(execFile);
const fail = message => { throw Object.assign(new Error(message), { code: 'setup_record_path' }); };

/** Agent-selected recovery file: ignored data/git-setup or tmp/git-setup in an existing workspace. */
export async function saveSetupRecord(file, record, { createOnly = false, beforeWrite } = {}) {
  file = resolve(file);
  let parent = dirname(file);
  while (true) {
    try { await lstat(parent); break; }
    catch (error) { if (error.code !== 'ENOENT' || parent === dirname(parent)) throw error; parent = dirname(parent); }
  }
  if (await realpath(parent) !== parent) fail('计划记录路径不得经过符号链接。');
  const project = await inspectProject(parent);
  const target = relative(project.root, file).replaceAll('\\', '/');
  if (!project.isGit || !/^(?:data|tmp)\/git-setup\/.+\.json$/.test(target)) fail('计划记录必须位于已有工作区的 data/git-setup/ 或 tmp/git-setup/ 中。空克隆目录应使用调用工作区中的记录。');
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG)/.test(key)) delete env[key];
  const git = async args => (await exec('git', ['-C', project.root, ...args], { env, timeout: 15000 })).stdout;
  if ((await git(['ls-files', '--', target])).trim()) fail('计划记录已被 Git 跟踪，不能写入。');
  try { await git(['check-ignore', '-q', '--', target]); }
  catch { fail('计划记录未被 Git 忽略；请按项目规则明确忽略位置，不自动修改 .gitignore。'); }
  await mkdir(dirname(file), { recursive: true });
  if (await realpath(dirname(file)) !== dirname(file)) fail('计划目录不得经过符号链接。');
  try { if ((await lstat(file)).isSymbolicLink()) fail('计划记录不得是符号链接。'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const lockPath = `${file}.lock`, temp = join(dirname(file), `.setup-${randomUUID()}.json`);
  let lock;
  try {
    lock = await open(lockPath, 'wx', 0o600);
    // Check caller-specific replacement conditions while holding the record lock.
    if (beforeWrite) await beforeWrite();
    if (createOnly) {
      try { await lstat(file); fail('本次操作记录已经存在；先核对原记录，不覆盖或重发。'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    const out = await open(temp, 'wx', 0o600);
    try { await out.writeFile(`${JSON.stringify({ schemaVersion: 1, ...record }, null, 2)}\n`); } finally { await out.close(); }
    await rename(temp, file);
    return JSON.parse(await readFile(file, 'utf8'));
  } finally {
    await rm(temp, { force: true });
    if (lock) { await lock.close(); await rm(lockPath, { force: true }); }
  }
}
