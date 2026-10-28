import { readFile, lstat, mkdir, open, rename, rm } from 'node:fs/promises';
import { join, resolve, relative, dirname, isAbsolute, sep } from 'node:path';
import { authTarget, localGit, authFail as fail } from './auth-roots.mjs';
import { safePath, fields, publicText } from './validation.mjs';

export const sourceRecordPath = owner => join(owner.main, '.credentials/repositories/sources.local.md');

export function validateSource(entry, owner) {
  fields(entry, ['url', 'method', 'file', 'account'], '本机凭证来源');
  publicText(entry);
  const target = authTarget(entry.url);
  if (!target || !['gh', 'github-token', 'cnb-url', 'environment'].includes(entry.method)
      || (target.host === 'cnb.cool' ? entry.method !== 'cnb-url' : entry.method === 'cnb-url')) fail('auth_source_invalid', '来源的目标与鉴权方式不匹配。');
  if (entry.method === 'gh') {
    if (!/^[A-Za-z0-9-]+$/.test(entry.account || '') || entry.file !== undefined) fail('auth_source_invalid', 'gh 来源必须明确账号且不能包含文件。');
  } else if (['github-token', 'cnb-url'].includes(entry.method)) {
    if (typeof entry.file !== 'string' || /[\r\n\0]/.test(entry.file) || entry.account !== undefined) fail('auth_source_invalid', '文件来源须指定文件路径。');
    const rel = relative(owner.main, resolve(owner.main, entry.file));
    if (!rel || isAbsolute(rel) || rel === '..' || rel.startsWith('..' + sep)) fail('auth_source_invalid', '来源文件须位于已确认凭证主检出。');
  } else if (entry.file !== undefined || entry.account !== undefined) fail('auth_source_invalid', '环境来源不得混入其他文件或账号。');
  return { ...entry, url: target.url };
}

async function protectedRecord(owner, allowMissing = false) {
  const file = sourceRecordPath(owner), rel = relative(owner.main, file).replaceAll('\\', '/');
  await safePath(owner.main, file);
  if ((await localGit(owner.main, ['ls-files', '--', rel])).trim()) fail('auth_source_tracked', '本机来源记录已被跟踪；不读取或写入。');
  if (await localGit(owner.main, ['check-ignore', '-q', '--', rel], true) === null) fail('auth_source_not_ignored', '本机来源记录必须先被 Git 忽略。');
  try {
    const info = await lstat(file);
    if (!info.isFile() || info.nlink !== 1 || info.size > 262144) fail('auth_source_invalid', '本机来源记录必须是普通小文件。');
  } catch (error) { if (!(allowMissing && error.code === 'ENOENT')) throw error; }
  return file;
}

export async function readSources(owner) {
  const file = sourceRecordPath(owner);
  try { await lstat(file); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  await protectedRecord(owner);
  const text = await readFile(file, 'utf8');
  const blocks = [...text.matchAll(/^```json\s*\r?\n([\s\S]*?)^```\s*$/gm)];
  let entries;
  try { if (blocks.length !== 1) throw new Error(); entries = JSON.parse(blocks[0][1]); } catch { fail('auth_source_invalid', '本机来源记录须包含唯一的 JSON 来源数组；不输出原文。'); }
  if (!Array.isArray(entries) || entries.length > 200) fail('auth_source_invalid', '本机来源记录必须是来源数组。');
  const result = entries.map(entry => validateSource(entry, owner));
  if (new Set(result.map(entry => authTarget(entry.url).key)).size !== result.length) fail('auth_source_duplicate', '同一完整目标有重复来源，请明确保留哪个选择。');
  return result;
}

export async function saveSource(owner, entry) {
  entry = validateSource(entry, owner);
  const file = await protectedRecord(owner, true);
  await mkdir(dirname(file), { recursive: true, mode: 0o700 });
  const lock = `${file}.lock`;
  let handle, acquired = false;
  try {
    handle = await open(lock, 'wx', 0o600); acquired = true;
    const entries = await readSources(owner);
    const target = authTarget(entry.url).key;
    const updated = [...entries.filter(item => authTarget(item.url).key !== target), entry];
    let existing = null;
    try { existing = await readFile(file, 'utf8'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const block = '```json\n' + JSON.stringify(updated, null, 2) + '\n```';
    const content = existing === null ? `# 本机 Git 凭证来源\n\n只记录已明确选择的来源；不含 Token，不代表鉴权已验证。\n\n\`\`\`json\n${JSON.stringify(updated, null, 2)}\n\`\`\`\n` : existing.replace(/^```json\s*\r?\n[\s\S]*?^```[ \t]*$/gm, block);
    await handle.writeFile(content);
    await handle.close(); handle = null;
    await rename(lock, file); acquired = false;
    const actual = await readSources(owner);
    if (!actual.some(item => JSON.stringify(item) === JSON.stringify(entry))) fail('auth_source_readback', '本机来源记录回读不一致。');
    return { status: 'source_saved', file, source: entry, authentication: 'not_performed' };
  } finally { if (handle) await handle.close(); if (acquired) await rm(lock, { force: true }); }
}
