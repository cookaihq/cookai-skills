#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const skill = fileURLToPath(new URL('../../', import.meta.url));
const pkg = JSON.parse(await readFile(resolve(skill, 'package.json'), 'utf8'));
const minimum = Number(pkg.engines.node.replace('>=', ''));
if (Number(process.versions.node.split('.')[0]) < minimum) {
  console.error(`Node >= ${minimum} required. Ask your Agent to install Node LTS (macOS: brew install node; Windows: winget install OpenJS.NodeJS.LTS).`);
  process.exit(1);
}

const boolean = new Set(['apply', 'use-global-config', 'help', 'accept-effects', 'allow-ignore', 'api-only']);
const repeated = new Set(['ref']);
const valued = new Set(['project', 'repository', 'checkout', 'policy-directory', 'workspace', 'input', 'branch', 'author-name', 'author-email', 'name', 'url', 'remote', 'operation-id', 'expected-fingerprint', 'owner', 'visibility', 'action', 'plan-file', 'auth-method', 'gh-user', 'credential-file']);
const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
function parse(args) {
  const options = {};
  for (let i = 0; i < args.length; i++) {
    const key = args[i].replace(/^--/, '');
    if (!args[i].startsWith('--') || (!boolean.has(key) && !valued.has(key) && !repeated.has(key))) fail('invalid_argument', `未知参数：${args[i]}`);
    if (Object.hasOwn(options, key) && !repeated.has(key)) fail('duplicate_argument', `参数重复：--${key}`);
    if (boolean.has(key)) options[key] = true;
    else {
      const value = args[++i];
      if (!value || value.startsWith('--')) fail('missing_argument', `参数缺值：--${key}`);
      if (repeated.has(key)) (options[key] ||= []).push(value); else options[key] = value;
    }
  }
  return options;
}
async function json(path) {
  if (!path) return undefined;
  try {
    if (path !== '-') return JSON.parse(await readFile(resolve(path), 'utf8'));
    if (process.stdin.isTTY) throw new Error('stdin required');
    const chunks = [];
    let size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > 8 * 1024 * 1024) throw new Error('input too large');
      chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }
  catch { fail('invalid_input_file', `无法读取有效 JSON：${path === '-' ? '标准输入（上限 8 MiB）' : resolve(path)}（不输出输入内容）。`); }
}
function required(value, name) { if (!value) fail('missing_argument', `缺少 ${name}`); return value; }
function apply(options) { if (!options.apply) fail('apply_required', '写入需要 --apply；Agent 只在本次请求已有具体操作授权时传入，无需重复询问已有授权。'); }
function scrub(text) {
  return text.replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1***@').replace(/\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b/g, '[REDACTED]');
}
const help = `git-setup ${pkg.version} — node <skill>/scripts/git-setup/cli.mjs <command> [options]
所有命令：--project <path>（默认当前目录）；--input - 接收本次 JSON（不含凭证）。
check | inspect                             只读现状，Agent 另读项目规则
setup --url <url>                            接入计划；apply + expected-fingerprint 执行
     [--action add|replace --name <remote>] [--branch <name>]
     [--author-name <name> --author-email <email>] [--accept-effects] [--plan-file <ignored-json>]
document-plan | document-write --input -     {path,content,ruleFiles}；write 需 apply + expected-fingerprint
credential-info --url <url>                  无值来源报告
credential-prepare --url <url>               准备被忽略的空文件；写入需 apply [--allow-ignore]
credential-check --url <url> [--api-only]     当前值验证，不安装 helper
source-save --input - [--apply --allow-ignore]  记住所选非敏感 {url,method,file?,account?}
GitHub 默认复用项目目标 Token；缺失或验证失败不回退全局 gh，同目标已确认来源可复用。
鉴权选项：--auth-method github-token|gh|cnb-url|environment，--credential-file <path>。
仅用户明确选择全局 gh 时传 --auth-method gh --gh-user <login>；活动账号不构成选择。
管理关系：--workspace <root> [--policy-directory <明确项目策略目录>]
check/read/publish/sync 支持 --repository <登记路径或 .> [--checkout <该仓检出>]
read --remote <name>                         读取指定远端
publish --remote <name> --ref <完整ref>       预览；apply + expected-fingerprint 执行
sync --input -                              {sync:{source,targets,refs},sources?:[...]}；或明确项目策略
操作 --input 还可提供 sources:[{url,method,file?,account?}]，每个目标独立选来源。
audit --ref <完整ref> [--ref ...]             可达历史检查
init --branch <name> --author-name <name> --author-email <email> --apply
remote-add --name <remote> --url <url> --apply
issue-resolve | issue-create | issue-update | issue-comment | issue-recover --input -
repo-create --owner <login> --name <repo> --visibility private|public --operation-id <id> --apply
--use-global-config 显式启用个人全局环境配置，仅 auth-method=environment 消费业务 Token。
Git 操作不自动 commit。普通 Git/IDE/CI 不会自动读取自定义凭证目录。
`;

