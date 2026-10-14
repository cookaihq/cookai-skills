import { relationshipSelection, resolveRepository } from './repository-relationship.mjs';
import { lstat, open, mkdir, appendFile } from 'node:fs/promises';
import { join, resolve, dirname, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash } from 'node:crypto';
import { inspectProject, loadEnvironment } from './config.mjs';
import { updateLocalConfig, inspectRemote, prepareCredentialRepository, credentialInitialization } from './git.mjs';
import { createGitHubClient, authFromEnvironment } from './github.mjs';
import { authFail, authTarget, credentialOwner, localEnvironment, localGit } from './auth-roots.mjs';
import { installHelperRuntime } from './runtime-bundle.mjs';

const exec = promisify(execFile);
const hash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const bindingKey = target => `gitsetup.auth-${hash(target.key)}.binding`;
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
const helperFile = fileURLToPath(new URL('./credential-helper.mjs', import.meta.url));
const methods = { 'github.com': ['gh', 'github-token', 'legacy'], 'cnb.cool': ['cnb-url'] };
const sourceLabel = binding => binding.file || (binding.method === 'gh' ? `gh:${binding.account}` : binding.source);
function sourceClient(binding, client) {
  return Object.fromEntries(Object.entries(client).map(([name, value]) => [name, typeof value === 'function' ? async (...args) => {
    try { return await value(...args); }
    catch (error) { error.message = `${error.message} 目标：${binding.target.url}；已选来源：${sourceLabel(binding)}。`; throw error; }
  } : value]));
}

async function parseBinding(root, target) {
  const state = await inspectProject(root);
  if (!state.isGit) return null;
  const values = await localGit(state.root, ['config', '--local', '--get-all', bindingKey(target)], true);
  if (values === null) return null;
  let binding;
  try { binding = JSON.parse(values); } catch { authFail('auth_binding_invalid', '仓库鉴权绑定损坏或重复；修复原 Git 配置，不回退其他来源。'); }
  const allowed = ['version', 'target', 'method', 'project', 'commonDir', 'owner', 'state', 'account', 'file', 'source', 'useGlobalConfig', 'identity', 'credentialRevision', 'relationship'];
  if (!binding || typeof binding !== 'object' || Object.keys(binding).some(key => !allowed.includes(key)) || !binding.owner || typeof binding.owner.main !== 'string' || typeof binding.project !== 'string') authFail('auth_binding_invalid', '鉴权绑定结构无效；不输出原配置。');
  if (binding.version !== 1 || binding.target?.key !== target.key || !methods[target.host].includes(binding.method) || !['pending', 'ready'].includes(binding.state) || binding.commonDir !== state.commonDir) authFail('auth_binding_invalid', '仓库鉴权绑定的目标、状态或 Git common directory 不匹配。');
  if (JSON.stringify(authTarget(binding.target.url)) !== JSON.stringify(binding.target)) authFail('auth_binding_invalid', '鉴权绑定的完整目标被修改。');
  const pathValid = value => typeof value === 'string' && isAbsolute(value) && !/[\r\n\0]/.test(value);
  const ownerKeys = Object.keys(binding.owner).sort().join(',');
  if (binding.relationship && (Object.keys(binding.relationship).sort().join(',') !== 'directory,workspace' || !pathValid(binding.relationship.workspace) || (binding.relationship.directory !== null && typeof binding.relationship.directory !== 'string'))) authFail('auth_binding_invalid', '保存的管理仓选择无效。');
  const isFile = ['github-token', 'cnb-url'].includes(binding.method);
  if (ownerKeys !== 'commonDir,main' || ![binding.project, binding.commonDir, binding.owner.main, binding.owner.commonDir].every(pathValid)
      || (isFile && !pathValid(binding.file)) || (!isFile && binding.file !== undefined)
      || (binding.method === 'gh' && (typeof binding.account !== 'string' || !/^[A-Za-z0-9-]+$/.test(binding.account)))
      || (binding.method !== 'gh' && binding.account !== undefined)
      || (binding.method === 'legacy' && !(binding.source === 'process' || pathValid(binding.source)))
      || (binding.credentialRevision !== undefined && !/^[a-f0-9]{64}$/.test(binding.credentialRevision))) authFail('auth_binding_invalid', '鉴权绑定的来源结构无效；不输出原配置。');
  if (binding.identity) {
    const expectedKeys = target.host === 'github.com' ? 'id,login,status' : 'reason,status';
    if (Object.keys(binding.identity).sort().join(',') !== expectedKeys
        || (target.host === 'github.com' && (binding.identity.status !== 'verified' || !Number.isSafeInteger(binding.identity.id) || typeof binding.identity.login !== 'string' || !/^[A-Za-z0-9-]+$/.test(binding.identity.login)))
        || (target.host === 'cnb.cool' && (binding.identity.status !== 'not_available' || binding.identity.reason !== 'CNB 本次仅验证 Git 凭证；不调用账号 API。'))) authFail('auth_binding_invalid', '已保存身份结构无效；不输出原配置。');
  }
  if (binding.state === 'ready' && (!binding.identity || !binding.credentialRevision)) authFail('auth_binding_invalid', '已完成绑定缺少身份或来源核验记录。');
  return binding;
}

