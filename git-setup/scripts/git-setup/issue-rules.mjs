import { readFile, lstat, readlink, realpath } from 'node:fs/promises';
import { resolve, join, isAbsolute, dirname } from 'node:path';
import { mainCheckout } from './repository-facts.mjs';
import { resolveRepository } from './repository-relationship.mjs';
import { portablePath, publicText } from './harness-location.mjs';
import { digest, safePath } from './runtime-bundle.mjs';
import { validateGitHubTarget } from './github.mjs';
import { contentFields, validateLocalRules } from './issues.mjs';

const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
export function fields(value, allowed, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !allowed.includes(key))) fail('issue_input_invalid', `${label} 必须是本次完整输入，不能包含未知字段。`);
}
export function text(value, label) {
  if (typeof value !== 'string' || !value.trim()) fail('issue_input_invalid', `${label} 必须明确提供。`);
  return value;
}
async function repository(directory) {
  if (!isAbsolute(text(directory, '仓库检出'))) fail('issue_project_invalid', '本次仓库检出必须使用明确的绝对路径。');
  const actual = await mainCheckout(directory);
  if (!actual.isGit || actual.root !== resolve(directory)) fail('issue_project_invalid', '本次路径必须是已存在的实际 Git 检出根目录。');
  return actual;
}
async function document(owner, input, mapping) {
  fields(input, ['path', 'sha256'], '文档校验依据');
  portablePath(input.path);
  if (!/^[a-f0-9]{64}$/.test(input.sha256)) fail('issue_input_invalid', '文档必须包含 Agent 已读取内容的 SHA-256。');
  const mapped = mapping && input.path.startsWith(mapping.from + '/');
  const root = mapped ? owner.main : owner.root;
  const file = mapped ? join(mapping.target, input.path.slice(mapping.from.length + 1)) : join(root, input.path);
  await safePath(root, file);
  let raw;
  try {
    const stat = await lstat(file);
    if (!stat.isFile() || stat.size > 2 * 1024 * 1024) fail('issue_document_unreadable', '规则或模板必须是可读取的普通文件。');
    raw = await readFile(file);
  } catch (error) {
    if (error.code === 'ENOENT') fail('issue_rules_missing', 'Issue 规则或模板缺失；请先核对项目文档位置。普通 Git 设置不依赖这些文档。');
    if (['EACCES', 'EPERM'].includes(error.code)) fail('issue_document_unreadable', '规则或模板无法读取；修复实际文件访问权限，不切换来源。');
    throw error;
  }
  if (digest(raw) !== input.sha256) fail('issue_document_changed', '规则或模板内容已改变；Agent 需重新读取并解释，旧计划不能继续。');
  return { file, sha256: input.sha256, root };
}
async function documentMapping(owner, input) {
  if (input === undefined) return null;
  fields(input, ['from', 'to', 'declaration'], 'Worktree 文档映射');
  portablePath(input.from);
  if (input.to !== 'docs') fail('issue_mapping_changed', '文档映射必须指向同仓主检出的 docs 实体。');
  const declaration = await document(owner, input.declaration);
  const link = join(owner.root, input.from), target = join(owner.main, input.to);
  await safePath(owner.root, dirname(link)); await safePath(owner.main, target);
  try {
    if (!(await lstat(link)).isSymbolicLink() || await realpath(link) !== target) fail('issue_mapping_changed', 'Worktree 文档链接不再指向已声明的同仓主检出。');
    return { from: input.from, target, link, value: await readlink(link), declaration };
  } catch (error) {
    if (error.code === 'ENOENT') fail('issue_mapping_changed', '文档映射或目标实体缺失；修复原映射，不创建另一份规则。');
    throw error;
  }
}
const identity = value => ({ root: value.root, main: value.main, commonDir: value.commonDir });

