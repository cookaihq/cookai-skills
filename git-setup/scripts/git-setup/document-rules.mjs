import { readFile, lstat, realpath, readlink } from 'node:fs/promises';
import { join, relative, dirname } from 'node:path';
import { safePath, digest, fail, portablePath } from './validation.mjs';

const start = '<!-- git-setup:documentation:start -->';
const end = '<!-- git-setup:documentation:end -->';
export function withoutRegistration(content) {
  const first = content.indexOf(start), last = content.indexOf(end);
  if (first === -1 && last === -1) return content;
  if (first < 0 || last < first || content.indexOf(start, first + start.length) !== -1 || content.indexOf(end, last + end.length) !== -1) fail('project_rules_invalid', '项目规则入口登记标记不完整或重复；先核对原文。');
  return content.slice(0, first) + content.slice(last + end.length);
}

// Resolve only explicitly supplied project rule files. Preserve the link itself.
export async function ruleSnapshot(root, name) {
  portablePath(name);
  const path = join(root, name);
  await safePath(root, dirname(path));
  let info;
  try { info = await lstat(path); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const target = info ? await realpath(path) : path;
  await safePath(root, target);
  const stat = info ? await lstat(target) : null;
  if (stat && (!stat.isFile() || stat.nlink !== 1 || stat.size > 262144)) fail('project_rules_invalid', '规则必须是项目内的普通小文件，或指向它的符号链接。');
  const content = stat ? await readFile(target, 'utf8') : '';
  return { path: name, entity: relative(root, target).replaceAll('\\', '/'), link: info?.isSymbolicLink() ? await readlink(path) : null,
    before: stat ? digest(content) : null, content, mode: stat ? stat.mode & 0o777 : 0o644 };
}

export async function registrationEdits(root, ruleFiles, documentPath) {
  const entities = new Map();
  for (const name of ruleFiles) {
    const snapshot = await ruleSnapshot(root, name);
    if (snapshot.entity === documentPath || snapshot.entity.startsWith(`${documentPath}/`)) fail('project_rules_invalid', '规则文件和 Git 使用说明必须是不同文件。');
    const existing = entities.get(snapshot.entity);
    if (existing) { existing.aliases.push({ ...snapshot, content: undefined }); continue; }
    const link = relative(dirname(join(root, snapshot.entity)), join(root, documentPath)).replaceAll('\\', '/');
    const block = `${start}\n执行本项目 Git 操作前，必须阅读并遵循[项目 Git 使用说明](<${link}>)。沿用其中明确的仓库、凭证来源及操作边界。\n${end}`;
    withoutRegistration(snapshot.content);
    const first = snapshot.content.indexOf(start), last = snapshot.content.indexOf(end);
    const content = first < 0 ? `${snapshot.content}${snapshot.content.endsWith('\n') || !snapshot.content ? '' : '\n'}\n${block}\n`
      : snapshot.content.slice(0, first) + block + snapshot.content.slice(last + end.length);
    entities.set(snapshot.entity, { path: snapshot.entity, before: snapshot.before, after: digest(content), content, mode: snapshot.mode, aliases: [{ ...snapshot, content: undefined }] });
  }
  return [...entities.values()];
}

export async function verifyEdit(root, edit, applied = false) {
  for (const alias of edit.aliases || [{ path: edit.path, entity: edit.path, link: null, before: edit.before }]) {
    const current = await ruleSnapshot(root, alias.path);
    if (current.entity !== alias.entity || current.link !== alias.link || current.before !== (applied ? edit.after : alias.before)) fail('project_plan_changed', '入口原文或规则符号链接实体发生变化；保留现场，重新计划。');
  }
}
