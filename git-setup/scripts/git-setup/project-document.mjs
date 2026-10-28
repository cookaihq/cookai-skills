import { readFile, lstat, mkdir, open } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { inspectProject } from './repository-facts.mjs';
import { digest, safePath, portablePath, publicText, fields, fail } from './validation.mjs';
import { registrationEdits, verifyEdit } from './document-rules.mjs';

export async function planDocument(project, input) {
  fields(input, ['path', 'content', 'ruleFiles'], '项目 Git 说明');
  const state = await inspectProject(project);
  const root = state.root || state.requestedPath;
  if (root !== state.requestedPath) fail('document_root_required', '请明确项目 Git 根，不从普通子目录修改父仓规则。');
  const path = portablePath(input.path || 'docs/agent/git.md');
  if (typeof input.content !== 'string' || !input.content.trim() || Buffer.byteLength(input.content) > 262144
      || !Array.isArray(input.ruleFiles) || !input.ruleFiles.length) fail('document_input', '需要完整 Git 使用说明与实际生效的规则入口列表。');
  publicText(input.content);
  await safePath(root, join(root, path));
  let before = null;
  try {
    const info = await lstat(join(root, path));
    if (!info.isFile() || info.nlink !== 1 || info.size > 262144) fail('document_input', '已有说明必须是普通小文件。');
    before = digest(await readFile(join(root, path)));
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const edits = await registrationEdits(root, input.ruleFiles, path);
  const document = { path, before, after: digest(input.content), content: input.content };
  return { status: 'planned', root, document, edits, fingerprint: digest({ root, document, edits }), runtimeInstalled: false };
}

export async function writeDocument(project, input, options = {}) {
  const plan = await planDocument(project, input);
  if (!options.apply) return plan;
  if (options.expectedFingerprint !== plan.fingerprint) fail('document_changed', '说明、规则或计划已变化，先重新核对。');
  const files = [plan.document, ...plan.edits], completed = [];
  try {
    for (const edit of plan.edits) await verifyEdit(plan.root, edit);
    for (const item of files) {
      const file = join(plan.root, item.path);
      await safePath(plan.root, file);
      await mkdir(dirname(file), { recursive: true });
      const stat = item.before === null ? null : await lstat(file);
      const handle = await open(file, item.before === null ? 'wx' : 'r+');
      try {
        const opened = await handle.stat();
        if (!opened.isFile() || opened.nlink !== 1 || (stat && (opened.dev !== stat.dev || opened.ino !== stat.ino))) fail('document_changed', '写入前文件实体变化。');
        if (item.before !== null && digest(await handle.readFile()) !== item.before) fail('document_changed', '写入前文件发生变化，保留后续修改。');
        const data = Buffer.from(item.content);
        await handle.write(data, 0, data.length, 0);
        await handle.truncate(data.length);
      } finally { await handle.close(); }
      if (digest(await readFile(file)) !== item.after) fail('document_readback', '文件回读与预期不一致。');
      completed.push(item.path);
    }
    return { status: 'documented', complete: true, files: completed, runtimeInstalled: false };
  } catch (error) {
    return { status: 'incomplete', complete: false, files: completed, code: error.code || 'document_write_failed', message: '说明或规则写入未完成；保留现存内容，核对后继续。' };
  }
}
