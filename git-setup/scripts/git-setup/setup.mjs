import { resolveRepository } from './repository-relationship.mjs';
import { createHash } from 'node:crypto';
import { readdir } from 'node:fs/promises';
import { inspect, inspectRemote, init, completeInitialization, addRemote, replaceRemote, assertLocalRemote, setupFacts, cloneInto, redact, validateUrl,
  credentialInitialization, credentialProjectFiles, initializeCredentialRepository, cloneCredentialRepository, finishCredentialInitialization } from './git.mjs';
import { saveSetupRecord } from './setup-record.mjs';
import { authSelection, verifySelection, publicSelection, gitAuthentication, installAuth, rememberAuth } from './auth.mjs';
import { authTarget } from './auth-roots.mjs';
import { readHarnessConfig } from './harness-config.mjs';

const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const message = '已完成 Git 设置和远端读取验证。本次暂未提交和推送。';

async function harnessPolicies(project, relationship) {
  const own = await readHarnessConfig(project, relationship.registration.kind === 'self' ? relationship.harness.directory ?? undefined : undefined);
  const policies = [{ project, repository: '.', path: own.path, fingerprint: own.fingerprint, policy: own.config ?? null }];
  if (relationship.registration.kind !== 'self') {
    const management = await readHarnessConfig(relationship.management.root, relationship.harness.directory ?? undefined);
    policies.push({ project: relationship.management.root, repository: relationship.registration.path, path: management.path, fingerprint: management.fingerprint, policy: management.config?.repositories?.[relationship.registration.path] ?? null });
  }
  return policies;
}
function policyEffects(policies, name, url) {
  return policies.filter(item => item.policy?.targets?.[name]).map(item => {
    const sync = item.policy.sync, role = sync?.source === name ? 'source' : sync?.targets.includes(name) ? 'target' : null;
    return { project: item.project, path: item.path, repository: item.repository, before: item.policy.targets[name], after: url,
      sync: role ? { role, refs: sync.refs } : null,
      consequence: '已确认目标变化后，标准读取/发布/同步会停止；需重新生成并确认对应仓库的 Harness 策略。' };
  });
}
async function otherWorktreeEffects(local, facts, selection, options, relationship) {
  const effects = [];
  for (const line of facts?.worktrees.split('\n') ?? []) {
    if (!line.startsWith('worktree ')) continue;
    let directory = line.slice(9);
    if (directory.startsWith('"')) {
      try { directory = JSON.parse(directory); }
      catch { fail('setup_worktree_path', '其他 Worktree 路径包含未支持的 Git 转义；请先核对共享 Git 配置作用范围。'); }
    }
    if (directory === local.root) continue;
    const state = await inspect(directory, options);
    if (state.root === local.root) continue;
    await assertLocalRemote(state.root ?? directory, selection.name, options);
    const relation = await resolveRepository(directory, { workspace: relationship.registration.kind === 'self' ? directory : relationship.management.root, harnessDirectory: relationship.registration.kind === 'self' ? undefined : relationship.harness.directory });
    const policies = await harnessPolicies(directory, relation);
    effects.push({ project: directory, policies, harness: policyEffects(policies, selection.name, options.url),
      projectRules: 'Agent 必须另行读取本 Worktree 的规则；当前请求的规则不套用于它。' });
  }
  return effects;
}

