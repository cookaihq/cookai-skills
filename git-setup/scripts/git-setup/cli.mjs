#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';

const skill = fileURLToPath(new URL('../../', import.meta.url));
const pkg = JSON.parse(await readFile(resolve(skill, 'package.json'), 'utf8'));
const minimum = Number(pkg.engines.node.replace('>=', ''));
if (Number(process.versions.node.split('.')[0]) < minimum) {
  console.error(`Node >= ${minimum} required. Ask your Agent to install Node LTS (macOS: brew install node; Windows: winget install OpenJS.NodeJS.LTS).`);
  process.exit(1);
}

const boolean = new Set(['apply', 'use-global-config', 'help', 'github-auth']);
const repeated = new Set(['ref', 'rule-file', 'label', 'assignee']);
const valued = new Set(['project', 'workspace', 'input', 'override', 'project-rule', 'branch', 'author-name', 'author-email', 'name', 'url', 'remote', 'title', 'body-file', 'filename', 'operation-id', 'expected-fingerprint', 'issue-url', 'owner', 'visibility']);
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
  try { return JSON.parse(await readFile(resolve(path), 'utf8')); }
  catch { fail('invalid_input_file', `无法读取有效 JSON 文件：${resolve(path)}（不输出文件内容）。`); }
}
function required(value, name) { if (!value) fail('missing_argument', `缺少 ${name}`); return value; }
function apply(options) { if (!options.apply) fail('apply_required', '写入需要 --apply；Agent 只在本次请求已有具体操作授权时传入，无需重复询问已有授权。'); }
function scrub(text) {
  return text.replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, '$1***@').replace(/\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b/g, '[REDACTED]');
}
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;

async function gitAuth(project, options, loadEnvironment) {
  if (!options['github-auth']) return {};
  const { values } = await loadEnvironment(project, { useGlobalConfig: options['use-global-config'] });
  const repo = required(values.GIT_SETUP_GITHUB_REPOSITORY, 'GIT_SETUP_GITHUB_REPOSITORY');
  if (values.GIT_SETUP_GITHUB_HOST !== 'github.com' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) fail('invalid_auth_binding', 'GitHub 凭证必须绑定 github.com 和完整 owner/repo。');
  const method = required(values.GIT_SETUP_GITHUB_AUTH, 'GIT_SETUP_GITHUB_AUTH');
  if (!['token', 'gh'].includes(method)) fail('invalid_auth_method', 'GitHub 鉴权仅支持 token 或 gh。');
  return { authForUrl: async url => {
    let parsed;
    try { parsed = new URL(url); } catch { return { env: {}, metadata: { method: 'existing' } }; }
    const target = parsed.pathname.replace(/^\//, '').replace(/\.git$/, '');
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'github.com' || target.toLowerCase() !== repo.toLowerCase()) return { env: {}, metadata: { method: 'existing' } };
    if (parsed.username || parsed.password || parsed.port || parsed.search || parsed.hash) fail('invalid_remote_url', 'GitHub HTTPS 目标不能内嵌凭证或附加查询参数。');
    let token = values.GIT_SETUP_GITHUB_TOKEN;
    if (method === 'gh') {
      try { token = execFileSync('gh', ['auth', 'token', '--hostname', 'github.com'], { encoding: 'utf8', timeout: 30000, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, GH_HOST: 'github.com', GH_TOKEN: '', GITHUB_TOKEN: '' } }).trim(); }
      catch { fail('gh_auth_unavailable', '无法读取 gh 的 GitHub 登录凭证；请核对 gh 安装和该主机的登录状态。'); }
    }
    required(token, 'GIT_SETUP_GITHUB_TOKEN');
    const count = Number(process.env.GIT_CONFIG_COUNT || 0);
    if (!Number.isInteger(count) || count < 0 || count > 100) fail('invalid_git_environment', 'GIT_CONFIG_COUNT 无效。');
    return { env: {
      GIT_SETUP_ASKPASS_TOKEN: token,
      GIT_ASKPASS: `${quote(process.execPath.replaceAll('\\', '/'))} ${quote(resolve(skill, 'scripts/git-setup/askpass.mjs').replaceAll('\\', '/'))}`,
      GIT_TERMINAL_PROMPT: '0',
      GIT_CONFIG_COUNT: String(count + 2),
      [`GIT_CONFIG_KEY_${count}`]: 'credential.helper', [`GIT_CONFIG_VALUE_${count}`]: '',
      [`GIT_CONFIG_KEY_${count + 1}`]: 'credential.useHttpPath', [`GIT_CONFIG_VALUE_${count + 1}`]: 'true',
    }, metadata: { method, host: 'github.com', repository: repo } };
  } };
}