/** Saved auth choices provide manager candidates, never an alternate registry. */
export async function boundRepositoryRelationship(project, options = {}) {
  if (options.workspace || options.credentialWorkspace) return resolveRepository(project, options);
  const state = await inspectProject(project);
  if (!state.isGit) return resolveRepository(project, options);
  const raw = await localGit(state.root, ['config', '--local', '-z', '--get-regexp', String.raw`^gitsetup\.auth-[a-f0-9]+\.binding$`], true);
  const relations = new Map();
  for (const record of (raw || '').split('\0').filter(Boolean)) {
    let target;
    try { target = authTarget(JSON.parse(record.slice(record.indexOf('\n') + 1)).target.url); }
    catch { authFail('auth_binding_invalid', '保存的鉴权选择无法解析；不输出原配置或猜测管理仓。'); }
    if (!target) authFail('auth_binding_invalid', '保存的鉴权目标无效。');
    const binding = await parseBinding(state.root, target);
    const relation = await resolveRepository(project, { ...options, expectedOwner: binding.owner, expectedRelationship: binding.relationship });
    relations.set(relation.management.commonDir, relation);
  }
  if (relations.size > 1) authFail('WORKSPACE_AMBIGUOUS', '已保存选择涉及多个管理仓；本次明确指定 workspace 后再检查。');
  return relations.values().next().value || resolveRepository(project, options);
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
  for (const part of path.split('/')) {
    cursor = join(cursor, part);
    try {
      const info = await lstat(cursor);
      if (info.isSymbolicLink()) authFail('auth_file_symlink', `凭证路径不能经过符号链接：${cursor}`);
      if (cursor !== file && !info.isDirectory()) authFail('auth_file_invalid', `凭证父路径不是目录：${cursor}`);
      if (cursor !== file && process.platform !== 'win32' && (info.mode & 0o777) !== 0o700) authFail('auth_directory_permissions', `凭证目录权限必须为 700：${cursor}`);
    } catch (error) { if (!(allowMissing && error.code === 'ENOENT')) throw error; }
  }
}

function validateFileInfo(info, file) {
  if (!info.isFile() || info.nlink !== 1 || info.size > 16384) authFail('auth_file_invalid', `凭证必须是单链接普通小文件：${file}`);
  if (process.platform !== 'win32' && (info.mode & 0o777) !== 0o600) authFail('auth_file_permissions', `凭证文件权限必须为 600：${file}`);
}