export async function setup(project, options = {}) {
  validateUrl(options.url);
  const local = await inspect(project, options);
  if (local.bare || local.insideParent) fail('setup_project_required', '请先选择实际项目根目录；不自动修改父仓或 bare 仓库。');
  let relationship = await resolveRepository(local.project, options);
  const initialization = local.initialized ? await credentialInitialization(local.project, options) : null;
  if (initialization && initialization.target !== authTarget(options.url)?.key) fail('setup_initialization_target_changed', '凭证准备中的目标与本次 URL 不同；先核对原初始化记录，不连接其他目标。');
  if (initialization?.stage === 'checked-out' && (local.head !== initialization.head || local.branch !== initialization.branch)) fail('setup_initialization_changed', '已检出的 HEAD 或分支与初始化记录不同；先核对原操作，不自动重新检出。');
  if (initialization && !['checkout', 'checked-out'].includes(initialization.stage) && local.head) fail('setup_initialization_changed', '凭证准备后已有本地提交；先明确它与远端历史的关系。');
  const request = { project: local.project, url: options.url, action: options.action, name: options.name, branch: options.branch, authorName: options.authorName, authorEmail: options.authorEmail, workspace: options.workspace, harnessDirectory: options.harnessDirectory, useGlobalConfig: Boolean(options.useGlobalConfig), acceptEffects: Boolean(options.acceptEffects),
    authMethod: options.authMethod, ghUser: options.ghUser, credentialWorkspace: options.credentialWorkspace, credentialFile: options.credentialFile, credentialsFilled: Boolean(options.credentialsFilled), githubAuth: Boolean(options.githubAuth) };
  const record = async result => {
    if (options.planFile) await saveSetupRecord(options.planFile, { request, result });
    return result;
  };
  const need = async (required, detail, extra = {}) => {
    if (options.apply) fail('setup_choice_required', detail);
    return record({ status: 'needs_input', required, message: detail, local, relationship, initialization, ...extra });
  };
  if (local.remotes?.length && (!options.action || !options.name)) {
    return need(['action', 'name'], '已有远端：请先让用户选择添加或替换，并明确远端名称。此前自动采用 origin、脚本 selection、计划记录或成功结果不等于用户选择；没有用户亲自明确选择或明确继续中断操作的依据时，必须在会话中询问，不能自行补参数。');
  }
  const selection = { action: options.action || 'add', name: options.name || 'origin' };
  if (!['add', 'replace'].includes(selection.action) || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(selection.name)) fail('invalid_setup_selection', '必须选择 add/replace 和有效远端名称。');
  const existing = local.remotes?.find(r => r.name === selection.name);
  if (selection.action === 'replace' && !existing) fail('setup_remote_missing', '要替换的远端不存在。');
  if (existing && (existing.mirror || existing.fetch.length !== 1 || existing.push.length !== 1)) fail('setup_complex_remote', '选中的远端包含 mirror 或多个地址，需先明确其迁移方式。');
  if (selection.action === 'add' && existing && (existing.fetch[0] !== options.url || existing.push[0] !== options.url)) fail('remote_conflict', '新增远端名称已占用，请选择未占用的名称。');
  let authentication = await authSelection(local.project, options.url, options);
  relationship = authentication.relationship || relationship;
  if (authentication.status === 'needs_input') return need(authentication.required, authentication.message || '先在会话中完成目标仓库的鉴权选择及凭证准备；公开仓库可读不能代替选择。', { authentication: publicSelection(authentication) });
  if (authentication.status === 'selected') {
    authentication = await verifySelection(authentication, options);
    options = { ...options, ...gitAuthentication(local.project, options, authentication) };
  }
  const remote = await inspectRemote(local.project, options.url, options);
  const preparing = initialization && initialization.stage !== 'checked-out';
  const recoveringCheckout = preparing && initialization.stage === 'checkout' && local.head;
  const entries = preparing && !recoveringCheckout ? await credentialProjectFiles(local.project, authentication.binding.file) : local.initialized ? null : (await readdir(local.project)).sort();
  const kind = recoveringCheckout ? 'clone' : local.initialized && !preparing ? 'connect' : remote.empty ? 'init' : entries.length === 0 ? 'clone' : 'files_conflict';
  if (kind === 'files_conflict') return need(['local-files'], '当前目录已有文件且远端已有历史，请明确文件保留与迁入方式。', { remote, initialization, files: entries });
  if (kind === 'init' && (!options.branch || !options.authorName || !options.authorEmail)) return need(['branch', 'author-name', 'author-email'], '初始化前需沿项目规则明确初始分支与作者。');
  if ((options.authorName || options.authorEmail) && (!options.authorName || !options.authorEmail)) return need(['author-name', 'author-email'], '明确配置作者时必须同时提供姓名与邮箱。');
  const cloneBranch = kind === 'clone' ? options.branch || (remote.defaultRef?.startsWith('refs/heads/') ? remote.defaultRef.slice(11) : null) : null;
  if (kind === 'clone' && (!cloneBranch || !remote.refs[`refs/heads/${cloneBranch}`])) return need(['branch'], '远端有引用但没有可确定的检出分支；请明确一个实际存在的分支。', { remote });
  const inputs = { url: options.url, ...selection, branch: options.branch ?? null, authorName: options.authorName ?? null, authorEmail: options.authorEmail ?? null };
  const facts = local.initialized ? await setupFacts(local.project, options) : null;
  const policies = await harnessPolicies(local.project, relationship);
  const changed = existing && (existing.fetch[0] !== options.url || existing.push[0] !== options.url);
  const effects = {
    branches: facts?.branches ?? [], remotePushDefault: local.remotePushDefault ?? null,
    author: options.authorName ? { before: local.author ?? null, after: { name: options.authorName, email: options.authorEmail }, scope: 'repository; fill missing local fields only' } : null,
    harness: changed ? policyEffects(policies, selection.name, options.url) : [],
    retainedTrackingRefs: facts?.trackingRefs.filter(line => line.startsWith(`refs/remotes/${selection.name}/`)) ?? [],
    historyCompared: false, trackingRefsRefreshed: false,
    otherWorktrees: selection.action === 'replace' && changed ? await otherWorktreeEffects(local, facts, selection, options, relationship) : [],
  };
  if ((effects.harness.length || effects.otherWorktrees.length) && !options.acceptEffects) return need(['accept-effects'], '远端变更将影响对应仓库的 Harness 目标/同步策略或其他 Worktree；请先确认计划列出的具体变化。', { remote, selection, effects });
  const auth = publicSelection(authentication);
  const fingerprint = hash({ relationship: relationship.fingerprint, local, remote, entries, inputs, facts, effects, policies, auth, initialization, acceptedEffects: Boolean(options.acceptEffects) });
  const plan = { status: 'planned', kind, cloneBranch, local, remote, relationship, selection, authentication: auth, initialization, fingerprint, facts, effects, committed: false, pushed: false };
  if (!options.apply) return record(plan);
  if (options.expectedFingerprint !== fingerprint) fail('setup_plan_changed', '缺少计划指纹或状态已改变；请重新检查并使用最新计划，尚未写入。');
  await record({ ...plan, status: 'applying' });
  const completedSteps = [];
  let remoteStateFingerprint = facts?.fingerprint;
  let step = kind === 'init' ? 'init' : kind === 'clone' ? 'clone' : 'remote';
  try {
    if (kind === 'init') {
      if (preparing) await initializeCredentialRepository(local.project, options);
      else await init(local.project, options);
      completedSteps.push('init');
      remoteStateFingerprint = (await setupFacts(local.project, options)).fingerprint;
    }
    else if (kind === 'connect' && options.authorName && options.authorEmail) {
      step = 'initialization_author';
      await completeInitialization(local.project, { ...options, branch: options.branch ?? local.branch }); completedSteps.push('initialization_author');
      remoteStateFingerprint = (await setupFacts(local.project, options)).fingerprint;
    }
    if (kind === 'clone') {
      const cloneOptions = { ...options, name: selection.name, branch: cloneBranch, expectedHead: remote.refs[`refs/heads/${cloneBranch}`], defaultRef: remote.defaultRef, credentialFile: authentication.binding?.file };
      if (preparing) await cloneCredentialRepository(local.project, cloneOptions);
      else await cloneInto(local.project, cloneOptions);
      completedSteps.push('clone');
      if (authentication.status === 'selected') { await rememberAuth(local.project, authentication); completedSteps.push('authentication_choice'); }
      // Only the Agent can interpret newly obtained project rules. Yield before continuing Git setup.
      return await record({ ...plan, status: 'needs_project_rules', complete: false, completedSteps, local: await inspect(local.project, options), message: '已克隆到选定目录；Agent 需读取新取得的项目规则和实际 Harness 策略，然后沿用本次远端选择重新计划并完成设置。本次暂未提交和推送。' });
    }
    step = 'remote';
    if (selection.action === 'replace') await replaceRemote(local.project, { ...options, name: selection.name, expectedFetch: existing.fetch[0], expectedPush: existing.push[0], expectedState: remoteStateFingerprint });
    else await addRemote(local.project, { ...options, name: selection.name });
    completedSteps.push('remote');
    step = 'readback';
    let installedAuth;
    if (authentication.status === 'selected') {
      step = 'authentication';
      installedAuth = await installAuth(local.project, authentication);
      completedSteps.push('authentication'); step = 'readback';
    }
    const state = await inspect(local.project, options);
    const actual = state.remotes.find(r => r.name === selection.name);
    if (actual?.fetch.length !== 1 || actual.fetch[0] !== options.url || actual.push.length !== 1 || actual.push[0] !== options.url) fail('setup_readback', '远端实际地址与计划不一致，请检查 URL 重写或并发配置变化。');
    const verified = await inspectRemote(local.project, options.url, options);
    if (initialization) { await finishCredentialInitialization(local.project, options); completedSteps.push('initialization_complete'); }
    return await record({ ...plan, status: 'configured', message, local: state, remote: verified, initialization: initialization ? { ...initialization, stage: 'complete' } : null, authentication: installedAuth ? { ...installedAuth, remoteRead: 'verified' } : auth, completedSteps: [...completedSteps, 'readback'], complete: true });
  } catch (error) {
    let actual;
    try { actual = await inspect(local.project, options); } catch { actual = null; }
    const result = { ...plan, status: 'incomplete', complete: false, completedSteps, failedStep: step, local: actual, error: { code: error.code, message: redact(error.message) }, message: 'Git 设置未完成。已保留实际文件与配置；重新检查后补齐缺项，不自动清理、回滚或重试克隆。本次暂未提交和推送。' };
    try { await record(result); } catch (recordError) { result.recordError = { code: recordError.code, message: redact(recordError.message) }; }
    return result;
  }
}
