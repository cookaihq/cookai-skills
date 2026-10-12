import { spawn } from 'node:child_process';
import { realpath, lstat, readFile, open, rename, rm, readdir } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import { devNull } from 'node:os';
import { auditSnapshot } from './audit.mjs';

const LOCAL_TIMEOUT = 15_000;
const READ_TIMEOUT = 60_000;
const TRANSFER_TIMEOUT = 300_000;
const MAX_OUTPUT = 8 * 1024 * 1024;
const pause = (ms) => new Promise((done) => setTimeout(done, ms));

function failure(code, message) {
  return Object.assign(new Error(redact(message)), { code });
}

export function redact(value) {
  return String(value)
    .replace(/(https?:\/\/)[^\s/@]+@/gi, '$1[redacted]@')
    .replace(/([?&](?:token|access_token|api_key|key|password)=)[^&\s]*/gi, '$1[redacted]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b/g, '[redacted]');
}

function transient(result) {
  if (result.timedOut) return true;
  const message = result.stderr;
  if (/authentication failed|permission denied|repository not found|requested URL returned error: (?:400|401|403|404)|could not read username|access denied/i.test(message)) return false;
  return /could not resolve|failed to connect|connection (?:reset|timed out|refused)|broken pipe|early EOF|TLS.*(?:failed|error)|SSL.*(?:failed|error)|requested URL returned error: (?:429|5\d\d)|remote end hung up|network is unreachable/i.test(message);
}

async function command(project, args, { timeout = LOCAL_TIMEOUT, env = {}, networkRead = false, onRetry } = {}) {
  // Ambient repository selectors can silently redirect `git -C` to another tree.
  const childEnv = { ...process.env, ...env, GIT_TERMINAL_PROMPT: '0', GCM_INTERACTIVE: 'never', LC_ALL: 'C', GIT_NO_REPLACE_OBJECTS: '1', GIT_GRAFT_FILE: devNull };
  for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_SHALLOW_FILE']) delete childEnv[key];
  const once = () => new Promise((done) => {
    let stdout = '', stderr = '', timedOut = false, overflow = false;
    const child = spawn('git', ['-C', project, ...args], { env: childEnv, windowsHide: true, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] });
    const kill = () => {
      if (process.platform !== 'win32') {
        try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
      } else {
        const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore', timeout: 5_000 });
        killer.on('error', () => child.kill('SIGKILL'));
      }
    };
    const timer = setTimeout(() => { timedOut = true; kill(); }, timeout);
    child.stdout.on('data', (chunk) => { stdout += chunk; if (stdout.length + stderr.length > MAX_OUTPUT) { overflow = true; kill(); } });
    child.stderr.on('data', (chunk) => { stderr += chunk; if (stdout.length + stderr.length > MAX_OUTPUT) { overflow = true; kill(); } });
    child.on('error', (error) => { clearTimeout(timer); done({ code: -1, stdout, stderr: error.message, timedOut, unavailable: error.code === 'ENOENT' }); });
    child.on('close', (code) => {
      clearTimeout(timer);
      for (const [key, value] of Object.entries(childEnv)) {
        if (/token|password|secret/i.test(key) && typeof value === 'string' && value.length >= 4) stderr = stderr.split(value).join('[redacted]');
      }
      done({ code: code ?? -1, stdout, stderr, timedOut, overflow });
    });
  });
  for (let attempt = 1; ; attempt++) {
    const result = await once();
    if (result.unavailable) throw failure('git_unavailable', 'Git 无法执行。请按实际系统安装 Git：macOS 使用 xcode-select --install；Windows 使用 winget install --id Git.Git -e；Debian/Ubuntu 使用 sudo apt-get install git。');
    if (!networkRead || !transient(result) || attempt === 3 || result.code === 0) return result;
    const event = { attempt: attempt + 1, attempts: 3, delayMs: 1000 * 2 ** (attempt - 1), reason: redact(result.timedOut ? 'Git read timed out' : result.stderr.trim()) };
    if (onRetry) onRetry(event); else console.error(JSON.stringify({ gitReadRetry: event }));
    await pause(event.delayMs);
  }
}

