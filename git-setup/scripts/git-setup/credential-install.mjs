import { readFile, lstat, realpath, mkdir, open, rename, rm } from 'node:fs/promises';
import { resolve, join, relative, dirname, delimiter } from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { authTarget, credentialOwner, localEnvironment, localGit } from './auth-roots.mjs';
import { authSelection, rememberSource } from './auth.mjs';
import { readSources, sourceRecordPath } from './source-record.mjs';
import { inspect, setupFacts, updateLocalConfig, redact } from './git.mjs';
import { digest, safePath, portablePath, publicText, fields, fail } from './validation.mjs';

const execute = promisify(execFile);
const marker = '// git-setup:standalone-credential-helper:1';
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";

export async function renderProjectHelper() {
  const pkg = JSON.parse(await readFile(new URL('../../package.json', import.meta.url), 'utf8'));
  const data = await readFile(new URL('./credential-data.mjs', import.meta.url), 'utf8');
  const trust = (await readFile(new URL('./git-trust.mjs', import.meta.url), 'utf8')).replace(/^import \{ isAbsolute \} from 'node:path';\r?\n/, '');
  const runtime = (await readFile(new URL('./project-credential-helper.mjs', import.meta.url), 'utf8'))
    .replace(/^import .+ from '\.\/(?:credential-data|git-trust)\.mjs';\r?\n/gm, '');
  if (/from ['"]\.\//.test(data + trust + runtime)) fail('auth_helper_build', '独立 helper 仍有未分发的相对依赖。');
  const minimum = Number(pkg.engines.node.replace('>=', ''));
  if (!Number.isInteger(minimum) || minimum < 20) fail('auth_helper_build', 'Node 运行时要求无效。');
  const body = (data + '\n' + trust + '\n' + runtime + `\nawait runProjectCredentialHelper(${minimum});\n`).replaceAll('\r\n', '\n');
  return `${marker}\n// sha256:${digest(body)}\n${body}`;
}

async function executable(name, args) {
  const key = Object.keys(process.env).find(key => key.toLowerCase() === 'path');
  const suffix = process.platform === 'win32' ? '.exe' : '';
  for (const directory of (process.env[key] || '').split(delimiter).filter(Boolean)) {
    let file;
    try { file = await realpath(join(directory, name + suffix)); if (!(await lstat(file)).isFile()) continue; }
    catch (error) { if (['ENOENT', 'ENOTDIR'].includes(error.code)) continue; throw error; }
    try { await execute(file, args, { env: localEnvironment(), timeout: 15000, maxBuffer: 32768, windowsHide: true }); }
    catch { fail('auth_helper_runtime', `${name} 无法执行；修复所选运行时后重试。`); }
    return file;
  }
  fail('auth_helper_runtime', `找不到可执行的 ${name}；请安装并重新设置本机接入。`);
}

async function helperSnapshot(root, path) {
  const file = join(root, path);
  await safePath(root, file);
  try {
    const info = await lstat(file);
    if (!info.isFile() || info.nlink !== 1 || info.size > 262144) fail('auth_helper_file', 'helper 路径已被其他文件占用。');
    const content = await readFile(file, 'utf8');
    const match = /^\/\/ git-setup:standalone-credential-helper:1\r?\n\/\/ sha256:([a-f0-9]{64})\r?\n/.exec(content);
    if (!match || digest(content.slice(match[0].length).replaceAll('\r\n', '\n')) !== match[1]) fail('auth_helper_modified', '已有 helper 不是未修改的本版本生成文件；先核对其用途和保留方式。');
    return { digest: digest(content), mode: info.mode & 0o777 };
  } catch (error) { if (error.code === 'ENOENT') return { digest: null, mode: 0o644 }; throw error; }
}

function credentialScopeMatches(key, urls) {
  const match = /^credential\.(.+)\.(?:helper|usehttppath|username)$/i.exec(key);
  if (!match) return false;
  let scope;
  try { scope = new URL(match[1]); } catch { return true; }
  const host = new RegExp('^' + scope.hostname.split('*').map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[^.]*') + '$', 'i');
  return urls.some(url => {
    let target;
    try { target = new URL(url); } catch { return false; }
    if (scope.protocol !== target.protocol || scope.port !== target.port || !host.test(target.hostname)) return false;
    const prefix = scope.pathname.replace(/\/$/, '');
    return !prefix || [target.pathname, target.pathname.replace(/\.git\/?$/, '')].some(path => path === prefix || path.startsWith(prefix + '/'));
  });
}

async function configuration(root, urls) {
  const raw = await localGit(root, ['config', '--null', '--show-origin', '--show-scope', '--list']);
  const values = raw.split('\0');
  if (values.at(-1) === '') values.pop();
  if (values.length % 3) fail('auth_helper_configuration', '无法核对 Git 配置来源。');
  const entries = [];
  for (let i = 0; i < values.length; i += 3) {
    const split = values[i + 2].indexOf('\n');
    const key = split < 0 ? values[i + 2] : values[i + 2].slice(0, split);
    entries.push({ scope: values[i], origin: values[i + 1], key, value: split < 0 ? '' : values[i + 2].slice(split + 1) });
  }
  // A common-config installation must not claim to override more specific
  // credential rules or a per-worktree setting. The Agent can reuse such an
  // existing helper instead, after checking its actual contract.
  const conflict = entries.find(entry => credentialScopeMatches(entry.key, urls)
    || (entry.scope === 'worktree' && /^(?:credential\.|http\..*(?:extraheader|followredirects)$)/i.test(entry.key)));
  if (conflict) fail('auth_helper_configuration', `存在需要先核对的 URL 专属或 Worktree 鉴权配置：${redact(conflict.key)}（${redact(conflict.origin)}）；本入口不覆盖该来源。`);
  return { entries, fingerprint: digest(raw) };
}

async function prepareInstall(project, input, options) {
  fields(input, ['targets', 'helperPath', 'sources'], '普通 Git 接入');
  publicText(input);
  if (!Array.isArray(input.targets) || !input.targets.length || input.targets.length > 200) fail('auth_helper_targets', '需要明确接入的完整 HTTPS 目标列表。');
  const targets = input.targets.map(url => authTarget(url));
  if (targets.some(target => !target) || new Set(targets.map(target => target.key)).size !== targets.length) fail('auth_helper_targets', '目标必须是不同的 GitHub/CNB HTTPS 仓库。');
  if (input.sources !== undefined && (!Array.isArray(input.sources) || input.sources.some(source => !targets.some(target => authTarget(source.url)?.key === target.key)))) fail('auth_helper_sources', '来源选择必须属于本次接入目标。');
  const scope = await credentialOwner(project, options);
  if (!scope.repository.isGit || scope.repository.requestedPath !== scope.repository.root) fail('auth_helper_project', '请明确实际 Git 检出根目录。');
  const root = scope.repository.main;
  const local = await inspect(root);
  const endpoints = local.remotes.flatMap(remote => [...remote.fetch, ...remote.push]);
  for (const target of targets) if (!endpoints.some(url => authTarget(url)?.key === target.key)) fail('auth_helper_targets', '接入目标必须对应所选仓库当前的实际远端。');
  const path = portablePath(input.helperPath || 'scripts/git/credential-helper.mjs');
  const before = await helperSnapshot(root, path);
  const content = await renderProjectHelper();
  const configurations = [];
  const worktreePaths = (await localGit(root, ['-c', 'core.quotePath=false', 'worktree', 'list', '--porcelain'])).split('\n').filter(line => line.startsWith('worktree ')).map(line => {
    const value = line.slice(9);
    try { return value.startsWith('"') ? JSON.parse(value) : value; }
    catch { fail('auth_helper_worktree', 'Worktree 路径无法核对。'); }
  });
  const worktrees = await Promise.all(worktreePaths.map(path => realpath(resolve(path))));
  for (const checkout of worktrees) configurations.push({ checkout, ...await configuration(checkout, endpoints) });
  const saved = await readSources(scope.owner);
  let recordDigest = null;
  try { recordDigest = digest(await readFile(sourceRecordPath(scope.owner))); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const selections = [], updates = [];
  for (const target of targets) {
    const method = input.sources?.find(source => authTarget(source.url)?.key === target.key)?.method
      ?? options.authMethod ?? saved.find(source => authTarget(source.url).key === target.key)?.method;
    if (method === 'environment') fail('auth_helper_environment', '普通 Git 接入需要明确的文件或 gh 账号来源；进程环境来源继续用于按次鉴权。');
    const selected = await authSelection(project, target.url, { ...options, sources: input.sources });
    if (selected.status !== 'selected') fail('auth_selection_required', `目标 ${target.url} 的来源尚未就绪；先处理 ${selected.required?.join(', ')}。`);
    if (selected.source.method === 'environment') fail('auth_helper_environment', '普通 Git 接入需要明确的文件或 gh 账号来源；进程环境来源继续用于按次鉴权。');
    selections.push(selected);
    const source = selected.source;
    const entry = { url: target.url, method: source.method, ...(source.file ? { file: relative(scope.owner.main, source.file).replaceAll('\\', '/') } : { account: source.account }) };
    const previous = saved.find(item => authTarget(item.url).key === target.key);
    const normalized = previous && { ...previous, ...(previous.file ? { file: relative(scope.owner.main, resolve(scope.owner.main, previous.file)).replaceAll('\\', '/') } : {}) };
    const standard = source.file === join(scope.directory, target.filename) && source.method === (target.host === 'github.com' ? 'github-token' : 'cnb-url');
    if ((previous || !standard) && JSON.stringify(normalized) !== JSON.stringify(entry)) updates.push(entry);
    for (const checkout of worktrees) for (const form of [target.url, target.url + '.git']) {
      const headers = await localGit(checkout, ['config', '--get-urlmatch', 'http.extraheader', form], true);
      if (/(?:authorization|proxy-authorization|cookie)\s*:/im.test(headers || '')) fail('auth_http_header_conflict', '目标已有额外鉴权头；先核对其来源，不覆盖或借用。');
      const username = (await localGit(checkout, ['config', '--get-urlmatch', 'credential.username', form], true))?.trim();
      if (username && username !== selected.credential.username) fail('auth_helper_username', 'Git 已配置的用户名与目标所选来源不同；先核对原配置。');
    }
  }
  const git = await executable('git', ['--version']);
  const gh = selections.some(item => item.source.method === 'gh') ? await executable('gh', ['--version']) : null;
  const node = await realpath(process.execPath);
  const registration = scope.relationship.registration;
  const binding = {
    version: 1, git, gh,
    repository: { main: root, commonDir: scope.repository.commonDir }, owner: scope.owner,
    registration: { kind: registration.kind, path: registration.path,
      ...(registration.kind === 'independent' ? { registry: relative(scope.owner.main, registration.evidence).replaceAll('\\', '/') } : {}) },
    targets: targets.map(target => target.url),
  };
  const encoded = Buffer.from(JSON.stringify(binding)).toString('base64url');
  const command = '!f() { NODE_OPTIONS= NODE_PATH= ' + quote(node.replaceAll('\\', '/')) + ' ' + quote(join(root, path).replaceAll('\\', '/')) + ' --binding ' + quote(encoded) + ' "$@" || printf "quit=true\\n\\n"; }; f';
  const existingHelpers = configurations.flatMap(item => {
    let effective = [];
    for (const entry of item.entries) if (entry.key === 'credential.helper') {
      if (!entry.value) effective = []; else effective.push(entry.value);
    }
    return effective.filter(value => value !== command);
  });
  const blockedRemotes = local.remotes.filter(remote => [...remote.fetch, ...remote.push].some(url => /^https?:/.test(url) && !targets.some(target => authTarget(url)?.key === target.key))).map(remote => remote.name);
  const effects = { otherWorktrees: worktrees.filter(path => path !== scope.repository.root), replacedHelperEntries: existingHelpers.length,
    blockedRemotes, sourceRecord: updates.length ? sourceRecordPath(scope.owner) : null, sourceUpdates: updates };
  const facts = await setupFacts(root);
  const plan = { status: 'planned', project: scope.repository.root, main: root, targets: binding.targets,
    helper: { path: join(root, path), before: before.digest, after: digest(content), node, git, gh },
    authentication: selections.map(item => ({ target: item.target.url, method: item.source.method, file: item.source.file, account: item.source.account })),
    effects, requiresAcceptEffects: Boolean(effects.otherWorktrees.length || existingHelpers.length || blockedRemotes.length),
    sourceRecordIgnoredRequired: Boolean(updates.length), persistentHelper: true, remoteRead: 'not_performed', pushed: false,
  };
  plan.fingerprint = digest({ plan, facts, configurations: configurations.map(item => [item.checkout, item.fingerprint]), relationship: scope.relationship.fingerprint,
    authentication: selections.map(item => item.fingerprint), recordDigest });
  return { plan, root, path, before, content, command, binding, facts, selections, updates, scope };
}

async function writeHelper(prepared) {
  const { root, path, before, content } = prepared;
  if (before.digest === digest(content)) return;
  const file = join(root, path), lock = `${file}.lock`;
  await safePath(root, file);
  await mkdir(dirname(file), { recursive: true });
  let handle, acquired = false;
  try {
    handle = await open(lock, 'wx', before.mode); acquired = true;
    if ((await helperSnapshot(root, path)).digest !== before.digest) fail('auth_helper_changed', 'helper 文件在写入前变化。');
    await handle.writeFile(content); await handle.close(); handle = null;
    await rename(lock, file); acquired = false;
  } finally { if (handle) await handle.close(); if (acquired) await rm(lock, { force: true }); }
}

function fillCredential(git, project, url) {
  return new Promise((resolveFill, reject) => {
    const child = spawn(git, ['-C', project, 'credential', 'fill'], { env: { ...localEnvironment(), GIT_ASKPASS: '', SSH_ASKPASS: '' }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '', overflow = false;
    const timer = setTimeout(() => { child.kill(); }, 30000);
    child.stdout.on('data', data => { output += data; if (output.length > 32768) { overflow = true; child.kill(); } });
    child.stderr.resume();
    child.stdin.on('error', () => {});
    child.on('error', () => { clearTimeout(timer); reject(Object.assign(new Error('普通 Git helper 无法启动。'), { code: 'auth_helper_readback' })); });
    child.on('close', code => {
      clearTimeout(timer);
      if (code !== 0 || overflow) reject(Object.assign(new Error('普通 Git 凭证读取未通过；配置已保留，核对后续作。'), { code: 'auth_helper_readback' }));
      else resolveFill(Object.fromEntries(output.split(/\r?\n/).filter(Boolean).map(line => { const i = line.indexOf('='); return [line.slice(0, i), line.slice(i + 1)]; })));
    });
    child.stdin.end(`url=${url}\n\n`);
  });
}

export async function installCredentialHelper(project, input, options = {}) {
  const prepared = await prepareInstall(project, input, options);
  const { plan, root, path, command, binding, facts, selections, updates } = prepared;
  if (!options.apply) return plan;
  if (options.expectedFingerprint !== plan.fingerprint) fail('auth_helper_changed', '来源、配置、目标或 helper 已变化；先重新预览，尚未写入。');
  if (plan.requiresAcceptEffects && !options.acceptEffects) fail('auth_helper_effects', '本计划影响已有 helper、其他远端或 Worktree；沿已确认范围传入 accept-effects。');
  const completed = [];
  try {
    for (const entry of updates) {
      await rememberSource(project, entry, { ...options, apply: true, allowIgnore: Boolean(options.allowIgnore) });
      completed.push('source:' + entry.url);
    }
    await writeHelper(prepared); completed.push(join(root, path));
    await updateLocalConfig(root, { expectedState: facts.fingerprint }, async file => {
      for (const selected of selections) {
        const current = await authSelection(project, selected.target.url, { ...options, sources: input.sources });
        if (current.fingerprint !== selected.fingerprint) fail('auth_source_changed', '配置前凭证或来源已变化；重新预览。');
      }
      const desired = new Map([['credential.helper', ['', command]], ['credential.useHttpPath', ['true']]]);
      for (const url of binding.targets) for (const form of [url, url + '.git']) desired.set(`http.${form}.followRedirects`, ['false']);
      for (const [key, values] of desired) {
        const raw = await localGit(root, ['config', '--file', file, '--null', '--get-all', key], true);
        const previous = raw === null ? [] : raw.split('\0').slice(0, -1);
        if (JSON.stringify(previous) === JSON.stringify(values)) continue;
        await localGit(root, ['config', '--file', file, '--replace-all', key, values[0]]);
        for (const value of values.slice(1)) await localGit(root, ['config', '--file', file, '--add', key, value]);
      }
    });
    completed.push('repository-config');
    if (digest(await readFile(join(root, path))) !== plan.helper.after) fail('auth_helper_readback', 'helper 文件回读不一致。');
    for (const selected of selections) {
      const actual = await fillCredential(binding.git, project, selected.target.url);
      if (actual.username !== selected.credential.username || actual.password !== selected.credential.password) fail('auth_helper_readback', '普通 Git 未使用本次选定的来源。');
    }
    return { status: 'installed', complete: true, project, helper: plan.helper, targets: binding.targets, authentication: plan.authentication,
      completed, persistentHelper: true, credentialProtocol: 'verified', remoteRead: 'not_performed', ide: 'not_verified', pushed: false };
  } catch (error) {
    return { status: 'incomplete', complete: false, project, helper: plan.helper, completed, code: error.code || 'auth_helper_install',
      message: '普通 Git 接入未完成；保留已写入的文件或配置，核对后重新预览续作。', persistentHelper: completed.includes('repository-config'), pushed: false };
  }
}
