import { readFile, lstat, mkdir, open, rename, rm } from 'node:fs/promises';
import { join, relative, dirname } from 'node:path';
import { authTarget, localGit, authFail as fail } from './auth-roots.mjs';
import { safePath, publicText } from './validation.mjs';
import { validateCredentialSource, parseCredentialSources } from './credential-data.mjs';

export const sourceRecordPath = owner => join(owner.main, '.credentials/repositories/sources.local.md');

export function validateSource(entry, owner) {
  publicText(entry);
  return validateCredentialSource(entry, owner);
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
  const entries = parseCredentialSources(text, owner);
  publicText(entries);
  return entries;
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
