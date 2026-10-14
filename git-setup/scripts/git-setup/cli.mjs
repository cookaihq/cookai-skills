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

const boolean = new Set(['apply', 'use-global-config', 'help', 'github-auth', 'accept-effects', 'credentials-filled', 'allow-ignore', 'replace-auth', 'api-only']);
const repeated = new Set(['ref']);
const valued = new Set(['project', 'repository', 'checkout', 'harness-directory', 'workspace', 'input', 'branch', 'author-name', 'author-email', 'name', 'url', 'remote', 'operation-id', 'expected-fingerprint', 'owner', 'visibility', 'action', 'plan-file', 'auth-method', 'gh-user', 'credential-workspace', 'credential-file']);
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
const help = `git-setup ${pkg.version} — node <skill>/scripts/git-setup/cli.mjs <command> [options]
所有命令：--project <path>（默认当前目录），--use-global-config（显式启用个人全局业务配置）
inspect                                      读取 Git 状态
check                                        只读事实检查（无参数默认）；Agent 另读实际宿主规则
      [--harness-directory <项目内实际目录>]  没有本机位置记录时显式提供，不猜测目录
harness-plan --input <含 choice/directory/ruleFiles/比较的 JSON> [--plan-file <ignored-json>]
harness-apply --input <同一选择.json> --plan-file <ignored-json> --expected-fingerprint <指纹> --apply
harness-recover --plan-file <原记录> [--apply --expected-fingerprint <恢复检查指纹>]
setup --url <url>                            检查当前项目并生成接入计划，不提交或推送
      [--action add|replace --name <remote>] [--branch <name>]
      [--author-name <name> --author-email <email>] [--accept-effects]
      [--plan-file <ignored-json>] [--workspace <outer>] [--github-auth]
      [--apply --expected-fingerprint <plan fingerprint>]
audit --ref <full-ref> [--ref ...]             检查所有可达历史对象中的已知秘密模式及大文件
init --branch <name> --author-name <name> --author-email <email> --apply
remote-add --name <remote> --url <url> --apply
publish --remote <name> --ref <full-ref> [--ref ...] --apply [--github-auth]
sync [--apply --expected-fingerprint <指纹>]  只读已登记 Harness 同步策略，无旧 JSON 回退
read --remote <name>                       通过已登记 Harness 读取选中目标
check/read/publish/sync 可用 --repository <登记路径或 .> [--checkout <该仓检出>]。
--project 是 Harness 管理仓；各仓使用独立策略，错误选择不回退外仓。
可用 --harness-directory <项目内实际目录>；项目日常入口不执行 Skill 完整比较
auth-info [--url <url>]                      显示来源及选择/填写缺项，不输出凭证
auth-prepare --url <url> --auth-method gh|github-token|cnb-url [--gh-user <login>]
             [--credential-workspace <outer>] [--credential-file <existing-file>]
             [--allow-ignore] [--replace-auth] [--apply]
auth-verify --url <url> [--credentials-filled] [--api-only] [--apply --expected-fingerprint <fingerprint>]
setup 另接受 --auth-method、--gh-user、--credential-workspace、--credential-file、--credentials-filled。
--workspace 与 --credential-workspace 指定同一管理关系；同时提供时必须一致。
管理关系核对 Git 子模块登记及元数据，或实际 Harness 目录内的 repos.conf；不读取旧 JSON 登记。
--harness-directory 指管理仓内实际位置；已保存的鉴权选择仍须通过当前登记核验。
issue-resolve --input <本次完整输入.json>      验证规则实体、关系、目标、模板及所选鉴权
issue-create|issue-update|issue-comment --input <同一输入.json>
             [--apply --expected-fingerprint <本次计划指纹>]
issue-recover --input <原输入.json>           回读同一操作结果，不重复创建或回复
repo-create --owner <login> --name <repo> --visibility private|public --operation-id <id> --apply
Issue 输入契约见 references/configuration.md；Agent 解释现有规则，CLI 不解析 Markdown 语义。
旧 config-check/config-write/install-rules 已退出，不读取或重建旧项目 JSON/routing。
Git 和 Issue 不自动 commit。
`;