async function main() {
  const [command = 'check', ...args] = process.argv.slice(2);
  const options = parse(args);
  if (command === 'help' || options.help) { process.stdout.write(help); return; }
  const project = resolve(options.project || process.cwd());
  if ((options.repository !== undefined || options.checkout !== undefined) && !['check', 'read', 'publish', 'sync', 'credential-info', 'credential-prepare', 'credential-check', 'source-save'].includes(command)) fail('invalid_argument', 'repository/checkout 只用于项目 的 check/read/publish/sync。');
  if (options.checkout !== undefined && options.repository === undefined) fail('missing_argument', 'checkout 必须与明确的 repository 一起提供。');
  if (options.repository !== undefined && [options.workspace].some(value => value !== undefined && resolve(value) !== project)) fail('WORKSPACE_CONFLICT', '选仓操作的 project 是 管理仓；其他管理仓参数不能重定向该入口。');
  const input = options.input ? await json(options.input) : undefined;
  const operationOptions = { directory: options['policy-directory'], repository: options.repository, checkout: options.checkout, remote: options.remote, refs: options.ref || [], apply: Boolean(options.apply), 'expected-fingerprint': options['expected-fingerprint'], 'operation-id': options['operation-id'] };
  const config = await import('./config.mjs');
  const authModule = await import('./auth.mjs');
  const authenticationOptions = { workspace: options.workspace && resolve(options.workspace), policyDirectory: options['policy-directory'], authMethod: options['auth-method'], ghUser: options['gh-user'], credentialFile: options['credential-file'], useGlobalConfig: Boolean(options['use-global-config']) };
  authenticationOptions.sources = input?.sources;
  Object.assign(operationOptions, authenticationOptions, { sync: input?.sync });
  let authProject = project;
  if (options.repository !== undefined && ['credential-info', 'credential-prepare', 'credential-check', 'source-save'].includes(command)) {
    const { selectRepository } = await import('./repository-relationship.mjs');
    authProject = (await selectRepository(project, { repository: options.repository, checkout: options.checkout, policyDirectory: options['policy-directory'] })).project.root;
    authenticationOptions.workspace = project;
  }
  let result;
  if (command === 'read' || (command === 'check' && options.repository !== undefined)) {
    const { projectOperation } = await import('./project-operations.mjs');
    result = await projectOperation(project, command, operationOptions);
  }
  else if (command === 'check') {
    const { entryCheck } = await import('./entry-check.mjs');
    result = await entryCheck(project, { useGlobalConfig: Boolean(options['use-global-config']), directory: options['policy-directory'], ...authenticationOptions });
  }
  else if (['document-plan', 'document-write'].includes(command)) {
    const { planDocument, writeDocument } = await import('./project-document.mjs');
    required(input, '--input');
    if (command === 'document-write') { apply(options); result = await writeDocument(project, input, { apply: true, expectedFingerprint: options['expected-fingerprint'] }); }
    else result = await planDocument(project, input);
  } else if (command === 'setup') {
    const { setup } = await import('./setup.mjs');
    result = await setup(project, { url: required(options.url, '--url'), action: options.action, name: options.name, apply: options.apply, branch: options.branch, authorName: options['author-name'], authorEmail: options['author-email'], expectedFingerprint: options['expected-fingerprint'], acceptEffects: options['accept-effects'], planFile: options['plan-file'], ...authenticationOptions });
  } else if (['credential-info', 'credential-prepare', 'credential-check', 'source-save'].includes(command)) {
    if (command === 'source-save') result = await authModule.rememberSource(authProject, required(input, '--input'), { ...authenticationOptions, apply: Boolean(options.apply), allowIgnore: Boolean(options['allow-ignore']) });
    else {
      const url = required(options.url, '--url');
      if (command === 'credential-info') result = await authModule.inspectAuth(authProject, url, authenticationOptions);
      if (command === 'credential-prepare') result = await authModule.prepareAuth(authProject, url, { ...authenticationOptions, apply: Boolean(options.apply), allowIgnore: Boolean(options['allow-ignore']) });
      if (command === 'credential-check') result = await authModule.verifyAuth(authProject, url, { ...authenticationOptions, apiOnly: Boolean(options['api-only']) });
    }
  } else if (['issue-resolve', 'issue-create', 'issue-update', 'issue-comment', 'issue-recover'].includes(command)) {
    const { issueOperation } = await import('./issue-operation.mjs');
    result = await issueOperation(project, command, required(input, '--input'), { ...authenticationOptions, apply: Boolean(options.apply), expectedFingerprint: options['expected-fingerprint'] });
  }
  else if (command === 'audit') {
    const { auditHistory } = await import('./audit.mjs');
    result = auditHistory(project, required(options.ref, '--ref'));
  } else if (['inspect', 'init', 'remote-add', 'publish', 'sync'].includes(command)) {
    const git = await import('./git.mjs');
    if (command === 'inspect') result = await git.inspect(project);
    if (command === 'init') { apply(options); result = await git.init(project, { branch: required(options.branch, '--branch'), authorName: required(options['author-name'], '--author-name'), authorEmail: required(options['author-email'], '--author-email') }); }
    if (command === 'remote-add') { apply(options); result = await git.addRemote(project, { name: required(options.name, '--name'), url: required(options.url, '--url') }); }
    if (command === 'publish' || command === 'sync') {
      const { projectOperation } = await import('./project-operations.mjs');
      result = await projectOperation(project, command, operationOptions);
    }
  } else if (command === 'repo-create') {
    apply(options);
    if (!['private', 'public'].includes(options.visibility)) fail('visibility_required', '明确指定 --visibility private 或 public。');
    const { createRemoteRepository } = await import('./issues.mjs');
    const owner = required(options.owner, '--owner'), name = required(options.name, '--name');
    const client = await authModule.selectedGitHubClient((await config.inspectProject(project)).root, { host: 'github.com', repository: `${owner}/${name}` }, authenticationOptions);
    result = await createRemoteRepository({ projectRoot: project, owner, name, private: options.visibility === 'private', operationId: required(options['operation-id'], '--operation-id'), client, authorized: true });
  } else fail('unknown_command', `未知命令：${command}`);
  process.stdout.write(`${scrub(JSON.stringify(result, null, 2))}\n`);
  const planned = result?.dryRun === true && result.status === 'planned' && result.sourceStable === true
    && result.results?.every(item => ['planned', 'verified', 'needs_history_fetch'].includes(item.status));
  if ((!planned && result?.complete === false) || ['ambiguous', 'uncertain', 'failed', 'conflict', 'multiple-matches'].includes(result?.status)) process.exitCode = 2;
}

try { await main(); }
catch (error) {
  const ambiguous = error.code === 'ambiguous' || error.ambiguous === true;
  process.stderr.write(`${scrub(JSON.stringify({ status: ambiguous ? 'ambiguous' : 'error', code: error.code || 'unexpected_error', message: error.message, operationId: error.operationId, filePath: error.filePath }))}\n`);
  process.exitCode = ambiguous ? 2 : 1;
}