const help = `git-setup ${pkg.version} — node <skill>/scripts/git-setup/cli.mjs <command> [options]
所有命令：--project <path>（默认当前目录），--use-global-config（显式启用个人全局业务配置）
inspect                                      读取 Git 状态
audit --ref <full-ref> [--ref ...]             检查所有可达历史对象中的已知秘密模式及大文件
init --branch <name> --author-name <name> --author-email <email> --apply
remote-add --name <remote> --url <url> --apply
publish --remote <name> --ref <full-ref> [--ref ...] --apply [--github-auth]
sync [--apply] [--github-auth]                不带 --apply 仅预检
config-check | config-write --input <json> --apply
auth-info                                    仅显示凭证字段来源和填写状态
issue-resolve [--workspace <outer>] [--override <json>] [--project-rule <json>]
issue-create --title <text> --body-file <path> --operation-id <id> --apply
             [--expected-fingerprint <issue-resolve fingerprint>]
             [--filename <name>] [--label <label>] [--assignee <login>]
issue-recover --operation-id <id>             恢复同一目标上的创建结果
issue-update --issue-url <GitHub Issue URL> [--title <text>] [--body-file <path>] --apply
issue-comment --issue-url <GitHub Issue URL> --body-file <path> --apply
repo-create --owner <login> --name <repo> --visibility private|public --operation-id <id> --apply
install-rules --rule-file <relative-path> [--rule-file ...] [--workspace <outer>] [--apply]
Issue 创建/恢复也接受 issue-resolve 的选择参数。Git 和 Issue 不自动 commit。
`;

