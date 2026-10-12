import { createHash } from 'node:crypto';
import { readdir } from 'node:fs/promises';
import { inspect, inspectRemote, init, completeInitialization, addRemote, replaceRemote, assertLocalRemote, setupFacts, cloneInto, redact, validateUrl } from './git.mjs';
import { readProjectConfig, writeProject, resolveIssue, validateIssue } from './config.mjs';
import { saveSetupRecord } from './setup-record.mjs';

const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const message = '已完成 Git 设置和远端读取验证。本次暂未提交和推送。';

async function issueStatus(local, options) {
  const inspectedProject = {
    requestedPath: local.project, root: local.root ?? local.project, isGit: local.initialized,
    gitDir: local.gitDir ?? null, commonDir: local.commonDir ?? null, isWorktree: local.type === 'worktree',
    superprojectRoot: local.superproject ?? null,
    remotes: (local.remotes ?? []).map(r => ({ name: r.name, fetchUrls: r.fetch, pushUrls: r.push })),
  };
  try { return { status: 'existing', resolution: await resolveIssue(local.project, { ...options, inspectedProject }) }; }
  catch (error) { return { status: error.code === 'ISSUE_CONFIG_REQUIRED' ? 'not_configured' : 'invalid', code: error.code, message: error.message }; }
}

function issueTarget(status) {
  const r = status.resolution;
  return r ? { backend: r.backend, targetProject: r.targetProject, github: r.github, local: r.local } : { status: status.status, code: status.code };
}

async function otherWorktreeEffects(local, facts, selection, options) {
  const effects = [];
  for (const line of facts?.worktrees.split('\n') ?? []) {
    if (!line.startsWith('worktree ')) continue;
    let directory = line.slice(9);
    if (directory.startsWith('"')) {
      try { directory = JSON.parse(directory); }
      catch { fail('setup_worktree_path', '其他 Worktree 路径包含未支持的 Git 转义；请先核对共享配置作用范围。'); }
    }
    if (directory === local.root) continue;
    const state = await inspect(directory, options);
    if (state.root === local.root) continue; // Submodule registrations can list their Git metadata path.
    await assertLocalRemote(state.root ?? directory, selection.name, options);
    const shared = await readProjectConfig(state.root ?? directory);
    const ownOptions = { ...options, workspace: undefined, issueOverride: undefined, projectRuleIssue: undefined };
    const before = await issueStatus(state, ownOptions);
    const remotes = (state.remotes ?? []).filter(r => r.name !== selection.name).map(r => ({ name: r.name, fetchUrls: r.fetch, pushUrls: r.push }));
    remotes.push({ name: selection.name, fetchUrls: [options.url], pushUrls: [options.url] });
    const after = await issueStatus(state, { ...ownOptions, prospectiveRemotes: remotes });
    const sync = shared.config?.sync;
    const role = sync?.source === selection.name ? 'source' : sync?.targets.includes(selection.name) ? 'target' : null;
    effects.push({ project: directory, sharedFingerprint: shared.fingerprint, issueFingerprint: before.resolution?.fingerprint ?? null,
      sync: role ? { role, configuration: sync } : null,
      issues: hash(issueTarget(before)) !== hash(issueTarget(after)) ? { before: issueTarget(before), after: issueTarget(after) } : null,
      sharedRemote: shared.config?.remotes?.[selection.name] ?? null,
      projectRules: 'Agent 必须另行读取本 Worktree 的规则；当前请求的规则、覆盖和显式外仓选择不套用于它。',
    });
  }
  return effects;
}

