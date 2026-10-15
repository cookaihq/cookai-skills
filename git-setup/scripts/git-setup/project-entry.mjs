import { readFile, lstat, open, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { projectCheck } from './project-check.mjs';
import { selectRepository } from './repository-relationship.mjs';
import { assertHarnessTargets } from './harness-config.mjs';
import { digest, safePath, runtimeFail as fail } from './runtime-bundle.mjs';
import { gitAuthentication } from './auth.mjs';
import { localEnvironment } from './auth-roots.mjs';
import { inspectRemote, publish, sync, redact } from './git.mjs';
import { auditHistory } from './audit.mjs';
import { saveSetupRecord } from './setup-record.mjs';

function parse(args) {
  const options = { refs: [] };
  for (let i = 0; i < args.length; i++) {
    const key = args[i];
    if (key === '--apply') { if (options.apply) fail('invalid_argument', '参数重复。'); options.apply = true; continue; }
    if (!['--repository', '--checkout', '--remote', '--ref', '--expected-fingerprint', '--operation-id'].includes(key) || !args[i + 1] || args[i + 1].startsWith('--')) fail('invalid_argument', '项目入口只接受 repository、checkout、remote、完整 ref、计划指纹、operation-id 和 apply。');
    const value = args[++i];
    if (key === '--ref') options.refs.push(value);
    else { if (options[key.slice(2)]) fail('invalid_argument', '参数重复。'); options[key.slice(2)] = value; }
  }
  return options;
}

async function assertExtensions(root, extensions) {
  for (const extension of extensions) {
    const path = join(root, extension.path);
    await safePath(root, path);
    const info = await lstat(path);
    if (!info.isFile() || info.size > 262144 || digest(await readFile(path)) !== extension.sha256) fail('harness_extension_changed', '项目扩展内容已变化；重新检查并确认后才执行 Git 写入。');
  }
}

async function executeExtension(root, extension, input) {
  await assertExtensions(root, [extension]);
  const env = localEnvironment();
  for (const key of Object.keys(env)) if (/^GIT_SETUP_|(?:TOKEN|PASSWORD|SECRET|API_KEY)/i.test(key)) delete env[key];
  return new Promise(done => {
    const child = spawn(process.execPath, [join(root, extension.path)], { cwd: root, env, detached: process.platform !== 'win32', windowsHide: true, stdio: ['pipe', 'ignore', 'ignore'] });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      if (process.platform !== 'win32') { try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); } }
      else {
        const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore', timeout: 5000 });
        killer.on('error', () => child.kill('SIGKILL'));
      }
    }, extension.timeoutMs);
    child.on('error', () => { clearTimeout(timer); done({ id: extension.id, status: 'uncertain', reason: 'could_not_start' }); });
    child.on('close', code => { clearTimeout(timer); done({ id: extension.id, status: code === 0 && !timedOut ? 'completed' : 'uncertain', code, timedOut }); });
    child.stdin.on('error', () => {});
    child.stdin.end(`${JSON.stringify(input)}\n`);
  });
}