async function main() {
  const [command = 'help', ...args] = process.argv.slice(2);
  const options = parse(args);
  if (command === 'help' || options.help) { process.stdout.write(help); return; }
  const project = resolve(options.project || process.cwd());
  const config = await import('./config.mjs');
  const resolutionOptions = {
    workspace: options.workspace && resolve(options.workspace),
    useGlobalConfig: Boolean(options['use-global-config']),
    issueOverride: await json(options.override), projectRuleIssue: await json(options['project-rule']),
  };
  let result;
  if (command === 'config-check') result = await config.loadProject(project);
  else if (command === 'config-write') { apply(options); result = await config.writeProject(project, await json(required(options.input, '--input'))); }
  else if (command === 'auth-info') {
    const { values, sources } = await config.loadEnvironment((await config.inspectProject(project)).root, resolutionOptions);
    result = { sources, present: Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Boolean(value)])) };
  } else if (command === 'issue-resolve') result = await config.resolveIssue(project, resolutionOptions);
  else if (command === 'install-rules') {
    const { installRules } = await import('./integration.mjs');
    result = await installRules(project, { files: options['rule-file'], workspace: options.workspace, apply: options.apply });
  } else if (command === 'audit') {
    const { auditHistory } = await import('./audit.mjs');
    result = auditHistory(project, required(options.ref, '--ref'));
  } else if (['inspect', 'init', 'remote-add', 'publish', 'sync'].includes(command)) {
    const git = await import('./git.mjs');
    const auth = ['publish', 'sync'].includes(command) ? await gitAuth((await config.inspectProject(project)).root, options, config.loadEnvironment) : {};
    if (command === 'inspect') result = await git.inspect(project);
    if (command === 'init') { apply(options); result = await git.init(project, { branch: required(options.branch, '--branch'), authorName: required(options['author-name'], '--author-name'), authorEmail: required(options['author-email'], '--author-email') }); }
    if (command === 'remote-add') { apply(options); result = await git.addRemote(project, { name: required(options.name, '--name'), url: required(options.url, '--url') }); }
    if (command === 'publish') {
      apply(options);
      const refs = required(options.ref, '--ref');
      const { auditHistory } = await import('./audit.mjs');
      const audit = auditHistory(project, refs);
      if (!audit.complete) result = { status: 'audit_blocked', complete: false, audit };
      else result = { ...await git.publish(project, { remote: required(options.remote, '--remote'), refs, expectedRefs: audit.snapshot, ...auth }), audit };
    }
    if (command === 'sync') result = await git.sync(project, (await config.loadProject(project)).config, { dryRun: !options.apply, ...auth });
  } else if (['issue-create', 'issue-recover', 'issue-update', 'issue-comment', 'repo-create'].includes(command)) {
    const io = await import('./issues.mjs');
    const github = await import('./github.mjs');
    const clientFor = async (target, configRoot = project) => {
      const { values } = await config.loadEnvironment((await config.inspectProject(configRoot)).root, resolutionOptions);
      return github.createGitHubClient({ auth: github.authFromEnvironment(values, target), host: target.host });
    };
    const body = options['body-file'] ? await readFile(resolve(options['body-file']), 'utf8') : undefined;
    const operationId = ['issue-create', 'issue-recover', 'repo-create'].includes(command) ? required(options['operation-id'], '--operation-id（本次操作的稳定标识，恢复时复用）') : undefined;
    const content = { title: options.title, body, filename: options.filename, labels: options.label, assignees: options.assignee };
    if (command === 'repo-create') {
      apply(options);
      if (!['private', 'public'].includes(options.visibility)) fail('visibility_required', '明确指定 --visibility private 或 public。');
      const owner = required(options.owner, '--owner'); const name = required(options.name, '--name');
      const client = await clientFor({ host: 'github.com', repository: `${owner}/${name}` });
      result = await io.createRemoteRepository({ projectRoot: project, owner, name, private: options.visibility === 'private', operationId, client, authorized: true });
    } else if (['issue-update', 'issue-comment'].includes(command)) {
      apply(options);
      const url = new URL(required(options['issue-url'], '--issue-url'));
      const match = url.pathname.match(/^\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)\/issues\/([1-9][0-9]*)$/);
      if (url.protocol !== 'https:' || url.host !== 'github.com' || url.username || url.password || !match || url.search || url.hash) fail('unsupported_issue_url', '请提供完整的 https://github.com/owner/repo/issues/number 地址。');
      const target = { host: 'github.com', repository: `${match[1]}/${match[2]}` };
      const resolution = { backend: 'github', initiatingProject: project, configOwner: project, targetProject: null, github: target, local: null, source: { kind: 'explicit-existing-issue', path: url.href }, fingerprint: createHash('sha256').update(url.href).digest('hex') };
      const input = { resolution, number: Number(match[3]), content, body, client: await clientFor(target), authorized: true };
      if (command === 'issue-comment') required(body, '--body-file');
      else if (content.title === undefined && content.body === undefined) fail('empty_update', '更新必须提供标题或正文。');
      result = await (command === 'issue-comment' ? io.commentRemoteIssue(input) : io.updateRemoteIssue(input));
    } else {
      const resolution = await config.resolveIssue(project, resolutionOptions);
      if (command === 'issue-create' && options['expected-fingerprint'] !== undefined
          && options['expected-fingerprint'] !== resolution.fingerprint) {
        fail('configuration_changed', 'Issue 配置与此前展示的目标不一致；请重新解析并核对目标，未创建事项。');
      }
      const client = resolution.backend === 'github' ? await clientFor(resolution.github, resolution.targetProject || resolution.configOwner) : undefined;
      if (command === 'issue-recover') result = await io.recoverIssue({ resolution, operationId: required(options['operation-id'], '--operation-id'), client });
      else {
        apply(options); required(content.title, '--title'); required(options['body-file'], '--body-file');
        const current = await config.resolveIssue(project, resolutionOptions);
        if (current.fingerprint !== resolution.fingerprint) fail('configuration_changed', 'Issue 配置已变化，请重新解析目标。');
        result = await (resolution.backend === 'local' ? io.createLocalIssue : io.createRemoteIssue)({ resolution: current, content, operationId, client, authorized: true });
      }
    }
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