async function required(project, args, options) {
  const result = await command(project, args, options);
  if (result.code !== 0 || result.overflow) throw failure(result.timedOut ? 'git_timeout' : 'git_failed', result.overflow ? 'Git 输出超过限制，请缩小检查范围。' : result.stderr.trim() || `Git exited ${result.code}`);
  return result.stdout.trim();
}

async function runtime(project, options = {}) {
  if (Number(process.versions.node.split('.')[0]) < 20) throw failure('node_version', '需要 Node.js >= 20。请安装受支持版本后重试。');
  const version = await required(project, ['--version'], options);
  const match = version.match(/git version (\d+)\.(\d+)/);
  if (!match || Number(match[1]) < 2 || (Number(match[1]) === 2 && Number(match[2]) < 29)) throw failure('git_version', `需要 Git >= 2.29（fetch --no-write-fetch-head）。当前：${version}`);
  return version;
}

async function rootOf(project, options = {}) {
  const root = await command(project, ['rev-parse', '--show-toplevel'], options);
  if (root.code !== 0) throw failure('not_worktree', '目标目录不是可操作的 Git 工作区。');
  return realpath(root.stdout.trim());
}

async function configValues(project, key, options = {}) {
  const result = await command(project, ['config', '--null', '--get-all', key], options);
  if (result.code === 1) return [];
  if (result.code !== 0) throw failure('git_config', result.stderr);
  return result.stdout.split('\0').filter(Boolean);
}

async function validateRef(project, ref, options = {}) {
  if (typeof ref !== 'string' || !/^refs\/(heads|tags)\/.+/.test(ref)) throw failure('invalid_ref', '引用必须是 refs/heads/<分支> 或 refs/tags/<标签>，不能使用省略名、通配符或删除规则。');
  if ((await command(project, ['check-ref-format', ref], options)).code !== 0) throw failure('invalid_ref', `非法 Git 引用：${ref}`);
  return ref;
}

async function validateRefs(project, refs, options = {}) {
  if (!Array.isArray(refs) || refs.length === 0 || new Set(refs).size !== refs.length) throw failure('invalid_refs', '必须提供不重复的完整分支/标签引用清单。');
  for (const ref of refs) await validateRef(project, ref, options);
  return refs;
}

function validateName(name) {
  if (typeof name !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) throw failure('invalid_remote', '远端名称只能包含字母、数字、点、下划线和连字符，并以字母或数字开头。');
  return name;
}

export function validateUrl(url) {
  if (typeof url !== 'string' || !url || /[\r\n\0]/.test(url) || url.startsWith('-') || url.includes('::')) throw failure('invalid_url', '远端地址为空、包含控制字符或使用了不支持的 Git remote helper。');
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) {
    let parsed;
    try { parsed = new URL(url); } catch { throw failure('invalid_url', '远端 URL 无法解析。'); }
    if (!['https:', 'http:', 'ssh:', 'git:', 'file:'].includes(parsed.protocol)) throw failure('invalid_url', '不支持此 Git 传输协议。');
    if (parsed.password || (['https:', 'http:'].includes(parsed.protocol) && parsed.username) || parsed.search || parsed.hash) throw failure('credential_url', '远端 URL 不得含凭证、查询参数或片段；凭证由独立鉴权来源提供。');
  }
  return url;
}

async function endpoint(project, name, push, options = {}) {
  validateName(name);
  const urls = (await required(project, ['remote', 'get-url', ...(push ? ['--push'] : []), '--all', name], options)).split('\n').filter(Boolean);
  if (urls.length !== 1) throw failure('multiple_remote_urls', `远端 ${name} 必须只有一个明确的${push ? '推送' : '读取'}地址。`);
  validateUrl(urls[0]);
  const mirror = await command(project, ['config', '--bool', '--get', `remote.${name}.mirror`], options);
  if (![0, 1].includes(mirror.code)) throw failure('git_config', mirror.stderr);
  if (mirror.stdout.trim() === 'true') throw failure('mirror_remote', `远端 ${name} 配置了 mirror；请先明确迁移该配置，不能通过本入口执行。`);
  return urls[0];
}