export async function projectOperation(root, command, options = {}) {
  if (options.checkout !== undefined && options.repository === undefined) fail('missing_argument', '指定 checkout 时必须明确选择 repository（外仓自身用 .）。');
  const installationRoot = root;
  const checkOptions = { directory: options.directory, command, remote: options.remote, repository: options.repository, checkout: options.checkout };
  const entry = await projectCheck(installationRoot, checkOptions);
  root = entry.project;
  if (command === 'check') return entry;
  if (!['read', 'publish', 'sync'].includes(command)) fail('unknown_command', '项目入口支持 check、read、publish、sync，以及 auth-info/auth-prepare/auth-verify。');
  const selected = entry.selected;
  const state = entry.local;
  const auth = gitAuthentication(root, { workspace: installationRoot, harnessDirectory: entry.installation.directory });
  if (command === 'read') {
    if (options.apply || options.refs?.length) fail('invalid_argument', 'read 只读取指定远端。');
    const remote = state.remotes.find(item => item.name === options.remote);
    if (!remote) fail('missing_remote', '明确选择已确认的远端名称。');
    return { status: 'read', remote: await inspectRemote(root, remote.fetch[0], auth), authentication: auth.authReports, actualPush: 'not_performed', entryCheck: entry };
  }
  const extensions = selected.config.extensions.filter(extension => extension.event === `after-${command}`);
  await assertExtensions(root, extensions);
  const scope = { command, repository: entry.repository, checkout: root, installationRoot, relationship: entry.relationship.fingerprint, remote: options.remote ?? null, refs: options.refs || [], config: selected.fingerprint };
  let receipt, record, previous;
  if (extensions.length) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/.test(options['operation-id'] || '')) fail('harness_operation_id_required', '有项目扩展时提供稳定的 operation-id（8–80 位）；中断后复用，不换标识重发。');
    receipt = join(root, 'data/git-setup', `harness-${options['operation-id']}.json`);
    await safePath(root, receipt);
    try { previous = JSON.parse(await readFile(receipt, 'utf8')); } catch (error) { if (error.code !== 'ENOENT') fail('harness_operation_invalid', '扩展操作记录无法读取；保留现场。'); }
    if (previous) {
      if (digest(previous.request.scope) !== digest(scope)) fail('harness_operation_changed', '同一 operation-id 对应不同输入；先核对原操作。');
      if (previous.result.status === 'completed') return { status: 'previous_completed', recovered: true, previousResult: previous.result.result, verification: 'historical_receipt_only', actualPush: 'not_performed', entryCheck: entry };
      fail('harness_extension_uncertain', '先前操作未完成或结果不明；检查原记录及扩展真实结果，不自动重发 Git 或扩展。');
    }
    record = async (result, writeOptions) => saveSetupRecord(receipt, { request: { scope, operationId: options['operation-id'] }, result }, writeOptions);
  }
  let preview;
  if (command === 'publish') {
    const remote = state.remotes.find(item => item.name === options.remote);
    if (!remote || !options.refs?.length) fail('missing_argument', '发布需要已确认的 remote 和完整 ref 清单。');
    const audit = auditHistory(root, options.refs);
    if (!audit.complete) return { status: 'audit_blocked', complete: false, audit, entryCheck: entry };
    preview = { audit, remote: await inspectRemote(root, remote.push[0], auth) };
  } else {
    if (options.remote || options.refs?.length) fail('invalid_argument', '同步只使用项目 config.json 中已确认的来源、目标和 refs。');
    preview = await sync(root, { sync: selected.config.sync }, { dryRun: true, ...auth });
  }
  const fingerprint = digest({ entry: entry.fingerprint, scope, preview });
  if (!options.apply) return { status: 'planned', dryRun: true, fingerprint, preview, extensions, pushPreflight: 'not_performed', actualPush: 'not_performed', entryCheck: entry };
  if (options['expected-fingerprint'] !== fingerprint) fail('harness_plan_changed', '项目、鉴权来源、引用或远端状态已改变；重新检查后使用本次计划指纹。');
  if ((await projectCheck(installationRoot, checkOptions)).fingerprint !== entry.fingerprint) fail('harness_plan_changed', '执行前仓库关系、策略或来源变化；未写入目标。');
  let lock;
  try {
    if (record) {
      // Validate ignored storage before any remote write; then serialize this operation.
      await record({ status: 'prepared' }, { createOnly: true });
      lock = await open(`${receipt}.operation-lock`, 'wx', 0o600);
      await record({ status: 'git_started' });
    }
    await assertHarnessTargets(root, selected.config, command === 'publish' ? [options.remote] : [selected.config.sync?.source, ...(selected.config.sync?.targets || [])].filter(Boolean));
    const result = command === 'publish'
      ? await publish(root, { remote: options.remote, refs: options.refs, expectedRefs: preview.audit.snapshot, ...auth })
      : await sync(root, { sync: selected.config.sync }, { ...auth, expectedRefs: preview.snapshots });
    result.extensions = [];
    result.authentication = auth.authReports;
    result.pushPreflight = 'not_performed'; result.actualPush = 'see_per_ref_results';
    if (result.complete) {
      for (const extension of extensions) {
        await record({ status: 'extension_started', id: extension.id, result });
        const outcome = await executeExtension(root, extension, { schemaVersion: 1, event: extension.event, project: root, operationId: options['operation-id'], git: result });
        result.extensions.push(outcome);
        if (outcome.status !== 'completed') { result.gitComplete = true; result.complete = false; result.status = 'extension_uncertain'; break; }
      }
    }
    if (record) await record({ status: result.complete ? 'completed' : 'incomplete', result });
    return { ...result, entryCheck: entry };
  } finally { if (lock) { await lock.close(); await rm(`${receipt}.operation-lock`, { force: true }); } }
}

export async function runProject(root, directory) {
  const [command = 'check', ...args] = process.argv.slice(2);
  try {
    if (command === 'runtime-info') {
      if (args.length) fail('invalid_argument', 'runtime-info 不接受仓库选择或业务参数。');
      const manifest = JSON.parse(await readFile(join(root, directory, 'manifest.json'), 'utf8'));
      process.stdout.write(`${JSON.stringify({ status: 'runtime_verified', installation: { root, directory, path: join(root, directory) }, fingerprint: digest(manifest.files), authentication: 'not_inspected', network: 'not_performed' })}\n`);
      return;
    }
    if (['auth-info', 'auth-prepare', 'auth-verify'].includes(command)) {
      if (args.some(value => ['--project', '--workspace', '--credential-workspace', '--harness-directory'].includes(value))) fail('invalid_argument', '生成入口的管理仓与安装位置固定；用 repository/checkout 选择已登记仓库。');
      const selectionOptions = {}, forwarded = [];
      for (let index = 0; index < args.length; index++) {
        const name = args[index];
        if (!['--repository', '--checkout'].includes(name)) { forwarded.push(name); continue; }
        const key = name.slice(2), value = args[++index];
        if (selectionOptions[key] !== undefined || !value || value.startsWith('--')) fail('invalid_argument', '仓库选择重复或缺值。');
        selectionOptions[key] = value;
      }
      if (selectionOptions.checkout && !selectionOptions.repository) fail('missing_argument', 'checkout 必须与明确的 repository 一起提供。');
      const selection = await selectRepository(root, { ...selectionOptions, harnessDirectory: directory });
      const cli = fileURLToPath(new URL('./cli.mjs', import.meta.url));
      const child = spawn(process.execPath, [cli, command, '--project', selection.project.root, '--workspace', root, '--harness-directory', directory, ...forwarded], { stdio: 'inherit', windowsHide: true });
      process.exitCode = await new Promise(done => { child.on('error', () => done(1)); child.on('close', code => done(code ?? 1)); });
      return;
    }
    const result = await projectOperation(root, command, { ...parse(args), directory });
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    if (result.complete === false) process.exitCode = 2;
  } catch (error) {
    process.stderr.write(`${JSON.stringify({ status: 'error', code: error.code || 'harness_error', message: redact(error.message) })}\n`);
    process.exitCode = 1;
  }
}