async function readFileCredential(binding) {
  await protectedPath(binding.file, binding.owner);
  let info, raw;
  try {
    info = await lstat(binding.file);
    validateFileInfo(info, binding.file);
    const handle = await open(binding.file, 'r');
    try {
      const current = await handle.stat();
      if (current.ino !== info.ino || current.dev !== info.dev) authFail('auth_file_changed', '读取前凭证文件发生变化。');
      raw = await handle.readFile('utf8');
    } finally { await handle.close(); }
  } catch (error) {
    if (error.code?.startsWith('auth_')) throw error;
    authFail('auth_file_unavailable', `无法读取已选凭证文件：${binding.file}。不切换来源。`);
  }
  const value = raw.replace(/^\uFEFF/, '').replace(/\r?\n$/, '');
  if (!value || /[\s\0"']/u.test(value)) authFail('auth_file_format', `凭证应为单行原文，不加引号、变量名或空格：${binding.file}`);
  if (binding.method === 'github-token') {
    if (/[=:]/.test(value)) authFail('auth_file_format', `GitHub 文件只能填写 Token 原文：${binding.file}`);
    return { username: 'x-access-token', password: value, revision: hash(raw) };
  }
  let url;
  try { url = new URL(value); } catch { authFail('auth_file_format', `CNB 文件必须填写完整 HTTPS 鉴权 URL：${binding.file}`); }
  let password;
  try { password = decodeURIComponent(url.password); } catch { authFail('auth_file_format', 'CNB 令牌包含无效 URL 编码。'); }
  const target = authTarget(`https://${url.host}${url.pathname}`);
  if (url.protocol !== 'https:' || url.username !== 'cnb' || !password || /[\r\n\0]/.test(password) || url.search || url.hash || target?.key !== binding.target.key) authFail('auth_file_target_mismatch', `CNB 凭证 URL 与已选完整仓库目标不一致：${binding.file}`);
  return { username: 'cnb', password, revision: hash(raw) };
}

export async function readCredential(binding, options = {}) {
  const scope = await credentialOwner(binding.project, { ...options, expectedOwner: binding.owner, expectedRelationship: binding.relationship });
  if (binding.commonDir && scope.repository.commonDir !== binding.commonDir) authFail('auth_binding_moved', '当前仓库已不再对应保存鉴权选择时的 Git 元数据。');
  if (binding.method === 'gh') {
    let output;
    try { output = await (options.run || exec)('gh', ['auth', 'token', '--hostname', 'github.com', '--user', binding.account], { env: localEnvironment(), encoding: 'utf8', timeout: 15000, maxBuffer: 32768 }); }
    catch { authFail('gh_account_unavailable', `无法读取已选 gh 账号 ${binding.account}；不使用当前活动账号或环境 Token。`); }
    const token = output.stdout.trim();
    if (!token || /[\s\0]/.test(token)) authFail('gh_account_unavailable', '已选 gh 账号没有可用的单行 Token。');
    return { username: binding.account, password: token, revision: hash(token) };
  }
  if (binding.method === 'legacy') {
    const { values, sources } = await loadEnvironment(binding.project, { useGlobalConfig: binding.useGlobalConfig });
    const auth = authFromEnvironment(values, binding.target);
    const source = sources.GIT_SETUP_GITHUB_TOKEN;
    if (auth.method !== 'token' || !source || source !== binding.source) authFail('auth_legacy_source_changed', '已登记环境文件来源发生变化；修复原来源，不回退或迁移凭证。');
    return { username: 'x-access-token', password: auth.token, revision: hash(auth.token) };
  }
  return readFileCredential(binding);
}

export async function authSelection(project, url, options = {}) {
  const target = authTarget(url);
  if (!target) return { status: 'not_applicable', target: null };
  const saved = await parseBinding(project, target);
  const scope = await credentialOwner(project, { ...options, expectedOwner: saved?.owner, expectedRelationship: saved?.relationship });
  const file = options.credentialFile ? resolve(options.credentialFile) : saved?.file || join(scope.directory, target.filename);
  const method = options.authMethod || saved?.method || (options.githubAuth ? 'legacy' : undefined);
  if (!method) return { status: 'needs_input', relationship: scope.relationship, required: ['auth-method'], target, credentialFile: file, choices: target.host === 'github.com' ? [
    { method: 'gh', label: '使用本机 gh 已登录账号' },
    { method: 'github-token', label: `使用这个仓库的独立 GitHub Token（保存于 ${file}）` },
  ] : [{ method: 'cnb-url', label: `使用此 CNB 仓库的凭证文件（${file}）` }] };
  if (!methods[target.host].includes(method)) authFail('auth_method_invalid', '鉴权方式与目标平台不匹配。');
  if (saved && ((options.authMethod && method !== saved.method) || (options.credentialFile && file !== saved.file) || (options.ghUser && options.ghUser !== saved.account))) authFail('auth_selection_changed', '已保存的来源与本次选择不同；先核对并通过 auth-prepare --replace-auth 明确替换绑定。');
  let binding = saved || { version: 1, target, method, project: scope.repository.main, commonDir: scope.repository.commonDir, owner: scope.owner, relationship: relationshipSelection(scope.relationship), state: 'pending' };
  if (method === 'gh') {
    const accounts = await ghAccounts(options);
    const account = saved?.account || options.ghUser || (accounts.length === 1 ? accounts[0].login : undefined);
    if (!account) return { status: 'needs_input', relationship: scope.relationship, required: ['gh-user'], target, accounts };
    if (!accounts.some(item => item.login === account && item.state === 'success')) authFail('gh_account_unavailable', `已选 gh 账号 ${account} 未通过登录检查。`);
    binding = { ...binding, account };
  } else if (method === 'legacy') {
    const { values, sources } = await loadEnvironment(scope.repository.main, { useGlobalConfig: saved?.useGlobalConfig ?? options.useGlobalConfig });
    const auth = authFromEnvironment(values, target);
    if (auth.method === 'gh') return { status: 'needs_input', relationship: scope.relationship, required: ['auth-method', 'gh-user'], target, message: '旧 gh 配置没有固定账号，请在会话选择 gh 账号；不改变其登录状态。' };
    if (sources.GIT_SETUP_GITHUB_TOKEN === 'process' && !options.allowSessionAuth) authFail('auth_session_only', '进程 Token 仅适用于本轮，无法形成持久 Git helper；请选择已登记文件或 gh，不自动保存 Token。');
    if (!saved) binding = { ...binding, source: sources.GIT_SETUP_GITHUB_TOKEN, useGlobalConfig: Boolean(options.useGlobalConfig) };
  } else {
    binding = { ...binding, file };
    if (!saved) return { status: 'needs_input', relationship: scope.relationship, required: ['credential-prepare'], target, binding, credentialFile: file };
    if (binding.state === 'pending' && !options.credentialsFilled) return { status: 'needs_input', relationship: scope.relationship, required: ['credentials-filled'], target, binding, credentialFile: file, message: '等待用户回复“已填写”后再读取验证；不能从文件非空推断用户已确认。' };
  }
  const credential = await readCredential(binding, options);
  return { status: 'selected', target, binding, relationship: scope.relationship, fingerprint: hash({ binding, revision: credential.revision, relationship: scope.relationship.fingerprint }), credential };
}

function helperCommand(project, file = helperFile) {
  const command = `${quote(process.execPath.replaceAll('\\', '/'))} ${quote(file.replaceAll('\\', '/'))} ${project ? `--project ${quote(project.replaceAll('\\', '/'))}` : '--transport'}`;
  // If Node or the installed Skill disappears, stop Git before another helper/askpass can run.
  return `!f() { ${command} "$@" || printf 'quit=true\\n\\n'; }; f`;
}

async function persistBinding(project, binding, install = false) {
  const scope = await credentialOwner(project, { expectedOwner: binding.owner, expectedRelationship: binding.relationship });
  const updated = { ...binding, project: scope.repository.main, commonDir: scope.repository.commonDir, owner: scope.owner, relationship: relationshipSelection(scope.relationship) };
  if (!scope.repository.isGit) authFail('auth_requires_git', '持久鉴权绑定需要先建立目标 Git 仓库。');
  const runtime = install ? await installHelperRuntime(scope.repository.commonDir) : null;
  await updateLocalConfig(project, {}, async file => {
    await localGit(project, ['config', '--file', file, bindingKey(binding.target), JSON.stringify(updated)]);
    if (install) {
      const prefix = `credential.https://${binding.target.host}`;
      await localGit(project, ['config', '--file', file, '--replace-all', `${prefix}.helper`, '']);
      await localGit(project, ['config', '--file', file, '--add', `${prefix}.helper`, helperCommand(updated.project, runtime.file)]);
      await localGit(project, ['config', '--file', file, `${prefix}.useHttpPath`, 'true']);
      await localGit(project, ['config', '--file', file, `http.https://${binding.target.host}/.followRedirects`, 'false']);
    }
  });
  return updated;
}

/** Check Git's effective helper chain without ever returning its credential to the CLI. */
async function verifyNativeHelper(project, target, selected) {
  const headers = await localGit(project, ['config', '--get-urlmatch', 'http.extraheader', target.url], true);
  if (/^(?:authorization|proxy-authorization|cookie)\s*:/im.test(headers || '')) authFail('auth_http_header_conflict', '当前目标存在额外 HTTP 鉴权头；先修复该 Git 配置来源，不能同时使用另一身份。');
  const result = await new Promise((done, reject) => {
    const child = spawn('git', ['-C', project, 'credential', 'fill'], { env: localEnvironment(), windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = ''; let overflow = false;
    const timer = setTimeout(() => { overflow = true; child.kill(); }, 30000);
    child.stdout.on('data', chunk => { stdout += chunk; if (stdout.length > 32768) { overflow = true; child.kill(); } });
    child.stderr.resume(); // A helper from another configuration may print secrets; never relay it.
    child.on('error', () => { clearTimeout(timer); reject(Object.assign(new Error('无法启动 Git helper 验证。'), { code: 'auth_helper_unavailable' })); });
    child.on('close', code => { clearTimeout(timer); done({ code, stdout, overflow }); });
    child.stdin.on('error', () => {});
    child.stdin.end(`protocol=https\nhost=${target.host}\npath=${target.repository}.git\n\n`);
  });
  const fields = Object.fromEntries(result.stdout.split('\n').filter(line => line.includes('=')).map(line => { const index = line.indexOf('='); return [line.slice(0, index), line.slice(index + 1)]; }));
  if (result.code !== 0 || result.overflow || fields.username !== selected.username || fields.password !== selected.password) authFail('auth_helper_mismatch', '原生 Git 未读取到本次选定来源；检查仓库/Worktree/全局 helper 的覆盖关系。');
}

export async function rememberAuth(project, selection) {
  return persistBinding(project, { ...selection.binding, state: 'pending' });
}

export async function prepareAuth(project, url, options = {}) {
  const target = authTarget(url);
  if (!target || !methods[target.host].includes(options.authMethod) || options.authMethod === 'legacy') authFail('auth_method_required', '准备凭证时须明确 github-token、gh 或 cnb-url。');
  const old = await parseBinding(project, target);
  if (old && !options.replaceAuth && (old.method !== options.authMethod || (options.ghUser && old.account !== options.ghUser) || (options.credentialFile && old.file !== resolve(options.credentialFile)))) authFail('auth_selection_changed', '替换已有来源需要明确 --replace-auth；旧文件会保留。');
  let scope = await credentialOwner(project, { ...options, expectedOwner: options.replaceAuth ? undefined : old?.owner, expectedRelationship: options.replaceAuth ? undefined : old?.relationship });
  if (scope.repository.requestedPath !== scope.repository.root) authFail('auth_project_required', '凭证准备必须在实际项目根目录执行；不初始化父仓普通子目录。');
  let initialization;
  if (!scope.repository.isGit) {
    if (options.authMethod === 'gh') authFail('auth_requires_git', '无 Git 目录的 gh 选择直接传给 setup，按远端状态初始化或克隆后保存。');
    initialization = await prepareCredentialRepository(project, target.key, { ...options, apply: false });
    if (!options.apply) return { status: 'planned', target, initialization, credentialFile: options.credentialFile ? resolve(options.credentialFile) : join(scope.directory, target.filename) };
    if (!options.allowIgnore) return { status: 'needs_input', required: ['allow-ignore'], target, initialization, message: '无 Git 项目的文件准备需一并建立 Git 元数据及本机忽略规则；沿本次准备授权传 --allow-ignore。' };
    await prepareCredentialRepository(project, target.key, options);
    scope = await credentialOwner(project, options);
  }
  initialization = await credentialInitialization(scope.repository.root);
  if (initialization && initialization.target !== target.key) authFail('auth_initialization_target_changed', '凭证准备中的目标仓库已变化；先处理原初始化记录，不新增另一来源。');
  let binding = old && !options.replaceAuth ? { ...old } : { version: 1, target, method: options.authMethod, project: scope.repository.main, commonDir: scope.repository.commonDir, owner: scope.owner, relationship: relationshipSelection(scope.relationship), state: 'pending' };
  if (binding.method === 'gh') {
    const accounts = await ghAccounts(options);
    binding.account = options.ghUser || old?.account || (accounts.length === 1 ? accounts[0].login : undefined);
    if (!binding.account) return { status: 'needs_input', required: ['gh-user'], accounts };
    if (!accounts.some(item => item.login === binding.account && item.state === 'success')) authFail('gh_account_unavailable', '选定 gh 账号未通过登录检查。');
  } else {
    binding.file = options.credentialFile ? resolve(options.credentialFile) : old?.file || join(scope.directory, target.filename);
    await protectedPath(binding.file, scope.owner, { allowMissing: true, allowIgnore: Boolean(options.apply && options.allowIgnore) });
  }
  if (!options.apply) return { status: 'planned', binding, initialization, relationship: scope.relationship };
  let created = false;
  if (binding.file) {
    // A registry in the credential owner's local Git config reserves names across child repositories.
    const key = `gitsetup.file-${hash(binding.file.toLowerCase())}.target`;
    await updateLocalConfig(scope.owner.main, {}, async configFile => {
      const previous = (await localGit(scope.owner.main, ['config', '--file', configFile, '--get', key], true))?.trim();
      if (previous && previous !== target.key) authFail('auth_filename_collision', `同一凭证文件已绑定另一个完整仓库目标：${binding.file}`);
      let cursor = scope.owner.main;
      for (const part of relative(scope.owner.main, dirname(binding.file)).split(/[\\/]/)) {
        cursor = join(cursor, part);
        try { await mkdir(cursor, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
        const info = await lstat(cursor);
        if (!info.isDirectory() || info.isSymbolicLink()) authFail('auth_file_symlink', '凭证目录必须是实体目录。');
        if (process.platform !== 'win32' && (info.mode & 0o777) !== 0o700) authFail('auth_directory_permissions', `凭证目录权限必须为 700：${cursor}`);
      }
      try { const file = await open(binding.file, 'wx', 0o600); await file.close(); created = true; }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
      await protectedPath(binding.file, scope.owner);
      validateFileInfo(await lstat(binding.file), binding.file);
      await localGit(scope.owner.main, ['config', '--file', configFile, key, target.key]);
    });
  }
  binding = await persistBinding(project, binding);
  return { status: binding.file ? 'awaiting_credentials' : 'selected', binding, initialization, created, credentialFile: binding.file,
    relationship: scope.relationship,
    credentialLink: binding.file ? `[${binding.file}](<${binding.file}>)` : undefined,
    format: binding.method === 'github-token' ? '单行 Token 原文，不加引号或变量名' : binding.method === 'cnb-url' ? `单行 https://cnb:<访问令牌>@cnb.cool/${target.repository}` : undefined,
    required: binding.file ? ['credentials-filled'] : [],
    message: binding.file ? '同一条会话回复原样使用 credentialLink，提供目标、填写格式、本次所需权限及用途；等待用户回复“已填写”后验证。本次暂未提交和推送。' : 'gh 账号已选择；尚未验证该仓库的读取或写入权限。本次暂未提交和推送。' };
}

export async function verifySelection(selection, options = {}) {
  if (selection.status !== 'selected') return selection;
  const { binding, credential } = selection;
  let identity = { status: 'not_available', reason: 'CNB 本次仅验证 Git 凭证；不调用账号 API。' };
  if (binding.target.host === 'github.com') {
    const client = sourceClient(binding, createGitHubClient({ auth: { method: 'token', token: credential.password, host: 'github.com', repository: binding.target.repository }, transport: options.apiTransport }));
    const account = await client.getAuthenticatedUser(binding.target.repository);
    if (!account?.login || !Number.isSafeInteger(account.id)) authFail('auth_identity_invalid', 'GitHub 没有返回可验证的账号身份。');
    if (binding.account && account.login.toLowerCase() !== binding.account.toLowerCase()) authFail('auth_identity_changed', 'GitHub 返回账号与已选 gh 账号不一致。');
    if (binding.identity?.id && binding.identity.id !== account.id) authFail('auth_identity_changed', '凭证对应账号与已保存身份不同；先明确身份变更，不继续操作。');
    identity = { status: 'verified', login: account.login, id: account.id };
  }
  const verified = { ...binding, identity };
  return { ...selection, binding: verified, identity, fingerprint: hash({ binding: verified, revision: credential.revision, relationship: selection.relationship?.fingerprint }) };
}

export function publicSelection(selection) {
  if (!selection) return null;
  const { credential, ...publicData } = selection;
  return publicData;
}

/** Local inspection only: never turns a previous identity check into a new network claim. */
export async function inspectAuth(project, url, options = {}) {
  let target;
  try {
    target = authTarget(url);
    if (!target) return { status: 'not_applicable', network: 'not_performed' };
    const binding = await parseBinding(project, target);
    if (!binding) {
      const scope = await credentialOwner(project, options);
      return { status: 'needs_input', required: ['auth-method'], target, credentialFile: join(scope.directory, target.filename), network: 'not_performed' };
    }
    await credentialOwner(project, { ...options, expectedOwner: binding.owner, expectedRelationship: binding.relationship });
    const source = sourceLabel(binding);
    if (binding.file) {
      await protectedPath(binding.file, binding.owner);
      validateFileInfo(await lstat(binding.file), binding.file);
    }
    if (binding.state === 'ready') {
      const current = await readCredential(binding);
      if (binding.credentialRevision !== current.revision) authFail('auth_source_changed', `已选凭证已变化，需要重新验证：${source}`);
    }
    return { status: binding.state, target, method: binding.method, source, owner: binding.owner,
      account: binding.account, sourceRevision: binding.credentialRevision,
      previousIdentity: binding.identity, network: 'not_performed' };
  } catch (error) {
    return { status: 'invalid', target, code: error.code, message: error.code?.startsWith('auth_') || error.code?.startsWith('gh_') ? error.message : '无法检查已选来源；不回退。', network: 'not_performed' };
  }
}

export async function installAuth(project, selection) {
  if (selection.status !== 'selected') authFail('auth_not_selected', '鉴权来源尚未完成选择。');
  if (selection.binding.source === 'process') authFail('auth_session_only', '进程 Token 不能保存为持久 Git helper。');
  const saved = await persistBinding(project, { ...selection.binding, state: 'ready', credentialRevision: selection.credential.revision }, true);
  try { await verifyNativeHelper(project, saved.target, await readCredential(saved)); }
  catch (error) {
    await persistBinding(project, { ...saved, state: 'pending' });
    authFail(error.code || 'auth_helper_mismatch', `${error.message} 本机绑定已保留为待验证，未报告设置完成。`);
  }
  return { binding: saved, identity: selection.identity, persistentHelper: true, remoteRead: 'pending', pushPreflight: 'not_performed', pushed: false };
}

/** Identity and source validation also work for Issue-only tokens or a repository not created yet. */
export async function verifyAuth(project, url, options = {}) {
  const selected = await verifySelection(await authSelection(project, url, options), options);
  if (selected.status !== 'selected') return publicSelection(selected);
  if (options.apiOnly && selected.target.host !== 'github.com') authFail('auth_api_unsupported', '仅 API 验证适用于 GitHub；CNB 必须验证实际 Git 读取。');
  const fingerprint = hash({ selection: selected.fingerprint, apiOnly: Boolean(options.apiOnly) });
  if (options.apply && options.expectedFingerprint !== fingerprint) authFail('auth_plan_changed', '鉴权来源、身份或验证范围已变化，请重新验证并使用本次指纹。');
  if (!options.apiOnly) await inspectRemote(project, url, { ...options, ...gitAuthentication(project, options, selected) });
  const report = { fingerprint, remoteRead: options.apiOnly ? 'not_performed' : 'verified', apiPermissions: 'not_verified', pushPreflight: 'not_performed', pushed: false };
  if (options.apply) return { status: 'verified', ...await installAuth(project, selected), ...report };
  return { ...publicSelection(selected), ...report };
}

export async function helperCredential(project, request) {
  if (request.protocol !== 'https' || !request.path || !['github.com', 'cnb.cool'].includes(request.host)) authFail('auth_request_target', 'Git 请求未包含已支持主机及完整仓库路径。');
  const target = authTarget(`https://${request.host}/${request.path}`);
  const binding = await parseBinding(project, target);
  if (!binding || binding.state !== 'ready') authFail('auth_binding_required', `没有已完成的鉴权绑定：${target.url}。不使用其他账号。`);
  const credential = await readCredential(binding);
  if (binding.credentialRevision !== credential.revision) authFail('auth_source_changed', `已选凭证内容发生变化，请重新核验原来源：${binding.file || `gh:${binding.account}`}。`);
  if (request.username && ![credential.username, binding.identity?.login].includes(request.username)) authFail('auth_username_changed', 'Git 请求用户名与已选来源不一致。');
  return credential;
}

export function gitAuthentication(project, options = {}, prepared) {
  const cache = new Map(prepared ? [[prepared.target.key, prepared]] : []);
  const authReports = [];
  return { authReports, authForUrl: async url => {
    const target = authTarget(url);
    if (!target) return { env: {}, metadata: { method: 'existing' } };
    let selected = cache.get(target.key) || await authSelection(project, url, { ...options, allowSessionAuth: true });
    if (selected.status !== 'selected') authFail('auth_selection_required', `目标 ${target.url} 尚未完成鉴权选择或文件填写；先完成 setup。`);
    if (!cache.has(target.key)) { selected = await verifySelection(selected, options); cache.set(target.key, selected); }
    if (!authReports.some(report => report.binding.target.key === target.key)) authReports.push(publicSelection(selected));
    // Read the same bound source again for every Git operation; detect rotation during a plan.
    const current = await readCredential(selected.binding, options);
    if (current.revision !== selected.credential.revision) authFail('auth_source_changed', '选定凭证在本次操作中发生变化，请重新核验计划。');
    const settings = [['credential.helper', ''], ['credential.helper', helperCommand()], ['credential.useHttpPath', 'true'], ['credential.username', current.username], ['http.extraHeader', ''], ['http.followRedirects', 'false']];
    return { env: Object.assign({ GIT_SETUP_TRANSPORT_TOKEN: current.password, GIT_SETUP_TRANSPORT_USERNAME: current.username, GIT_SETUP_TRANSPORT_TARGET: target.url, GIT_ASKPASS: '', SSH_ASKPASS: '', GIT_TERMINAL_PROMPT: '0', GIT_CONFIG_COUNT: String(settings.length) }, ...settings.map(([key, value], index) => ({ [`GIT_CONFIG_KEY_${index}`]: key, [`GIT_CONFIG_VALUE_${index}`]: value }))), metadata: publicSelection(selected) };
  } };
}

export async function boundGitHubClient(project, target, options = {}) {
  const url = `https://${target.host}/${target.repository}`;
  let selection = await authSelection(project, url, { ...options, allowSessionAuth: true });
  // Preserve existing API-only environment configuration without migrating it or changing a bound source.
  if (selection.required?.includes('auth-method') && !options.authMethod) {
    const { values } = await loadEnvironment(project, { useGlobalConfig: options.useGlobalConfig });
    if (values.GIT_SETUP_GITHUB_AUTH) selection = await authSelection(project, url, { ...options, githubAuth: true, allowSessionAuth: true });
  }
  const selected = await verifySelection(selection, options);
  if (selected.status !== 'selected') authFail('auth_selection_required', 'GitHub API 目标尚未完成独立鉴权选择；不借用发起仓库或外仓账号。');
  return { ...sourceClient(selected.binding, createGitHubClient({ auth: { method: 'token', token: selected.credential.password, ...target }, transport: options.apiTransport })), authentication: publicSelection(selected) };
}
