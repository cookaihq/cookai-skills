import * as credentialFs from 'node:fs/promises';
import * as credentialPath from 'node:path';
import { execFile as credentialExecFile } from 'node:child_process';
import { promisify as credentialPromisify } from 'node:util';
import { authTarget, parseCredentialFile, parseCredentialSources, parseCredentialRequest } from './credential-data.mjs';
import { gitConfiguration } from './git-trust.mjs';

const credentialExec = credentialPromisify(credentialExecFile);
const helperFail = (code, message) => { throw Object.assign(new Error(message), { code }); };

export function helperEnvironment() {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', GH_PROMPT_DISABLED: '1', GCM_INTERACTIVE: 'never', LC_ALL: 'C' };
  for (const key of Object.keys(env)) if (/^GIT_(?:DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG|TRACE|CURL_VERBOSE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES)/.test(key)
      || /^(?:GH_TOKEN|GITHUB_TOKEN|NODE_OPTIONS|NODE_PATH)$/.test(key)) delete env[key];
  return { ...env, ...gitConfiguration() };
}

async function helperGit(binding, root, args, optional = false) {
  try { return (await credentialExec(binding.git, ['-C', root, ...args], { env: helperEnvironment(), encoding: 'utf8', timeout: 15000, maxBuffer: 2 * 1024 * 1024, windowsHide: true })).stdout; }
  catch (error) {
    if (optional && error.code === 1) return null;
    helperFail('auth_helper_git', '无法核对 Git 配置或仓库关系；重新检查本机接入。');
  }
}

async function helperCommon(binding, root) {
  const directory = (await helperGit(binding, root, ['rev-parse', '--git-common-dir'])).trim();
  return credentialFs.realpath(credentialPath.resolve(root, directory));
}

async function helperPlainFile(root, file, { optional = false, maximum = 262144, privateFile = false } = {}) {
  const rel = credentialPath.relative(root, file);
  if (!rel || credentialPath.isAbsolute(rel) || rel === '..' || rel.startsWith('..' + credentialPath.sep)) helperFail('auth_file_outside_owner', '文件不在已确认主检出内。');
  if (credentialPath.relative(await credentialFs.realpath(root), root)) helperFail('auth_file_symlink', '主检出路径已变化。');
  let cursor = root;
  const parts = rel.split(credentialPath.sep);
  for (const [index, part] of parts.entries()) {
    cursor = credentialPath.join(cursor, part);
    let info;
    try { info = await credentialFs.lstat(cursor); }
    catch (error) { if (optional && error.code === 'ENOENT') return null; throw error; }
    if (info.isSymbolicLink()) helperFail('auth_file_symlink', '来源路径经过符号链接。');
    if (index < parts.length - 1) {
      if (!info.isDirectory()) helperFail('auth_file_invalid', '来源父路径不是目录。');
      if (privateFile && process.platform !== 'win32' && (info.mode & 0o777) !== 0o700) helperFail('auth_directory_permissions', '凭证目录权限必须为 700。');
      continue;
    }
    if (!info.isFile() || info.nlink !== 1 || info.size > maximum) helperFail('auth_file_invalid', '来源必须是单链接普通小文件。');
    if (privateFile && process.platform !== 'win32' && (info.mode & 0o777) !== 0o600) helperFail('auth_file_permissions', '凭证文件权限必须为 600。');
    const handle = await credentialFs.open(file, 'r');
    try {
      const current = await handle.stat();
      if (info.dev !== current.dev || info.ino !== current.ino) helperFail('auth_file_changed', '读取前来源实体发生变化。');
      const raw = await handle.readFile('utf8');
      if (Buffer.byteLength(raw) > maximum) helperFail('auth_file_invalid', '来源文件超过读取上限。');
      return raw;
    } finally { await handle.close(); }
  }
}

async function helperPrivateFile(binding, file, options) {
  const raw = await helperPlainFile(binding.owner.main, file, options);
  if (raw === null) return null;
  const rel = credentialPath.relative(binding.owner.main, file).replaceAll('\\', '/');
  if ((await helperGit(binding, binding.owner.main, ['ls-files', '--', rel])).trim()
      || await helperGit(binding, binding.owner.main, ['check-ignore', '-q', '--', rel], true) === null) helperFail('auth_file_not_private', '来源须未跟踪且实际被 Git 忽略。');
  return raw;
}