async function readRefs(project, url, refs, options = {}) {
  const output = await required(project, ['ls-remote', '--refs', '--', url, ...refs], { ...await authenticated(options, url), timeout: READ_TIMEOUT, networkRead: true });
  const found = Object.fromEntries(refs.map((ref) => [ref, null]));
  for (const line of output.split('\n')) {
    if (!line) continue;
    const [sha, ref] = line.split('\t');
    if (Object.hasOwn(found, ref)) {
      if (!/^[a-f0-9]{40,64}$/.test(sha)) throw failure('invalid_remote_output', '远端返回了非法对象 ID。');
      found[ref] = sha;
    }
  }
  return found;
}

async function authenticated(options, url) {
  if (!options.authForUrl) return options;
  const auth = await options.authForUrl(url);
  if (!auth || typeof auth.env !== 'object') throw failure('authentication_configuration', 'authForUrl 必须返回含 env 的对象。');
  return { ...options, env: { ...options.env, ...auth.env } };
}

/** Reads advertised refs without fetching objects or installing a remote. */
export async function inspectRemote(project, url, options = {}) {
  await runtime(project, options);
  validateUrl(url);
  const effective = await required(project, ['ls-remote', '--get-url', '--', url], options);
  if (effective !== url) throw failure('url_rewrite', 'Git URL 重写改变了实际目标，请先明确最终地址。');
  const output = await required(project, ['ls-remote', '--symref', '--', url], { ...await authenticated(options, url), timeout: READ_TIMEOUT, networkRead: true });
  const refs = {};
  let defaultRef = null;
  for (const line of output.split('\n').filter(Boolean)) {
    const [value, ref] = line.split('\t');
    if (value.startsWith('ref: ')) {
      if (ref === 'HEAD') defaultRef = value.slice(5);
    } else {
      if (!/^[a-f0-9]{40,64}$/.test(value) || !ref) throw failure('invalid_remote_output', '远端引用输出无法验证。');
      if (!ref.endsWith('^{}')) refs[ref] = value;
    }
  }
  return { url, readable: true, defaultRef, refs, empty: Object.keys(refs).length === 0 };
}

async function warningsAt(project, refs, options = {}) {
  const warnings = [];
  if (await required(project, ['rev-parse', '--is-shallow-repository'], options) === 'true') warnings.push({ code: 'shallow_history', message: '当前仓库为浅克隆，尚未证明完整历史已同步。' });
  for (const ref of refs) {
    const tree = await command(project, ['ls-tree', '-r', ref], options);
    if (tree.code !== 0) continue;
    if (/^160000 /m.test(tree.stdout)) warnings.push({ code: 'submodule_content', ref, message: '此引用包含子模块；本入口只同步 gitlink，不复制子模块仓库。' });
    const lfs = await command(project, ['grep', '-l', '-e', 'filter=lfs', '-e', '^version https://git-lfs.github.com/spec/v1$', ref, '--', ':/**/.gitattributes', ':/.gitattributes', '*'], options);
    if (lfs.code === 0) warnings.push({ code: 'lfs_content', ref, message: '此引用检测到 Git LFS 规则或指针；本入口未传输 LFS 对象。' });
    else if (lfs.code !== 1) throw failure('content_inspection', lfs.stderr || '无法完成 LFS 内容检查。');
    if (options.historyContent) {
      if (lfs.code !== 0) {
        const historyLfs = await command(project, ['log', '--format=%H', '-1', '-m', '--no-ext-diff', '--no-textconv', '--no-renames', '-G', 'filter=lfs|^version https://git-lfs.github.com/spec/v1$', ref, '--'], options);
        if (historyLfs.code !== 0 || historyLfs.overflow) warnings.push({ code: 'content_history_unverified', ref, message: '未能在读取预算内完成历史 LFS 检查，不能确认所有内容已同步。' });
        else if (historyLfs.stdout.trim()) warnings.push({ code: 'lfs_content', ref, message: '历史中检测到 Git LFS 规则或指针；本入口未传输历史 LFS 对象。' });
      }
      if (!/^160000 /m.test(tree.stdout)) {
        const historyModes = await command(project, ['log', '--format=', '--raw', '-m', '--no-ext-diff', '--no-textconv', '--no-renames', ref, '--'], options);
        if (historyModes.code !== 0 || historyModes.overflow) warnings.push({ code: 'content_history_unverified', ref, message: '未能在读取预算内完成历史子模块检查，不能确认所有内容已同步。' });
        else if (/^:(?:160000 [0-7]{6}|[0-7]{6} 160000) /m.test(historyModes.stdout)) warnings.push({ code: 'submodule_content', ref, message: '历史中含子模块 gitlink；本入口不复制历史子模块仓库。' });
      }
    }
  }
  return warnings;
}