/** The Agent interprets Markdown. This module verifies its explicit, one-operation input. */
export async function resolveIssueInput(project, input) {
  fields(input, ['schemaVersion', 'action', 'operationId', 'initiatingProject', 'relationship', 'rules', 'target', 'decision', 'content', 'existing', 'reference'], 'Issue 操作');
  if (input.schemaVersion !== 1 || !['create', 'update', 'comment'].includes(input.action) || !/^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/.test(input.operationId)) fail('issue_input_invalid', '需要 schemaVersion=1、明确动作及稳定的 8–80 位 operationId。');
  const initiating = await repository(input.initiatingProject);
  if (resolve(project) !== initiating.root) fail('issue_project_mismatch', '--project 与本次输入的发起检出不一致。');
  fields(input.relationship, ['workspace', 'kind', 'path', 'harnessDirectory'], '仓库关系');
  await repository(input.relationship.workspace);
  const relationship = await resolveRepository(initiating.root, { workspace: input.relationship.workspace, harnessDirectory: input.relationship.harnessDirectory });
  if (relationship.registration.kind !== input.relationship.kind || relationship.registration.path !== input.relationship.path) fail('issue_relationship_changed', '本次声明的仓库关系与实际登记不符。');
  fields(input.target, ['scope', 'project', 'backend', 'local', 'github'], '完整目标');
  const target = await repository(input.target.project);
  if (!['current', 'outer', 'specified'].includes(input.target.scope)) fail('issue_input_invalid', '目标范围必须是 current、outer 或 specified。');
  const expected = input.target.scope === 'current' ? initiating : relationship.management;
  if (input.target.scope === 'outer' && relationship.registration.kind === 'self') fail('issue_outer_missing', '没有已验证的外仓关系，不能把当前仓当成外仓。');
  if (input.target.scope !== 'specified' && target.commonDir !== expected.commonDir) fail('issue_target_mismatch', '目标检出不属于本次明确选择的仓库。');
  fields(input.rules, ['project', 'path', 'sha256', 'references', 'initialization', 'mapping'], '规则来源');
  const owner = await repository(input.rules.project);
  if (![initiating, relationship.management, target].some(item => item.commonDir === owner.commonDir)) fail('issue_rules_owner', '规则来源必须属于发起仓、已验证管理仓或明确目标仓。');
  const mapping = await documentMapping(owner, input.rules.mapping);
  if (input.rules.path !== 'docs/agents/issue-tracker.md' && input.rules.path !== (mapping?.from + '/agents/issue-tracker.md')) fail('issue_rules_path', '读取 docs/agents/issue-tracker.md；Worktree 的其他入口必须有可验证的映射声明。');
  if (!Array.isArray(input.rules.references)) fail('issue_input_invalid', '必须明确列出已读取的规则引用。');
  fields(input.rules.initialization, ['mode', 'evidence'], '缺失规则的处理约束');
  const mode = input.rules.initialization.mode;
  if (!['initialize-if-missing', 'repair-existing'].includes(mode)) fail('issue_input_invalid', '必须明确缺规则时是首次初始化还是修复已有文档。');
  const initialization = { mode };
  if (mode === 'repair-existing') initialization.evidence = await document(owner, input.rules.initialization.evidence, mapping);
  else if (input.rules.initialization.evidence !== undefined) fail('issue_input_invalid', '首次初始化指引不能同时携带禁止重跑的声明。');
  let rules;
  try { rules = await document(owner, { path: input.rules.path, sha256: input.rules.sha256 }, mapping); }
  catch (error) {
    if (error.code === 'issue_rules_missing') error.message = mode === 'repair-existing'
      ? 'Issue 规则实体缺失；本次已声明禁止重新初始化，请修复现有 docs/agents/issue-tracker.md 或原 Worktree 文档映射。普通 Git 设置可继续。'
      : '确认是真正缺少 Issue 规则后，可使用 /setup-matt-pocock-skills 初始化；脚本不会自动执行。普通 Git 设置可继续。';
    throw error;
  }
  rules.mapping = mapping;
  rules.initialization = initialization;
  rules.references = [];
  for (const reference of input.rules.references) rules.references.push(await document(owner, reference, mapping));
  fields(input.decision, ['basis', 'override'], '本次决定');
  text(input.decision.basis, '规则解释依据');
  if (input.decision.override !== null) {
    fields(input.decision.override, ['reason', 'request'], '本次覆盖');
    text(input.decision.override.reason, '覆盖依据'); text(input.decision.override.request, '用户本次请求');
  }
  const result = { backend: input.target.backend, initiatingProject: initiating.root, configOwner: owner.root, targetProject: target.root, rules, relationship, decision: input.decision, action: input.action, operationId: input.operationId };
  if (result.backend === 'local') {
    if (input.target.github !== undefined) fail('issue_input_invalid', '本地目标不能同时携带 GitHub 设置。');
    const local = input.target.local;
    fields(local, ['directory', 'filename', 'numbering', 'template', 'commentsHeading'], '本地目标');
    portablePath(local.directory); await safePath(target.root, join(target.root, local.directory));
    text(local.filename, '文件命名规则');
    if (local.numbering !== undefined) fields(local.numbering, ['start', 'width'], '编号规则');
    validateLocalRules(local);
    if (local.commentsHeading !== undefined && (typeof local.commentsHeading !== 'string' || !/^#{1,6} [^\r\n]+$/.test(local.commentsHeading))) fail('invalid-comments-heading', '回复标题必须是规则中的完整 Markdown 标题行。');
    result.local = { ...local, root: target.root };
    if (local.template !== undefined) {
      const template = await document(owner, local.template, mapping);
      Object.assign(result.local, { template: template.file, templateRoot: template.root, templateFingerprint: template.sha256 });
      result.rules.template = template;
    }
  } else if (result.backend === 'github') {
    if (input.target.local !== undefined) fail('issue_input_invalid', 'GitHub 目标不能同时携带本地设置。');
    fields(input.target.github, ['host', 'repository'], 'GitHub 目标');
    const github = validateGitHubTarget(input.target.github);
    result.github = { host: github.host, repository: github.repository };
  } else fail('issue_backend_unsupported', 'Issue 仅支持本地文件或 github.com，不切换记录方式。');
  if (input.reference !== undefined) {
    if (input.action !== 'create') fail('issue_input_invalid', 'reference 只用于参考新建；更新和回复必须沿用 existing。');
    text(input.reference, '参考来源');
  }
  const content = input.content;
  if (input.action === 'comment') {
    fields(content, ['body'], '回复内容'); text(content.body, '回复正文');
  } else if (input.action === 'update' && result.backend === 'local') {
    fields(content, ['document'], '本地完整更新'); text(content.document, '更新后完整文档');
  } else if (input.action === 'create' && result.backend === 'local' && content?.document !== undefined) {
    fields(content, ['title', 'document'], '本地完整创建');
    contentFields({ title: content.title, body: '' }); text(content.document, '创建的完整文档');
  } else {
    fields(content, result.backend === 'github' ? ['title', 'body', 'labels', 'assignees', 'state'] : ['title', 'body'], '事项内容');
    if (!Object.keys(contentFields(content, input.action === 'update')).length) fail('empty-update', '必须提供本次要更新的内容。');
    if (result.backend === 'local') {
      const template = result.local.template && await readFile(result.local.template, 'utf8');
      if (!template?.includes('{title}') || !template.includes('{body}')) fail('invalid-template', '正文渲染需要含 {title} 和 {body} 的已有模板；否则由 Agent 按规则提交完整 document，不改写项目模板。');
    }
  }
  if (input.action === 'create') {
    if (input.existing !== undefined) fail('issue_input_invalid', '参考新建使用 reference，不能将 existing 当作新建目标。');
  } else if (result.backend === 'local') {
    fields(input.existing, ['path', 'sha256'], '原本地事项');
    portablePath(input.existing.path);
    if (!input.existing.path.startsWith(input.target.local.directory + '/') || !input.existing.path.endsWith('.md') || !/^[a-f0-9]{64}$/.test(input.existing.sha256)) fail('issue_existing_invalid', '原事项必须位于本次目录中并提供已读取内容的 SHA-256。');
    await safePath(target.root, join(target.root, input.existing.path));
    result.existing = { file: join(target.root, input.existing.path), sha256: input.existing.sha256 };
  } else {
    fields(input.existing, ['url'], '原 GitHub 事项');
    const prefix = `https://github.com/${result.github.repository}/issues/`;
    const url = input.existing.url;
    if (typeof url !== 'string' || !url.toLowerCase().startsWith(prefix.toLowerCase()) || !/^[1-9][0-9]*$/.test(url.slice(prefix.length)) || !Number.isSafeInteger(Number(url.slice(prefix.length)))) fail('issue_existing_invalid', '必须提供属于本次明确目标仓库的完整原 Issue URL。');
    result.existing = { url, number: Number(url.slice(prefix.length)) };
  }
  publicText({ ...result, relationship: undefined });
  result.fingerprint = digest({ input, initiating: identity(initiating), owner: identity(owner), target: identity(target), relationship: relationship.fingerprint, rules });
  return result;
}