async function main() {
  const [command = 'check', ...args] = process.argv.slice(2);
  const options = parse(args);
  if (command === 'help' || options.help) { process.stdout.write(help); return; }
  if (['config-check', 'config-write', 'install-rules'].includes(command)) fail('legacy_command_removed', '旧项目配置和 Issue routing 已退出；Issue 使用现有规则及本次 --input，真实 Harness 入口通过 harness-plan/apply 登记。');
  const project = resolve(options.project || process.cwd());
  if ((options.repository !== undefined || options.checkout !== undefined) && !['check', 'read', 'publish', 'sync'].includes(command)) fail('invalid_argument', 'repository/checkout 只用于项目 Harness 的 check/read/publish/sync。');
  if (options.checkout !== undefined && options.repository === undefined) fail('missing_argument', 'checkout 必须与明确的 repository 一起提供。');
  if (options.repository !== undefined && [options.workspace, options['credential-workspace']].some(value => value !== undefined && resolve(value) !== project)) fail('WORKSPACE_CONFLICT', '选仓操作的 project 是 Harness 管理仓；其他管理仓参数不能重定向该入口。');
  const operationOptions = { directory: options['harness-directory'], repository: options.repository, checkout: options.checkout, remote: options.remote, refs: options.ref || [], apply: Boolean(options.apply), 'expected-fingerprint': options['expected-fingerprint'], 'operation-id': options['operation-id'] };
  const config = await import('./config.mjs');
  const authModule = await import('./auth.mjs');
  const authenticationOptions = { workspace: options.workspace && resolve(options.workspace), harnessDirectory: options['harness-directory'], authMethod: options['auth-method'], ghUser: options['gh-user'], credentialWorkspace: options['credential-workspace'], credentialFile: options['credential-file'], credentialsFilled: Boolean(options['credentials-filled']), githubAuth: Boolean(options['github-auth']), useGlobalConfig: Boolean(options['use-global-config']) };
  let result;
  if (command === 'read' || (command === 'check' && options.repository !== undefined)) {
    const { projectOperation } = await import('./project-entry.mjs');
    result = await projectOperation(project, command, operationOptions);
  }
  else if (command === 'check') {
    const { entryCheck } = await import('./entry-check.mjs');
    result = await entryCheck(project, { useGlobalConfig: Boolean(options['use-global-config']), directory: options['harness-directory'], ...authenticationOptions });
  }
  else if (command === 'harness-recover') {
    const { recoverHarness } = await import('./harness-transaction.mjs');
    result = await recoverHarness(project, required(options['plan-file'], '--plan-file'), { apply: Boolean(options.apply), expectedFingerprint: options['expected-fingerprint'] });
  }
  else if (['harness-plan', 'harness-apply'].includes(command)) {
    const harness = await import('./harness.mjs');
    const input = await json(required(options.input, '--input'));
    if (command === 'harness-apply') {
      apply(options);
      result = await harness.applyHarness(project, input, { expectedFingerprint: options['expected-fingerprint'], planFile: options['plan-file'] });
    } else {
      const plan = await harness.planHarness(project, input);
      result = { ...plan, files: undefined, edits: plan.edits.map(({ content, ...edit }) => edit) };
      if (options['plan-file']) {
        const { saveHarnessPreview } = await import('./harness-transaction.mjs');
        if (resolve(options.input) === resolve(options['plan-file'])) fail('harness_record_path', '选择输入与恢复记录必须使用不同文件。');
        await saveHarnessPreview(options['plan-file'], result);
      }
    }
  } else if (command === 'setup') {
    const { setup } = await import('./setup.mjs');
    result = await setup(project, { url: required(options.url, '--url'), action: options.action, name: options.name, apply: options.apply, branch: options.branch, authorName: options['author-name'], authorEmail: options['author-email'], expectedFingerprint: options['expected-fingerprint'], acceptEffects: options['accept-effects'], planFile: options['plan-file'], ...authenticationOptions });
  } else if (command === 'auth-prepare') {
    result = await authModule.prepareAuth(project, required(options.url, '--url'), { ...authenticationOptions, apply: options.apply, allowIgnore: options['allow-ignore'], replaceAuth: options['replace-auth'] });
  } else if (command === 'auth-verify') {
    result = await authModule.verifyAuth(project, required(options.url, '--url'), { ...authenticationOptions, apiOnly: Boolean(options['api-only']), apply: Boolean(options.apply), expectedFingerprint: options['expected-fingerprint'] });
  }  else if (command === 'auth-info') {
    if (options.url) result = authModule.publicSelection(await authModule.authSelection(project, options.url, authenticationOptions));
    else {
      const { values, sources } = await config.loadEnvironment((await config.inspectProject(project)).root, authenticationOptions);
      result = { sources, present: Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Boolean(value)])), hint: '用 --url 查询特定完整远端的文件或 gh 绑定。' };
    }
  } else if (['issue-resolve', 'issue-create', 'issue-update', 'issue-comment', 'issue-recover'].includes(command)) {
    for (const key of Object.keys(options)) if (!['project', 'input', 'apply', 'expected-fingerprint', 'use-global-config'].includes(key)) fail('invalid_argument', 'Issue 操作仅消费 --input 中的完整本次输入，旧目标覆盖参数已退出。');
    const { issueOperation } = await import('./issue-operation.mjs');
    result = await issueOperation(project, command, await json(required(options.input, '--input')), { apply: Boolean(options.apply), expectedFingerprint: options['expected-fingerprint'], useGlobalConfig: Boolean(options['use-global-config']) });
  }
  else if (command === 'audit') {
    const { auditHistory } = await import('./audit.mjs');
    result = auditHistory(project, required(options.ref, '--ref'));
  } else if (['inspect', 'init', 'remote-add', 'publish', 'sync'].includes(command)) {
    const git = await import('./git.mjs');
    const auth = ['publish', 'sync'].includes(command) ? authModule.gitAuthentication((await config.inspectProject(project)).root, authenticationOptions) : {};
    if (command === 'inspect') result = await git.inspect(project);
    if (command === 'init') { apply(options); result = await git.init(project, { branch: required(options.branch, '--branch'), authorName: required(options['author-name'], '--author-name'), authorEmail: required(options['author-email'], '--author-email') }); }
    if (command === 'remote-add') { apply(options); result = await git.addRemote(project, { name: required(options.name, '--name'), url: required(options.url, '--url') }); }
    if (command === 'publish') {
      const refs = required(options.ref, '--ref');
      const { readHarnessConfig } = await import('./harness-config.mjs');
      if (options.repository !== undefined || (await readHarnessConfig(project, options['harness-directory'])).exists) {
        const { projectOperation } = await import('./project-entry.mjs');
        result = await projectOperation(project, 'publish', { ...operationOptions, remote: required(options.remote, '--remote'), refs });
      } else {
        apply(options);
        const { auditHistory } = await import('./audit.mjs');
        const audit = auditHistory(project, refs);
        if (!audit.complete) result = { status: 'audit_blocked', complete: false, audit };
        else result = { ...await git.publish(project, { remote: required(options.remote, '--remote'), refs, expectedRefs: audit.snapshot, ...auth }), audit };
      }
    }
    if (command === 'sync') {
      const { projectOperation } = await import('./project-entry.mjs');
      result = { ...await projectOperation(project, 'sync', operationOptions), configurationSource: 'harness' };
    }
    if (auth.authReports && !result.authentication) result.authentication = { sources: auth.authReports, pushPreflight: 'not_performed', actualPush: options.apply ? 'see_per_ref_results' : 'not_performed' };
  } else if (command === 'repo-create') {
    apply(options);
    if (!['private', 'public'].includes(options.visibility)) fail('visibility_required', '明确指定 --visibility private 或 public。');
    const { createRemoteRepository } = await import('./issues.mjs');
    const owner = required(options.owner, '--owner'), name = required(options.name, '--name');
    const client = await authModule.boundGitHubClient((await config.inspectProject(project)).root, { host: 'github.com', repository: `${owner}/${name}` }, authenticationOptions);
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