export async function inspect(project, options = {}) {
  const requested = await realpath(project);
  const gitVersion = await runtime(requested, options);
  const probe = await command(requested, ['rev-parse', '--show-toplevel'], options);
  if (probe.code !== 0) {
    const bare = await command(requested, ['rev-parse', '--is-bare-repository'], options);
    if (bare.code !== 0 && !/not a git repository/i.test(probe.stderr)) throw failure('git_inspection', probe.stderr || 'Git 仓库状态无法确定。');
    if (bare.code !== 0) {
      let dotGit;
      try { dotGit = await lstat(join(requested, '.git')); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (dotGit) throw failure('invalid_git_directory', '目录已有 .git，但 Git 无法识别；不会重新初始化或覆盖。');
    }
    return { gitVersion, project: requested, initialized: bare.code === 0, bare: bare.stdout.trim() === 'true', type: bare.stdout.trim() === 'true' ? 'bare' : 'uninitialized' };
  }
  const root = await realpath(probe.stdout.trim());
  const gitDir = await required(root, ['rev-parse', '--absolute-git-dir'], options);
  const commonDir = resolve(root, await required(root, ['rev-parse', '--git-common-dir'], options));
  const superproject = await required(root, ['rev-parse', '--show-superproject-working-tree'], options);
  const head = await command(root, ['rev-parse', '--verify', 'HEAD'], options);
  const branch = await command(root, ['symbolic-ref', '--quiet', '--short', 'HEAD'], options);
  const names = (await required(root, ['remote'], options)).split('\n').filter(Boolean);
  const remotes = [];
  for (const name of names) {
    const fetch = await command(root, ['remote', 'get-url', '--all', name], options);
    const push = await command(root, ['remote', 'get-url', '--push', '--all', name], options);
    remotes.push({ name, fetch: redact(fetch.stdout.trim()).split('\n').filter(Boolean), push: redact(push.stdout.trim()).split('\n').filter(Boolean), mirror: (await configValues(root, `remote.${name}.mirror`, options)).includes('true') });
  }
  const statusResult = await command(root, ['status', '--porcelain=v1', '-z', '--untracked-files=normal'], options);
  if (statusResult.code !== 0 || statusResult.overflow) throw failure('git_status', statusResult.stderr || '无法完整读取工作区状态。');
  const upstream = await command(root, ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'], options);
  return {
    gitVersion, project: requested, root, initialized: true, bare: false,
    type: superproject ? 'submodule' : resolve(gitDir) !== commonDir ? 'worktree' : 'repository',
    insideParent: requested !== root, gitDir, commonDir, superproject: superproject || null,
    head: head.code === 0 ? head.stdout.trim() : null, branch: branch.code === 0 ? branch.stdout.trim() : null,
    upstream: upstream.code === 0 ? upstream.stdout.trim() : null,
    author: { name: (await configValues(root, 'user.name', options)).at(-1) ?? null, email: (await configValues(root, 'user.email', options)).at(-1) ?? null },
    remotes, status: redact(statusResult.stdout).split('\0').filter(Boolean),
    pushDefault: (await configValues(root, 'push.default', options)).at(-1) ?? null,
    remotePushDefault: (await configValues(root, 'remote.pushDefault', options)).at(-1) ?? null,
    contentWarnings: head.code === 0 ? await warningsAt(root, ['HEAD'], options) : [],
  };
}

export async function init(project, options = {}) {
  const root = await realpath(project);
  const state = await inspect(root, options);
  if (state.initialized) throw failure(state.insideParent ? 'inside_parent_repository' : 'already_initialized', '目录已经属于 Git 仓库；初始化不会修改现有仓库或创建嵌套仓库。');
  const { branch, authorName, authorEmail } = options;
  if (!branch || !authorName || !authorEmail || /[\r\n\0]/.test(`${authorName}${authorEmail}`)) throw failure('initialization_inputs', '初始化必须提供初始分支、提交作者姓名和邮箱。');
  await validateRef(root, `refs/heads/${branch}`, options);
  await required(root, ['init', `--initial-branch=${branch}`], options);
  await completeInitialization(root, options);
  return { status: 'initialized', committed: false, state: await inspect(root, options) };
}

/** Fill explicitly chosen repository author fields, including post-clone and interrupted init. */
export async function completeInitialization(project, options = {}) {
  const state = await inspect(project, options);
  if (state.branch !== options.branch || state.type === 'worktree') throw failure('setup_initialization_changed', '选定分支或仓库类型已变化，请重新明确作者配置范围。');
  const fields = [['user.name', options.authorName], ['user.email', options.authorEmail]];
  await updateLocalConfig(project, options, async file => {
    for (const [key, value] of fields) {
      if (!value || /[\r\n\0]/.test(value)) throw failure('initialization_inputs', '初始化作者姓名和邮箱必须是明确的单行值。');
      const current = await command(project, ['config', '--file', file, '--includes', '--get-all', key], options);
      if (current.code === 1) await required(project, ['config', '--file', file, key, value], options);
      else if (current.code !== 0 || current.stdout.trim() !== value) throw failure('setup_author_changed', '仓库已写入不同作者；不会用旧初始化选择覆盖，请核对实际身份。');
    }
  });
}

export async function addRemote(project, options = {}) {
  await runtime(project, options);
  const root = await rootOf(project, options);
  const name = validateName(options.name);
  const url = validateUrl(options.url);
  const current = await command(root, ['remote', 'get-url', '--all', name], options);
  if (current.code === 0) {
    const existing = await endpoint(root, name, false, options);
    const push = await endpoint(root, name, true, options);
    if (existing !== url || push !== url) throw failure('remote_conflict', `远端 ${name} 已存在且地址不同；未改写原配置。`);
    return { status: 'unchanged', name, url };
  }
  const effective = await required(root, ['ls-remote', '--get-url', '--', url], options);
  if (effective !== url) throw failure('url_rewrite', 'Git URL 重写配置改变了实际目标，请先明确最终地址。');
  await required(root, ['remote', 'add', name, url], options);
  const actual = await endpoint(root, name, false, options);
  if (actual !== url) throw failure('url_rewrite', 'Git URL 重写配置改变了实际目标，请检查 url.*.insteadOf 配置。');
  return { status: 'added', name, url };
}

/** Opaque state hash includes effective config, all refs and the index; no secret values escape. */
export async function setupFacts(project, options = {}) {
  const configuration = await required(project, ['config', '--null', '--list'], options);
  const refs = await required(project, ['for-each-ref', '--format=%(refname) %(objectname) %(symref)'], options);
  const index = await required(project, ['ls-files', '--stage', '-z'], options);
  const branches = await command(project, ['config', '--get-regexp', '^branch\\..*\\.(remote|pushremote|merge)$'], options);
  if (![0, 1].includes(branches.code)) throw failure('git_config', branches.stderr);
  return {
    fingerprint: createHash('sha256').update(JSON.stringify({ configuration, refs, index })).digest('hex'),
    branches: redact(branches.stdout).trim().split('\n').filter(Boolean),
    trackingRefs: refs.split('\n').filter(line => line.startsWith('refs/remotes/')),
    worktrees: redact(await required(project, ['worktree', 'list', '--porcelain'], options)),
  };
}

async function updateLocalConfig(root, options, update) {
  const common = resolve(root, await required(root, ['rev-parse', '--git-common-dir'], options));
  const file = join(common, 'config');
  if ((await lstat(file)).isSymbolicLink()) throw failure('setup_complex_remote', 'Git config 是符号链接，需先明确配置归属。');
  const lockPath = `${file}.lock`;
  let lock;
  try {
    try { lock = await open(lockPath, 'wx', (await lstat(file)).mode & 0o777); }
    catch (error) { if (error.code === 'EEXIST') throw failure('setup_config_busy', 'Git config.lock 已存在；先核对是否有正在执行或中断的 Git 操作。'); throw error; }
    const raw = await readFile(file);
    if (options.expectedState && (await setupFacts(root, options)).fingerprint !== options.expectedState) throw failure('setup_plan_changed', 'Git 状态在替换前变化，未替换。');
    await lock.writeFile(raw); await lock.close(); lock = true;
    await update(lockPath);
    if (!(await readFile(file)).equals(raw)) throw failure('setup_plan_changed', 'Git 配置在替换期间变化，未覆盖。');
    await rename(lockPath, file);
    lock = false; // rename released this lock path; a later writer may now own a new one.
  } finally {
    if (lock) { if (lock !== true) await lock.close(); await rm(lockPath, { force: true }); }
  }
}

/** Reject any effective remote address not solely owned by the common config file. */
export async function assertLocalRemote(project, name, options = {}) {
  validateName(name);
  const common = resolve(project, await required(project, ['rev-parse', '--git-common-dir'], options));
  for (const field of ['url', 'pushurl']) {
    const direct = await command(project, ['config', '--file', join(common, 'config'), '--null', '--get-all', `remote.${name}.${field}`], options);
    if (![0, 1].includes(direct.code)) throw failure('git_config', direct.stderr);
    if (JSON.stringify(direct.stdout.split('\0').filter(Boolean)) !== JSON.stringify(await configValues(project, `remote.${name}.${field}`, options))) throw failure('setup_complex_remote', `远端 ${name} 地址来自包含文件、全局或 Worktree 配置；需先明确要修改的来源。`);
  }
}

/** Replace a single locally owned remote in one atomic Git config update. */
export async function replaceRemote(project, options = {}) {
  const root = await rootOf(project, options);
  const name = validateName(options.name), url = validateUrl(options.url);
  const before = { fetch: await endpoint(root, name, false, options), push: await endpoint(root, name, true, options) };
  if (before.fetch !== options.expectedFetch || before.push !== options.expectedPush) throw failure('setup_plan_changed', '远端旧地址已变化，未替换。');
  if (before.fetch === url && before.push === url) return { status: 'unchanged', name, url };
  await updateLocalConfig(root, options, async file => {
    await assertLocalRemote(root, name, options);
    await required(root, ['config', '--file', file, '--replace-all', `remote.${name}.url`, url], options);
    if ((await configValues(root, `remote.${name}.pushurl`, options)).length) await required(root, ['config', '--file', file, '--replace-all', `remote.${name}.pushurl`, url], options);
  });
  return { status: 'replaced', name, url };
}

/** Native clone owns creation and checkout. Never retry it or remove a partial directory. */
export async function cloneInto(project, options = {}) {
  const root = await realpath(project);
  const state = await inspect(root, options);
  if (state.initialized || (await readdir(root)).length) throw failure('clone_directory_changed', '克隆目录不再为空，未覆盖任何文件。');
  const name = validateName(options.name), url = validateUrl(options.url);
  await validateRef(root, `refs/heads/${options.branch}`, options);
  const auth = await authenticated(options, url);
  await required(root, ['-c', `core.hooksPath=${devNull}`, 'clone', '--no-local', '--no-recurse-submodules', `--origin=${name}`, `--branch=${options.branch}`, '--', url, '.'], { ...auth, env: { ...auth.env, GIT_LFS_SKIP_SMUDGE: '1' }, timeout: TRANSFER_TIMEOUT });
  const head = await required(root, ['rev-parse', 'HEAD'], options);
  if (head !== options.expectedHead) throw failure('remote_changed', '克隆取得的 HEAD 与计划不一致；保留已下载内容，请重新检查远端及本地文件。');
  return { status: 'cloned', project: root, head };
}

async function fetchRef(project, url, ref, destination, expected, options) {
  await required(project, ['-c', 'fetch.prune=false', '-c', 'fetch.pruneTags=false', 'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules', '--no-auto-gc', '--', url, `${ref}:${destination}`], { ...await authenticated(options, url), timeout: TRANSFER_TIMEOUT, networkRead: true });
  const actual = await required(project, ['rev-parse', '--verify', destination], options);
  if (actual !== expected) throw failure('remote_changed', `读取期间 ${ref} 已改变；停止使用旧计划。`);
}

async function pushOne(project, url, ref, source, expected, options) {
  // Never retry a write. Even failed or interrupted pushes are read back first.
  const write = await command(project, ['push', '--porcelain', '--no-force', '--no-follow-tags', '--recurse-submodules=no', '--', url, `${source}:${ref}`], { ...await authenticated(options, url), timeout: TRANSFER_TIMEOUT });
  try {
    const actual = (await readRefs(project, url, [ref], options))[ref];
    if (actual === expected) return { ref, expected, actual, status: 'verified', writeReportedSuccess: write.code === 0 };
    return { ref, expected, actual, status: write.code === 0 || write.timedOut || transient(write) ? 'uncertain' : 'failed', error: redact(write.stderr.trim() || '远端引用与期望对象不一致。') };
  } catch (error) {
    return { ref, expected, actual: null, status: 'uncertain', error: redact(error.message) };
  }
}

function summarize(results, warnings, extra = {}) {
  const refsComplete = results.length > 0 && results.every((item) => item.status === 'verified');
  return { status: refsComplete ? warnings.length ? 'git_refs_verified_content_incomplete' : 'verified' : 'incomplete', complete: refsComplete && warnings.length === 0, refsComplete, contentWarnings: warnings, results, ...extra };
}

export async function publish(project, options = {}) {
  await runtime(project, options);
  const root = await rootOf(project, options);
  const refs = await validateRefs(root, options.refs, options);
  const url = await endpoint(root, options.remote, true, options);
  const local = {};
  for (const ref of refs) local[ref] = await required(root, ['show-ref', '--verify', '--hash', ref], options);
  if (options.expectedRefs && (Object.keys(options.expectedRefs).length !== refs.length || refs.some((ref) => options.expectedRefs[ref] !== local[ref]))) {
    throw failure('reviewed_refs_changed', '本地引用与发布前检查的快照不一致；请重新检查实际发布对象。');
  }
  const warnings = await warningsAt(root, refs, { ...options, historyContent: true });
  const before = await readRefs(root, url, refs, options);
  const results = [];
  for (const ref of refs) {
    if (before[ref] === local[ref]) results.push({ remote: options.remote, ref, expected: local[ref], actual: before[ref], status: 'verified', unchanged: true });
    else if (ref.startsWith('refs/tags/') && before[ref]) results.push({ remote: options.remote, ref, expected: local[ref], actual: before[ref], status: 'conflict', error: '同名标签指向不同对象；未覆盖。' });
    else results.push({ remote: options.remote, ...await pushOne(root, url, ref, local[ref], local[ref], options) });
  }
  return summarize(results, warnings, { source: 'local', snapshots: local, committed: false, upstreamChanged: false });
}

export async function sync(project, config, options = {}) {
  await runtime(project, options);
  const root = await rootOf(project, options);
  const plan = config?.sync;
  if (!plan || !Array.isArray(plan.targets) || !plan.targets.length || new Set(plan.targets).size !== plan.targets.length || plan.targets.includes(plan.source)) throw failure('sync_configuration', '同步配置必须提供一个来源及不重复、不同于来源的目标列表。');
  const refs = await validateRefs(root, plan.refs, options);
  const sourceUrl = await endpoint(root, plan.source, false, options);
  const targets = [];
  for (const name of [plan.source, ...plan.targets]) {
    const url = name === plan.source ? sourceUrl : await endpoint(root, name, true, options);
    const declared = config.remotes?.[name];
    if (declared && declared !== url) throw failure('remote_configuration_changed', `远端 ${name} 的实际地址与项目配置不同。`);
    if (name !== plan.source) targets.push({ name, url });
  }
  if (new Set([sourceUrl, ...targets.map((target) => target.url)]).size !== targets.length + 1) throw failure('duplicate_sync_endpoint', '来源和同步目标必须对应不同的远端地址。');
  const snapshot = await readRefs(root, sourceUrl, refs, options);
  for (const ref of refs) if (!snapshot[ref]) throw failure('missing_source_ref', `来源远端没有 ${ref}；不会用本地同名引用代替。`);
  if (options.dryRun) {
    const results = [];
    for (const target of targets) {
      try {
        const state = await readRefs(root, target.url, refs, options);
        for (const ref of refs) {
          const expected = snapshot[ref], actual = state[ref];
          let status = actual === expected ? 'verified' : !actual ? 'planned' : ref.startsWith('refs/tags/') ? 'conflict' : 'needs_history_fetch';
          if (status === 'needs_history_fetch') {
            const history = await command(root, ['merge-base', '--is-ancestor', actual, expected], options);
            if (history.code === 0) status = 'planned';
            else if (history.code === 1) status = 'conflict';
          }
          results.push({ remote: target.name, ref, expected, actual, status });
        }
      } catch (error) {
        for (const ref of refs) results.push({ remote: target.name, ref, expected: snapshot[ref], actual: null, status: 'failed', error: redact(error.message) });
      }
    }
    const finalSource = await readRefs(root, sourceUrl, refs, options);
    return { status: 'planned', dryRun: true, complete: false, source: plan.source, sourceStable: refs.every((ref) => snapshot[ref] === finalSource[ref]), snapshots: snapshot, audit: { status: 'not_performed', complete: false, reason: 'dry_run' }, contentWarnings: [{ code: 'content_unverified', message: '只读计划不获取对象，尚未检查完整历史、秘密、大文件、LFS 或子模块内容。' }], results };
  }
  const prefix = `refs/git-setup/${randomUUID()}`;
  const temporary = [];
  const results = [];
  let warnings = [], sourceStable = true;
  try {
    for (let index = 0; index < refs.length; index++) {
      const local = `${prefix}/source/${index}`;
      temporary.push(local);
      await fetchRef(root, sourceUrl, refs[index], local, snapshot[refs[index]], options);
    }
    const audit = auditSnapshot(root, snapshot);
    if (!audit.complete) {
      return { status: 'audit_blocked', complete: false, refsComplete: false, source: plan.source, snapshots: snapshot, audit, results: targets.flatMap((target) => refs.map((ref) => ({ remote: target.name, ref, expected: snapshot[ref], status: 'blocked', reason: 'history_audit', written: false }))), contentWarnings: [], message: '来源历史检查未通过；所有目标均未写入。' };
    }
    warnings = await warningsAt(root, refs.map((_, index) => `${prefix}/source/${index}`), { ...options, historyContent: true });
    for (const target of targets) {
      let targetRefs;
      try { targetRefs = await readRefs(root, target.url, refs, options); }
      catch (error) {
        for (const ref of refs) results.push({ remote: target.name, ref, expected: snapshot[ref], actual: null, status: 'failed', error: redact(error.message) });
        continue;
      }
      for (let index = 0; index < refs.length; index++) {
        const ref = refs[index], expected = snapshot[ref], actual = targetRefs[ref];
        if (actual === expected) { results.push({ remote: target.name, ref, expected, actual, status: 'verified', unchanged: true }); continue; }
        if (actual && ref.startsWith('refs/tags/')) { results.push({ remote: target.name, ref, expected, actual, status: 'conflict', error: '同名标签对象不同；未覆盖。' }); continue; }
        try {
          const currentSource = await readRefs(root, sourceUrl, refs, options);
          if (refs.some((item) => currentSource[item] !== snapshot[item])) { sourceStable = false; throw failure('source_changed', '来源引用已改变；未继续写入旧快照。'); }
          if (actual) {
            const targetLocal = `${prefix}/target/${targets.indexOf(target)}/${index}`;
            temporary.push(targetLocal);
            await fetchRef(root, target.url, ref, targetLocal, actual, options);
            const ancestry = await command(root, ['merge-base', '--is-ancestor', actual, expected], options);
            if (ancestry.code === 1) { results.push({ remote: target.name, ref, expected, actual, status: 'conflict', error: '目标分支无法快进到来源提交；未覆盖目标历史。' }); continue; }
            if (ancestry.code !== 0) throw failure('history_comparison', ancestry.stderr);
          }
          results.push({ remote: target.name, ...await pushOne(root, target.url, ref, expected, expected, options) });
        } catch (error) {
          results.push({ remote: target.name, ref, expected, actual, status: 'failed', error: redact(error.message) });
        }
      }
    }
    try {
      const finalSource = await readRefs(root, sourceUrl, refs, options);
      sourceStable = sourceStable && refs.every((ref) => finalSource[ref] === snapshot[ref]);
    } catch { sourceStable = false; }
    const summary = summarize(results, warnings, { source: plan.source, snapshots: snapshot, sourceStable, audit });
    if (!sourceStable) { summary.complete = false; summary.status = 'source_changed_or_unreadable'; }
    return summary;
  } finally {
    for (const ref of temporary) await required(root, ['update-ref', '-d', ref], options);
  }
}
