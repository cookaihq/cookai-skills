import { readFile, realpath, mkdir, writeFile, rename, lstat } from 'node:fs/promises';
import { resolve, relative, dirname, isAbsolute, sep } from 'node:path';
import { randomUUID } from 'node:crypto';

const start = '<!-- git-setup:issue-routing:start -->';
const end = '<!-- git-setup:issue-routing:end -->';
const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
const within = (root, path) => { const part = relative(root, path); return part === '' || (!part.startsWith(`..${sep}`) && part !== '..' && !isAbsolute(part)); };

/** Only the rule files selected after the Agent inspected this project's host rules. */
export async function installRules(project, { files, workspace, apply = false } = {}) {
  const root = await realpath(project);
  if (!files?.length) fail('rule_files_required', '先核对各宿主实际加载的项目规则文件，再通过 --rule-file 显式指定。');
  const workspaceArg = workspace ? relative(root, await realpath(workspace)).split(sep).join('/') || '.' : null;
  const block = `${start}\n用户要求新建 Issue 或记录问题时，先读取本项目 .git-setup/issue-routing.md，调用已安装的 git-setup Skill 解析记录方式和目标，再让实际创建工具消费解析结果。明确更新或回复已有事项时沿用原事项；参考旧事项新建时仍执行新建。\n${end}`;
  const guide = `# 本项目的 Issue 创建入口\n\n在当前宿主中加载 git-setup Skill，并读取其 SKILL.md。发起仓库为当前用户指定目录对应的实际 Git 仓库。先运行该 Skill 的 issue-resolve，再按同一结果调用 issue-create；不能改用当前 remote 或活动账号重新选择目标。\n\n${workspaceArg === null ? '外仓关系由 Git 子模块事实或明确的仓库登记确定。' : `本项目已明确登记的外仓位置，以本规则所在项目为路径基准：\`${workspaceArg}\`。把该路径解析为绝对路径后传给 --workspace。`}\n\n已有本地事项规则优先保留，必要时由 Agent 将记录方式、目录、文件名、模板、编号规则组成完整配置；没有足够依据时询问，不猜测 GitHub 或本地。子仓明确登记的独立配置优先于外仓默认，损坏时不回退。本地成功后明确回复“已按当前项目配置将 Issue 保存到本地”并给实际文件路径。\n\n该入口不授权改默认配置、提交或推送。只支持本地事项和 GitHub.com 在线 Issue；实际用户请求已有明确授权时不重复询问同一操作。保存完整 operation-id，结果不明时调用 issue-recover，不重复创建。\n`;
  const writes = new Map();
  for (const name of files) {
    const path = resolve(root, name);
    if (!within(root, path) || path === root) fail('invalid_rule_path', '规则文件必须位于当前项目内。');
    let actual = path; let text = '';
    try {
      actual = await realpath(path);
      if (!within(root, actual)) fail('external_rule_symlink', '规则符号链接指向项目外；请在该规则所属项目执行接入。');
      if (!(await lstat(actual)).isFile()) fail('invalid_rule_path', '规则目标必须是普通文件。');
      text = await readFile(actual, 'utf8');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    // Canonicalize existing ancestors before creating a nested path.
    let ancestor = dirname(actual);
    while (true) {
      try { if (!within(root, await realpath(ancestor))) fail('external_rule_symlink', '规则父目录不能指向项目外。'); break; }
      catch (error) { if (error.code !== 'ENOENT') throw error; ancestor = dirname(ancestor); }
    }
    const a = text.indexOf(start); const b = text.indexOf(end);
    if ((a < 0) !== (b < 0) || (a >= 0 && (b < a || text.indexOf(start, a + start.length) >= 0 || text.indexOf(end, b + end.length) >= 0))) fail('rule_marker_conflict', '发现不完整或重复的 git-setup 规则标记，保留原文件。');
    writes.set(actual, a >= 0 ? text.slice(0, a) + block + text.slice(b + end.length) : `${text}${text.endsWith('\n') || !text ? '' : '\n'}\n${block}\n`);
  }
  let guidePath = resolve(root, '.git-setup/issue-routing.md');
  try {
    const existing = await realpath(dirname(guidePath));
    if (!within(root, existing)) fail('external_rule_symlink', '配置目录不能指向项目外。');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  try {
    const info = await lstat(guidePath);
    const actual = await realpath(guidePath);
    if (!within(root, actual)) fail('external_rule_symlink', 'Issue 接入说明指向项目外，保留原符号链接。');
    if (!(await lstat(actual)).isFile()) fail('invalid_rule_path', 'Issue 接入说明必须指向普通文件。');
    if (info.isSymbolicLink()) guidePath = actual;
    const old = await readFile(guidePath, 'utf8');
    if (old !== guide) fail('existing_routing_guide', '已有 Issue 接入说明与本次内容不同，先审查原文件再决定如何修改。');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  writes.set(guidePath, guide);
  if (apply) for (const [path, content] of writes) {
    await mkdir(dirname(path), { recursive: true });
    // A sibling temporary file keeps rename atomic on the same filesystem.
    const staging = `${path}.git-setup-${randomUUID()}`;
    await writeFile(staging, content, { flag: 'wx', mode: 0o600 });
    await rename(staging, path);
  }
  return { status: apply ? 'written' : 'planned', files: [...writes.keys()], clientVerification: 'required' };
}
