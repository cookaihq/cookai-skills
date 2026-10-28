import { lstat, realpath } from 'node:fs/promises';
import { resolve, join, relative, isAbsolute, sep } from 'node:path';
import { createHash } from 'node:crypto';

export const digest = value => createHash('sha256').update(typeof value === 'string' || Buffer.isBuffer(value) ? value : JSON.stringify(value)).digest('hex');
export const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };

/** Keep reads and writes within an explicit project root without symlink ancestors. */
export async function safePath(root, path) {
  root = resolve(root); path = resolve(path);
  const rel = relative(root, path);
  if (isAbsolute(rel) || rel === '..' || rel.startsWith('..' + sep)) fail('project_path_invalid', '项目文件必须位于已解析的目标目录内。');
  if ((process.platform === 'win32' ? (await realpath(root)).toLowerCase() !== root.toLowerCase() : await realpath(root) !== root)) fail('project_path_symlink', '项目目录根不得经过符号链接。');
  let cursor = root;
  for (const part of rel.split(/[\\/]/).filter(Boolean)) {
    cursor = join(cursor, part);
    try { if ((await lstat(cursor)).isSymbolicLink()) fail('project_path_symlink', '项目文件路径不得经过符号链接。'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}


const object = value => value && typeof value === 'object' && !Array.isArray(value);
export function fields(value, allowed, label) {
  if (!object(value) || Object.keys(value).some(key => !allowed.includes(key))) fail('input_config_invalid', `${label} 的字段不符合标准；不输出原内容。`);
}
export function portablePath(value) {
  if (typeof value !== 'string' || !value || /[\\\0\r\n]/.test(value) || value.startsWith('/') || /^[A-Za-z]:/.test(value) || value.split('/').some(part => !part || ['.', '..', '.git', '.credentials'].includes(part))) fail('input_path_invalid', '需要项目内的相对文件路径，不得指向 Git 元数据或凭证。');
  return value;
}
export function publicText(value) {
  const raw = JSON.stringify(value).replace(/https:\/\/cnb:<(?:TOKEN|访问令牌)>@cnb\.cool\//g, 'https://cnb.cool/');
  if (/\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b|https?:\/\/[^\s/"\\]*@|-----BEGIN [^-]*PRIVATE KEY-----|[?&](?:access_token|token|password|api_key)=/i.test(raw)) fail('input_secret', '公开配置、文档和计划不得包含凭证。');
}