async function helperRelationship(binding) {
  if (await helperCommon(binding, process.cwd()) !== binding.repository.commonDir
      || await helperCommon(binding, binding.repository.main) !== binding.repository.commonDir
      || await helperCommon(binding, binding.owner.main) !== binding.owner.commonDir) helperFail('auth_owner_changed', '当前检出或凭证主检出不属于已配置仓库。');
  if (binding.repository.commonDir === binding.owner.commonDir) return;
  if (binding.registration.kind === 'independent') {
    const registry = credentialPath.resolve(binding.owner.main, binding.registration.registry);
    const text = await helperPlainFile(binding.owner.main, registry);
    const entries = text.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#'));
    if (entries.filter(entry => entry === binding.registration.path).length !== 1
        || credentialPath.resolve(binding.owner.main, binding.registration.path) !== binding.repository.main
        || (await helperGit(binding, binding.repository.main, ['rev-parse', '--show-superproject-working-tree'])).trim()) helperFail('auth_owner_changed', '独立仓登记已变化；不借用其他仓库凭证。');
    return;
  }
  let child = binding.repository.main;
  for (let depth = 0; depth < 16; depth++) {
    const parent = (await helperGit(binding, child, ['rev-parse', '--show-superproject-working-tree'])).trim();
    if (!parent) break;
    const manager = await credentialFs.realpath(parent);
    const rel = credentialPath.relative(manager, child).replaceAll('\\', '/');
    if (!rel || rel.startsWith('../') || credentialPath.isAbsolute(rel)) break;
    await helperPlainFile(manager, credentialPath.join(manager, '.gitmodules'));
    const paths = (await helperGit(binding, manager, ['config', '-z', '--file', '.gitmodules', '--get-regexp', '^submodule\\..*\\.path$'])).split('\0').filter(Boolean);
    const matching = paths.filter(item => item.slice(item.indexOf('\n') + 1) === rel);
    const index = await helperGit(binding, manager, ['ls-files', '--stage', '-z', '--', rel]);
    if (matching.length !== 1 || !new RegExp('^160000 [a-f0-9]+ 0\\t' + rel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\0$').test(index)) break;
    const key = matching[0].slice(0, matching[0].indexOf('\n'));
    const moduleName = key.slice(10, -5);
    const storage = (await helperGit(binding, manager, ['rev-parse', '--git-path', `modules/${moduleName}`])).trim();
    if (await credentialFs.realpath(credentialPath.resolve(manager, storage)) !== await helperCommon(binding, child)) break;
    if (await helperCommon(binding, manager) === binding.owner.commonDir && manager === binding.owner.main) return;
    child = manager;
  }
  helperFail('auth_owner_changed', '子模块登记与凭证管理仓不一致。');
}

export async function projectCredential(binding, text) {
  const request = parseCredentialRequest(text);
  if (!binding || binding.version !== 1 || !Array.isArray(binding.targets) || !binding.targets.length
      || binding.targets.length > 200 || !credentialPath.isAbsolute(binding.git)
      || !binding.repository?.main || !binding.repository?.commonDir || !binding.owner?.main || !binding.owner?.commonDir) helperFail('auth_helper_binding', '本机接入参数无效。');
  if (!binding.targets.some(url => authTarget(url)?.key === request.target.key)) helperFail('auth_helper_target', '目标未列入本仓普通 Git 接入范围。');
  await helperRelationship(binding);
  const record = credentialPath.join(binding.owner.main, '.credentials/repositories/sources.local.md');
  const raw = await helperPrivateFile(binding, record, { optional: true });
  const sources = raw === null ? [] : parseCredentialSources(raw, binding.owner);
  const selected = sources.find(item => authTarget(item.url).key === request.target.key) || {
    url: request.target.url, method: request.target.host === 'github.com' ? 'github-token' : 'cnb-url',
    file: credentialPath.join('.credentials/repositories', request.target.filename),
  };
  let credential;
  if (selected.method === 'gh') {
    if (!binding.gh || !credentialPath.isAbsolute(binding.gh)) helperFail('gh_auth_unavailable', '未配置固定 gh 程序；重新检查本机接入。');
    const { stdout } = await credentialExec(binding.gh, ['auth', 'token', '--hostname', 'github.com', '--user', selected.account], { env: helperEnvironment(), encoding: 'utf8', timeout: 15000, maxBuffer: 32768, windowsHide: true });
    const token = stdout.trim();
    if (!token || /[\s\0]/.test(token)) helperFail('gh_account_unavailable', '已选 gh 账号没有可用 Token。');
    credential = { username: selected.account, password: token };
  } else {
    if (selected.method === 'environment') helperFail('auth_helper_environment', '进程环境来源未接入独立启动的普通 Git；请明确选择持久来源。');
    const file = credentialPath.resolve(binding.owner.main, selected.file);
    credential = parseCredentialFile(await helperPrivateFile(binding, file, { privateFile: true, maximum: 16384 }), selected.method, request.target);
  }
  if (request.username && request.username !== credential.username) helperFail('auth_helper_username', '请求用户名与已选来源不一致。');
  return { username: credential.username, password: credential.password };
}

export async function runProjectCredentialHelper(minimumNodeMajor) {
  try {
    if (Number(process.versions.node.split('.')[0]) < minimumNodeMajor) helperFail('node_version', 'Node 版本过低；重新设置本机运行时。');
    const [flag, encoded, operation, extra] = process.argv.slice(2);
    if (flag !== '--binding' || !encoded || extra !== undefined || !['get', 'store', 'erase'].includes(operation)) helperFail('auth_helper_arguments', '凭证接入参数无效。');
    if (operation !== 'get') return;
    if (encoded.length > 65536 || !/^[A-Za-z0-9_-]+$/.test(encoded)) helperFail('auth_helper_binding', '本机接入参数无效。');
    const binding = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
    let text = '';
    for await (const chunk of process.stdin) { text += chunk; if (Buffer.byteLength(text) > 32768) helperFail('auth_request_invalid', 'Git 凭证请求过大。'); }
    const credential = await projectCredential(binding, text);
    process.stdout.write(`username=${credential.username}\npassword=${credential.password}\n\n`);
  } catch (error) {
    process.stderr.write(`git-setup: ${/^[a-z_]+$/.test(error.code || '') ? error.code : 'auth_helper_unavailable'}；检查本仓来源和普通 Git 接入，不回退其他账号。\n`);
    process.stdout.write('quit=true\n\n');
    process.exitCode = 1;
  }
}
