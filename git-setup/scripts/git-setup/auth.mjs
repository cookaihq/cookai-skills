import { relationshipSelection } from './repository-relationship.mjs';
import { lstat, open, mkdir, appendFile } from 'node:fs/promises';
import { join, resolve, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { loadEnvironment } from './config.mjs';
import { inspectRemote, prepareCredentialRepository, credentialInitialization } from './git.mjs';
import { createGitHubClient, authFromEnvironment } from './github.mjs';
import { authFail, authTarget, credentialOwner, localEnvironment, localGit } from './auth-roots.mjs';
import { parseCnbCredential, cnbCredentialFormat } from './cnb-credential.mjs';
import { readSources, saveSource, sourceRecordPath, validateSource } from './source-record.mjs';
import { gitConfiguration } from './git-trust.mjs';
const exec = promisify(execFile);
const hash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const helperFile = fileURLToPath(new URL('./credential-helper.mjs', import.meta.url));
const methods = { 'github.com': ['gh', 'github-token', 'environment'], 'cnb.cool': ['cnb-url'] };
const sourceLabel = source => source.file || (source.method === 'gh' ? 'gh:' + source.account : 'environment');
function sourceClient(source, client) {
  return Object.fromEntries(Object.entries(client).map(([name, value]) => [name, typeof value === 'function' ? async (...args) => {
    try { return await value(...args); }
    catch (error) { error.message = `${error.message} 目标：${source.target.url}；已选来源：${sourceLabel(source)}。`; throw error; }
  } : value]));
}

export async function ghAccounts({ run = exec, sleep = ms => new Promise(done => setTimeout(done, ms)) } = {}) {
  try { await run('gh', ['--version'], { env: localEnvironment(), encoding: 'utf8', timeout: 15000 }); }
  catch { authFail('gh_auth_unavailable', '无法运行 GitHub CLI。macOS 可安装 brew install gh；Windows 可安装 winget install --id GitHub.cli；安装后核对 github.com 登录。'); }
  for (let attempt = 0; attempt < 3; attempt++) {
    let accounts, retry = false;
    try {
      const probe = await run('gh', ['auth', 'status', '--hostname', 'github.com', '--json', 'hosts'], { env: localEnvironment(), encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024 });
      try { accounts = JSON.parse(probe.stdout).hosts['github.com']; } catch { authFail('gh_auth_unavailable', 'gh 未提供可验证的账号列表；请更新 GitHub CLI 并检查登录。'); }
      retry = Array.isArray(accounts) && accounts.some(account => account.state === 'timeout');
    } catch (error) {
      retry = Boolean(error.killed) || /timeout|timed out|connection reset|no such host|temporary failure|HTTP (?:429|5[0-9][0-9])/i.test(error.stderr || '');
      if (!retry) authFail('gh_auth_unavailable', '无法核验 gh 的 github.com 登录状态；不使用环境 Token 或其他 helper，错误原文已隐藏。');
    }
    if (retry) {
      if (attempt === 2) authFail('gh_auth_network', 'gh 登录核验在三次尝试内未完成；保留已选账号，不切换来源。');
      const waitMs = 1000 * 2 ** attempt;
      process.stderr.write(`${JSON.stringify({ operation: 'gh-auth-status', attempt: attempt + 2, maxAttempts: 3, waitMs, reason: 'network' })}\n`);
      await sleep(waitMs); continue;
    }
    if (!Array.isArray(accounts) || !accounts.length) authFail('gh_auth_unavailable', 'gh 没有 github.com 的已保存登录。');
    return accounts.map(account => ({ login: account.login, state: account.state, active: Boolean(account.active) })).filter(account => typeof account.login === 'string');
  }
}

async function protectedPath(file, owner, { allowMissing = false, allowIgnore = false } = {}) {
  const path = relative(owner.main, file).replaceAll('\\', '/');
  if (!path || path.startsWith('../') || path === '..' || path.startsWith('/') || /^[A-Za-z]:/.test(path)) authFail('auth_file_outside_owner', '凭证文件必须位于已确认凭证主检出内。');
  const tracked = await localGit(owner.main, ['ls-files', '--', path]);
  if (tracked.trim()) authFail('auth_file_tracked', `凭证文件已被 Git 跟踪：${file}。不会移除索引或写入。`);
  let ignored = await localGit(owner.main, ['check-ignore', '-q', '--', path], true);
  if (ignored === null && allowIgnore) {
    // Explicit preparation scope only. Escape Git ignore metacharacters; never overwrite project rules.
    const exclusion = join(owner.commonDir, 'info', 'exclude');
    await mkdir(dirname(exclusion), { recursive: true });
    try { if ((await lstat(exclusion)).isSymbolicLink()) authFail('auth_ignore_symlink', 'Git exclude 不能是符号链接。'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    await appendFile(exclusion, `\n/${path.replace(/[\\*?\[\]#! ]/g, '\\$&')}\n`, { mode: 0o600 });
    ignored = await localGit(owner.main, ['check-ignore', '-q', '--', path], true);
  }
  if (ignored === null) authFail('auth_file_not_ignored', `凭证文件未被 Git 忽略：${file}。在已授权的文件准备范围内增加忽略规则后重试。`);
  let cursor = owner.main;
  const parts = path.split('/');
  for (const [index, part] of parts.entries()) {
    cursor = join(cursor, part);
    try {
      const info = await lstat(cursor);
      if (info.isSymbolicLink()) authFail('auth_file_symlink', `凭证路径不能经过符号链接：${cursor}`);
      if (index < parts.length - 1 && !info.isDirectory()) authFail('auth_file_invalid', `凭证父路径不是目录：${cursor}`);
      if (index < parts.length - 1 && process.platform !== 'win32' && (info.mode & 0o777) !== 0o700) authFail('auth_directory_permissions', `凭证目录权限必须为 700：${cursor}`);
    } catch (error) { if (!(allowMissing && error.code === 'ENOENT')) throw error; }
  }
}

function validateFileInfo(info, file) {
  if (!info.isFile() || info.nlink !== 1 || info.size > 16384) authFail('auth_file_invalid', `凭证必须是单链接普通小文件：${file}`);
  if (process.platform !== 'win32' && (info.mode & 0o777) !== 0o600) authFail('auth_file_permissions', `凭证文件权限必须为 600：${file}`);
}

async function readFileCredential(source) {
  await protectedPath(source.file, source.owner);
  let info, raw;
  try {
    info = await lstat(source.file);
    validateFileInfo(info, source.file);
    const handle = await open(source.file, 'r');
    try {
      const current = await handle.stat();
      if (current.ino !== info.ino || current.dev !== info.dev) authFail('auth_file_changed', '读取前凭证文件发生变化。');
      raw = await handle.readFile('utf8');
    } finally { await handle.close(); }
  } catch (error) {
    if (error.code?.startsWith('auth_')) throw error;
    authFail('auth_file_unavailable', `无法读取已选凭证文件：${source.file}。不切换来源。`);
  }
  if (source.method === 'cnb-url') return parseCnbCredential(raw, source.target);
  const value = raw.replace(/^\uFEFF/, '').replace(/\r?\n$/, '');
  if (!value || /[\s\0"']/u.test(value)) authFail('auth_file_format', `凭证应为单行原文，不加引号、变量名或空格：${source.file}`);
  if (source.method === 'github-token') {
    if (/[=:]/.test(value)) authFail('auth_file_format', `GitHub 文件只能填写 Token 原文：${source.file}`);
    return { username: 'x-access-token', password: value, revision: hash(raw) };
  }
  authFail('auth_method_invalid', '未知文件鉴权方式。');
}

export async function readCredential(source, options = {}) {
  const scope = await credentialOwner(source.project, { ...options, expectedOwner: source.owner, expectedRelationship: source.relationship });
  if (source.commonDir && scope.repository.commonDir !== source.commonDir) authFail('auth_source_moved', '本次操作的仓库元数据发生变化。');
  if (source.method === 'gh') {
    let output;
    try { output = await (options.run || exec)('gh', ['auth', 'token', '--hostname', 'github.com', '--user', source.account], { env: localEnvironment(), encoding: 'utf8', timeout: 15000, maxBuffer: 32768 }); }
    catch { authFail('gh_account_unavailable', `无法读取已选 gh 账号 ${source.account}；不使用当前活动账号或环境 Token。`); }
    const token = output.stdout.trim();
    if (!token || /[\s\0]/.test(token)) authFail('gh_account_unavailable', '已选 gh 账号没有可用的单行 Token。');
    return { username: source.account, password: token, revision: hash(token) };
  }
  if (source.method === 'environment') {
    const { values, sources } = await loadEnvironment(source.project, { useGlobalConfig: source.useGlobalConfig });
    const auth = authFromEnvironment(values, source.target);
    const environmentSource = sources.GIT_SETUP_GITHUB_TOKEN;
    if (auth.method !== 'token' || !environmentSource) authFail('auth_environment_source_changed', '已登记环境文件来源发生变化；修复原来源，不回退或迁移凭证。');
    return { username: 'x-access-token', password: auth.token, revision: hash(auth.token) };
  }
  return readFileCredential(source);
}


function targetOptions(options, target) {
  const matches = (options.sources || []).filter(item => authTarget(item.url)?.key === target.key);
  if (matches.length > 1) authFail('auth_source_duplicate', '本次输入为同一目标提供了多个来源。');
  const item = matches[0];
  return item ? { ...options, authMethod: item.method, credentialFile: item.file, ghUser: item.account } : options;
}
async function resolveSource(project, target, options) {
  options = targetOptions(options, target);
  const scope = await credentialOwner(project, options);
  const saved = options.authMethod === undefined ? (await readSources(scope.owner)).find(item => authTarget(item.url).key === target.key) : undefined;
  const explicit = options.authMethod !== undefined;
  // Saved entries represent an explicit choice for this complete target. Ambient
  // gh logins never participate in selection or replace a missing project Token.
  const method = options.authMethod ?? saved?.method ?? (target.host === 'cnb.cool' ? 'cnb-url' : 'github-token');
  const file = options.credentialFile ? resolve(scope.owner.main, options.credentialFile) : (!explicit && saved?.file ? resolve(scope.owner.main, saved.file) : join(scope.directory, target.filename));
  if (!methods[target.host].includes(method)) authFail('auth_method_invalid', '鉴权方式与目标平台不匹配。');
  return { options, scope, method, file, account: options.ghUser || (!explicit && saved?.account) };
}

export async function authSelection(project, url, options = {}) {
  const target = authTarget(url);
  if (!target) return { status: 'not_applicable', target: null };
  const selected = await resolveSource(project, target, options);
  options = selected.options;
  const { scope, method, file, account } = selected;
  let source = { target, method, project: scope.repository.main, commonDir: scope.repository.commonDir, owner: scope.owner, relationship: relationshipSelection(scope.relationship) };
  if (method === 'gh') {
    if (!account) return { status: 'needs_input', required: ['gh-user'], target, accounts: options.metadataOnly ? [] : await ghAccounts(options) };
    if (!/^[A-Za-z0-9-]+$/.test(account)) authFail('gh_account_invalid', 'gh 账号名无效。');
    source.account = account;
  } else if (method === 'environment') source.useGlobalConfig = Boolean(options.useGlobalConfig);
  else {
    source.file = file;
    try { if ((await lstat(file)).size === 0) return { status: 'needs_input', required: ['credentials-filled'], target, source, credentialFile: file }; }
    catch (error) { if (error.code !== 'ENOENT') throw error; return { status: 'needs_input', required: ['credential-prepare'], target, source, credentialFile: file }; }
  }
  if (options.metadataOnly && method === 'gh') return { status: 'selected', target, source, relationship: scope.relationship, fingerprint: hash(source), verification: 'not_performed' };
  const credential = await readCredential(source, options);
  return { status: 'selected', target, source, relationship: scope.relationship, fingerprint: hash({ source, revision: credential.revision, relationship: scope.relationship.fingerprint }), credential };
}

const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
function helperCommand() {
  const command = quote(process.execPath.replaceAll('\\', '/')) + ' ' + quote(helperFile.replaceAll('\\', '/')) + ' --transport';
  return '!f() { ' + command + ' "$@" || printf "quit=true\\n\\n"; }; f';
}

export async function prepareAuth(project, url, options = {}) {
  const target = authTarget(url);
  if (!target) authFail('auth_target_unsupported', '凭证准备只支持 GitHub/CNB HTTPS。');
  const selected = await resolveSource(project, target, options);
  options = selected.options;
  const { method, file } = selected;
  if (method === 'gh' || method === 'environment') return publicSelection(await authSelection(project, url, options));
  let { scope } = selected;
  let initialization = scope.repository.isGit ? await credentialInitialization(project, options) : null;
  if (initialization && initialization.target !== target.key) authFail('auth_initialization_target_changed', '未完成初始化的目标不同；先核对原任务。');
  if (!scope.repository.isGit) {
    initialization = await prepareCredentialRepository(project, target.key, options);
    if (!options.apply) return { status: 'planned', initialization, target };
    scope = await credentialOwner(project, options);
  }
  await protectedPath(file, scope.owner, { allowMissing: true, allowIgnore: Boolean(options.apply && options.allowIgnore) });
  if (!options.apply) return { status: 'planned', credentialFile: file, target, method };
  await mkdir(dirname(file), { recursive: true, mode: 0o700 });
  let created = false;
  try { const handle = await open(file, 'wx', 0o600); await handle.close(); created = true; }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  await protectedPath(file, scope.owner);
  validateFileInfo(await lstat(file), file);
  return { status: 'awaiting_credentials', target, method, initialization, created, credentialFile: file,
    credentialLink: '[' + file + '](<' + file + '>)', format: method === 'cnb-url' ? cnbCredentialFormat(target) : '单行 Token 原文，不加引号或变量名',
    message: '用户在本机填写并回复已填写后再验证；本次暂未提交和推送。', persistentHelper: false };
}

export async function rememberSource(project, entry, options = {}) {
  const scope = await credentialOwner(project, options);
  entry = validateSource(entry, scope.owner);
  if (options.apply && options.allowIgnore) await protectedPath(sourceRecordPath(scope.owner), scope.owner, { allowMissing: true, allowIgnore: true });
  if (!options.apply) return { status: 'planned', file: sourceRecordPath(scope.owner), source: entry };
  return saveSource(scope.owner, entry);
}

export async function inspectAuth(project, url, options = {}) {
  try { return { ...publicSelection(await authSelection(project, url, { ...options, metadataOnly: true })), network: 'not_performed' }; }
  catch (error) { return { status: 'invalid', code: error.code, message: /^(auth_|gh_)/.test(error.code || '') ? error.message : '无法检查已选来源。', network: 'not_performed' }; }
}

export async function verifyAuth(project, url, options = {}) {
  const selected = await verifySelection(await authSelection(project, url, options), options);
  if (selected.status !== 'selected') return publicSelection(selected);
  if (options.apiOnly && selected.target.host !== 'github.com') authFail('auth_api_unsupported', 'CNB 必须验证实际 Git 读取。');
  if (!options.apiOnly) await inspectRemote(project, url, { ...options, ...gitAuthentication(project, options, selected) });
  return { ...publicSelection(selected), status: 'verified', remoteRead: options.apiOnly ? 'not_performed' : 'verified',
    persistentHelper: false, apiPermissions: 'not_verified', pushPreflight: 'not_performed', pushed: false };
}
export async function verifySelection(selection, options = {}) {
  if (selection.status !== 'selected') return selection;
  const { source, credential } = selection;
  let identity = { status: 'not_available', reason: 'CNB 本次仅验证 Git 凭证；不调用账号 API。' };
  if (source.target.host === 'github.com') {
    const client = sourceClient(source, createGitHubClient({ auth: { method: 'token', token: credential.password, host: 'github.com', repository: source.target.repository }, transport: options.apiTransport }));
    const account = await client.getAuthenticatedUser(source.target.repository);
    if (!account?.login || !Number.isSafeInteger(account.id)) authFail('auth_identity_invalid', 'GitHub 没有返回可验证的账号身份。');
    if (source.account && account.login.toLowerCase() !== source.account.toLowerCase()) authFail('auth_identity_changed', 'GitHub 返回账号与已选 gh 账号不一致。');
    if (options.expectedAccount && account.login.toLowerCase() !== options.expectedAccount.toLowerCase()) authFail('auth_identity_changed', 'GitHub 返回账号不符合本次项目规则。');
    identity = { status: 'verified', login: account.login, id: account.id };
  }
  const verified = { ...source, identity };
  return { ...selection, source: verified, identity, fingerprint: hash({ source: verified, revision: credential.revision, relationship: selection.relationship?.fingerprint }) };
}

export function publicSelection(selection) {
  if (!selection) return null;
  const { credential, ...publicData } = selection;
  return publicData;
}

export function gitAuthentication(project, options = {}, prepared) {
  const cache = new Map(prepared ? [[prepared.target.key, prepared]] : []);
  const authReports = [];
  return { authReports, authForUrl: async url => {
    const target = authTarget(url);
    if (!target) return { env: {}, metadata: { method: 'existing' } };
    let selected = cache.get(target.key) || await authSelection(project, url, options);
    if (selected.status !== 'selected') authFail('auth_selection_required', `目标 ${target.url} 的来源 ${selected.credentialFile || selected.source?.account || 'gh（待明确账号）'} 尚未就绪（${selected.required.join(', ')}）；先处理该来源，不回退全局 gh 或其他凭证。`);
    if (!cache.has(target.key)) { selected = await verifySelection(selected, options); cache.set(target.key, selected); }
    if (!authReports.some(report => report.source.target.key === target.key)) authReports.push(publicSelection(selected));
    // Read the selected source again for every Git operation; detect rotation during a plan.
    const current = await readCredential(selected.source, options);
    if (current.revision !== selected.credential.revision) authFail('auth_source_changed', '选定凭证在本次操作中发生变化，请重新核验计划。');
    const headers = await localGit(project, ['config', '--get-urlmatch', 'http.extraheader', url], true);
    if (/(?:authorization|proxy-authorization|cookie)\s*:/im.test(headers || '')) authFail('auth_http_header_conflict', '当前目标存在额外 HTTP 鉴权头；先修复原配置来源。');
    const settings = [[`http.${url}.followRedirects`, 'false'], [`http.${url}.extraHeader`, ''], ['credential.helper', ''], ['credential.helper', helperCommand()], ['credential.useHttpPath', 'true'], ['credential.username', current.username], ['http.extraHeader', ''], ['http.followRedirects', 'false']];
    return { env: { GIT_SETUP_TRANSPORT_TOKEN: current.password, GIT_SETUP_TRANSPORT_USERNAME: current.username, GIT_SETUP_TRANSPORT_TARGET: target.url, GIT_ASKPASS: '', SSH_ASKPASS: '', GIT_TERMINAL_PROMPT: '0', ...gitConfiguration(settings) }, metadata: publicSelection(selected) };
  } };
}


export async function selectedGitHubClient(project, target, options = {}) {
  const selected = await verifySelection(await authSelection(project, 'https://' + target.host + '/' + target.repository, options), options);
  if (selected.status !== 'selected') authFail('auth_selection_required', `GitHub API 目标 ${selected.target.url} 的来源 ${selected.credentialFile || 'gh（待明确账号）'} 尚未就绪（${selected.required.join(', ')}）；先处理该来源，不回退全局 gh 或其他凭证。`);
  return { ...sourceClient(selected.source, createGitHubClient({ auth: { method: 'token', token: selected.credential.password, ...target }, transport: options.apiTransport })), authentication: publicSelection(selected) };
}