export async function setup(project, options = {}) {
  validateUrl(options.url);
  if (options.issueOverride) validateIssue(options.issueOverride);
  if (options.projectRuleIssue) validateIssue(options.projectRuleIssue);
  const local = await inspect(project, options);
  if (local.bare || local.insideParent) fail('setup_project_required', '请先选择实际项目根目录；不自动修改父仓或 bare 仓库。');
  const request = { project: local.project, url: options.url, action: options.action, name: options.name, branch: options.branch, authorName: options.authorName, authorEmail: options.authorEmail, workspace: options.workspace, useGlobalConfig: Boolean(options.useGlobalConfig), acceptEffects: Boolean(options.acceptEffects), issueOverride: options.issueOverride, projectRuleIssue: options.projectRuleIssue };
  const record = async result => {
    if (options.planFile) await saveSetupRecord(options.planFile, { request, result });
    return result;
  };
  const need = async (required, detail, extra = {}) => {
    if (options.apply) fail('setup_choice_required', detail);
    return record({ status: 'needs_input', required, message: detail, local, ...extra });
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
  const shared = await readProjectConfig(local.project);
  const remote = await inspectRemote(local.project, options.url, options);
  const entries = local.initialized ? null : (await readdir(local.project)).sort();
  const kind = local.initialized ? 'connect' : remote.empty ? 'init' : entries.length === 0 ? 'clone' : 'files_conflict';
  if (kind === 'files_conflict') return need(['local-files'], '当前目录已有文件且远端已有历史，请明确文件保留与迁入方式。', { remote });
  if (kind === 'init' && (!options.branch || !options.authorName || !options.authorEmail)) return need(['branch', 'author-name', 'author-email'], '初始化前需沿项目规则明确初始分支与作者。');
  if ((options.authorName || options.authorEmail) && (!options.authorName || !options.authorEmail)) return need(['author-name', 'author-email'], '明确配置作者时必须同时提供姓名与邮箱。');
  const cloneBranch = kind === 'clone' ? options.branch || (remote.defaultRef?.startsWith('refs/heads/') ? remote.defaultRef.slice(11) : null) : null;
  if (kind === 'clone' && (!cloneBranch || !remote.refs[`refs/heads/${cloneBranch}`])) return need(['branch'], '远端有引用但没有可确定的检出分支；请明确一个实际存在的分支。', { remote });
  const inputs = { url: options.url, ...selection, branch: options.branch ?? null, authorName: options.authorName ?? null, authorEmail: options.authorEmail ?? null };
  const facts = local.initialized ? await setupFacts(local.project, options) : null;
  const issues = await issueStatus(local, options);
  const prospectiveRemotes = (local.remotes ?? []).filter(r => r.name !== selection.name).map(r => ({ name: r.name, fetchUrls: r.fetch, pushUrls: r.push }));
  prospectiveRemotes.push({ name: selection.name, fetchUrls: [options.url], pushUrls: [options.url] });
  const afterIssues = await issueStatus(local, { ...options, prospectiveRemotes });
  const changed = existing && (existing.fetch[0] !== options.url || existing.push[0] !== options.url);
  const sync = shared.config?.sync;
  const syncRole = changed && (sync?.source === selection.name ? 'source' : sync?.targets.includes(selection.name) ? 'target' : null);
  const effects = {
    branches: facts?.branches ?? [], remotePushDefault: local.remotePushDefault ?? null,
    author: options.authorName ? { before: local.author ?? null, after: { name: options.authorName, email: options.authorEmail }, scope: 'repository; fill missing local fields only' } : null,
    sync: syncRole ? { role: syncRole, before: existing, after: { name: selection.name, fetch: [options.url], push: [options.url] }, refs: sync.refs } : null,
    issues: hash(issueTarget(issues)) !== hash(issueTarget(afterIssues)) ? { before: issueTarget(issues), after: issueTarget(afterIssues) } : null,
    retainedTrackingRefs: facts?.trackingRefs.filter(line => line.startsWith(`refs/remotes/${selection.name}/`)) ?? [],
    historyCompared: false, trackingRefsRefreshed: false,
    otherWorktrees: selection.action === 'replace' && changed ? await otherWorktreeEffects(local, facts, selection, options) : [],
  };
  if ((effects.sync || effects.issues || effects.otherWorktrees.length) && !options.acceptEffects) return need(['accept-effects'], '远端变更将影响同步、Issue 解析目标或其他 Worktree；请先确认计划列出的具体变化。', { remote, selection, effects, issues });
  const fingerprint = hash({ local, remote, entries, inputs, facts, effects, issues, afterIssues, acceptedEffects: Boolean(options.acceptEffects), config: shared.fingerprint });
  const plan = { status: 'planned', kind, cloneBranch, local, remote, selection, fingerprint, facts, effects, configPath: shared.configPath, issues, committed: false, pushed: false };
  if (!options.apply) return record(plan);
  if (options.expectedFingerprint !== fingerprint) fail('setup_plan_changed', '缺少计划指纹或状态已改变；请重新检查并使用最新计划，尚未写入。');
  await record({ ...plan, status: 'applying' });
  const completedSteps = [];
  let remoteStateFingerprint = facts?.fingerprint;
  let step = kind === 'init' ? 'init' : kind === 'clone' ? 'clone' : 'remote';
  try {
    if (kind === 'init') { await init(local.project, options); completedSteps.push('init'); }
    else if (kind === 'connect' && options.authorName && options.authorEmail) {
      step = 'initialization_author';
      await completeInitialization(local.project, { ...options, branch: options.branch ?? local.branch }); completedSteps.push('initialization_author');
      remoteStateFingerprint = (await setupFacts(local.project, options)).fingerprint;
    }
    if (kind === 'clone') {
      await cloneInto(local.project, { ...options, name: selection.name, branch: cloneBranch, expectedHead: remote.refs[`refs/heads/${cloneBranch}`] });
      completedSteps.push('clone');
      // Only the Agent can interpret newly obtained project rules. Yield before changing shared configuration.
      return await record({ ...plan, status: 'needs_project_rules', complete: false, completedSteps, local: await inspect(local.project, options), message: '已克隆到选定目录；Agent 需读取新取得的项目规则与共享配置，然后沿用本次远端选择重新计划并完成设置。本次暂未提交和推送。' });
    }
    step = 'remote';
    if (selection.action === 'replace') await replaceRemote(local.project, { ...options, name: selection.name, expectedFetch: existing.fetch[0], expectedPush: existing.push[0], expectedState: remoteStateFingerprint });
    else await addRemote(local.project, { ...options, name: selection.name });
    completedSteps.push('remote'); step = 'shared_config';
    const config = { ...(shared.config || { schemaVersion: 1 }), remotes: { ...shared.config?.remotes, [selection.name]: options.url } };
    if (JSON.stringify(config) !== JSON.stringify(shared.config)) await writeProject(local.project, config, { expectedFingerprint: shared.fingerprint });
    completedSteps.push('shared_config'); step = 'readback';
    const state = await inspect(local.project, options);
    const actual = state.remotes.find(r => r.name === selection.name);
    if (actual?.fetch.length !== 1 || actual.fetch[0] !== options.url || actual.push.length !== 1 || actual.push[0] !== options.url) fail('setup_readback', '远端实际地址与计划不一致，请检查 URL 重写或并发配置变化。');
    const verified = await inspectRemote(local.project, options.url, options);
    const saved = await readProjectConfig(local.project);
    if (JSON.stringify(saved.config) !== JSON.stringify(config)) fail('setup_readback', '共享配置回读与计划不一致。');
    return await record({ ...plan, status: 'configured', message, local: state, remote: verified, issues: await issueStatus(state, options), completedSteps: [...completedSteps, 'readback'], complete: true });
  } catch (error) {
    let actual;
    try { actual = await inspect(local.project, options); } catch { actual = null; }
    const result = { ...plan, status: 'incomplete', complete: false, completedSteps, failedStep: step, local: actual, error: { code: error.code, message: redact(error.message) }, message: 'Git 设置未完成。已保留实际文件与配置；重新检查后补齐缺项，不自动清理、回滚或重试克隆。本次暂未提交和推送。' };
    try { await record(result); } catch (recordError) { result.recordError = { code: recordError.code, message: redact(recordError.message) }; }
    return result;
  }
}
