import { readFile, readdir, lstat, realpath } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { devNull } from 'node:os';
import { isDeepStrictEqual } from 'node:util';
import { inspectProject } from './config.mjs';
import { inspect } from './git.mjs';
import { localGit } from './auth-roots.mjs';
import { runtimeBundle, safePath, digest, runtimeFail as fail } from './runtime-bundle.mjs';
import { readChoice, fields, portablePath, publicText, confirmedTargets, readHarnessConfig, validateHarnessConfig } from './harness-config.mjs';
import { projectLauncher } from './project-launcher.mjs';
import { registrationEdits, ruleSnapshot, withoutRegistration } from './harness-rules.mjs';
import { registeredRepositories, selectRepository } from './repository-relationship.mjs';
import { projectFile, installationFiles } from './harness-files.mjs';

export const standardBehavior = {
  inspect: '明确调用 Skill 时读取实际规则并比较实现；日常入口只核对必要运行文件、目标、绑定和计划。',
  publish: '显式发布指定本地 refs；先扫描历史，再逐引用推送并回读，不自动同步。',
  sync: '显式同步配置中的远端来源和完整 refs；保留目标独有引用，不强推、镜像或删除。',
  authentication: '每个完整目标使用自己的已选来源；Git helper 安装在本仓 Git common directory，失效停止。',
  extensions: '只执行明确选入且内容指纹一致的 Node 项目扩展；在对应操作全部成功后执行一次，结果不明不重发。',
  automation: '不新增自动 hooks、CI、后台服务、标签或 Release；已有触发关系必须分析后明确保留或修改。',
};
const json = value => `${JSON.stringify(value, null, 2)}\n`;
const snapshotHash = files => digest(Object.entries(files).sort(([a], [b]) => a.localeCompare(b)));

/** Candidate discovery is evidence collection, never a claim of semantic equivalence. */
export async function harnessFacts(project, { evidenceFiles = [], recoveryPaths = [], directory, ruleFiles } = {}) {
  const state = await inspectProject(project), root = state.root;
  const saved = state.isGit ? await readChoice(root) : null;
  directory = directory ?? saved?.directory ?? null;
  if (directory) portablePath(directory);
  ruleFiles = ruleFiles ?? saved?.ruleFiles ?? [];
  const rules = [];
  for (const name of ruleFiles) rules.push(await ruleSnapshot(root, name));
  const files = {}, candidates = [], unknown = [];
  let total = 0;
  async function visit(path, label, force = false, depth = 0) {
    if ((directory && path === join(root, directory)) || recoveryPaths.includes(path)) return; // Only the current apply's known backup is excluded from its final choice.
    let info;
    try { info = await lstat(path); } catch (error) { if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return; throw error; }
    if (info.isSymbolicLink()) {
      const target = await realpath(path), targetInfo = await lstat(target);
      if (!targetInfo.isFile() || targetInfo.size > 262144) { files[label] = 'symlink'; unknown.push({ path, reason: 'directory_link_requires_explicit_file_evidence' }); return; }
      const sha256 = digest({ target, content: digest(await readFile(target)) });
      files[label] = sha256;
      if (force || /\b(?:git|gh)\s+(?:push|fetch|pull|config|credential|release)|repo-push|repo-harness|git-setup|release-on-push|hooksPath/i.test(withoutRegistration(await readFile(target, 'utf8')))) candidates.push({ path: target, link: path, evidence: label, sha256 });
      return;
    }
    if (info.isDirectory()) {
      if (depth > 12) { unknown.push({ path, reason: 'depth_budget' }); return; }
      for (const name of (await readdir(path)).sort()) {
        if (['.git', '.credentials', 'node_modules', '.venv', 'tmp', 'data'].includes(name) || name.endsWith('.sample')) continue;
        await visit(join(path, name), `${label}/${name}`, force, depth + 1);
      }
      return;
    }
    if (!info.isFile() || info.size > 262144 || total + info.size > 8 * 1024 * 1024 || Object.keys(files).length >= 500) { unknown.push({ path, reason: 'inspection_budget_or_nonregular_file' }); return; }
    total += info.size;
    const raw = await readFile(path);
    files[label] = digest(raw);
    if (force || /\b(?:git|gh)\s+(?:push|fetch|pull|config|credential|release)|repo-push|repo-harness|git-setup|release-on-push|hooksPath/i.test(withoutRegistration(raw.toString('utf8')))) candidates.push({ path, evidence: label, sha256: files[label] });
  }
  for (const name of ['scripts', 'harness', '.githooks', '.husky', '.github/workflows', '.gitlab-ci.yml', '.cnb.yml', 'package.json', 'Makefile', 'AGENTS.md', 'CLAUDE.md']) await visit(join(root, name), name, /^(?:\.githooks|\.husky|\.github|\.gitlab|\.cnb)/.test(name));
  for (const name of ruleFiles) await visit(join(root, name), name);
  for (const name of new Set([...(saved?.evidenceFiles || []), ...evidenceFiles])) { portablePath(name); await visit(join(root, name), name, true); }
  const hooksPath = state.isGit ? (await localGit(root, ['config', '--path', '--get', 'core.hooksPath'], true))?.trim() || join(state.commonDir, 'hooks') : null;
  if (hooksPath && hooksPath !== devNull) await visit(resolve(root, hooksPath), '@git-hooks', true);
  let installed = null;
  try {
    if (!directory) throw Object.assign(new Error('No installation selected'), { code: 'ENOENT' });
    const path = join(root, directory);
    await safePath(root, path);
    await lstat(path);
    const snapshot = await installationFiles(path), actual = snapshot.files;
    files['@standard'] = snapshot.fingerprint;
    let manifest;
    try { manifest = JSON.parse(await readFile(join(path, 'manifest.json'), 'utf8')); }
    catch (error) {
      if (error.code !== 'ENOENT' || (await readdir(path, { withFileTypes: true })).some(entry => entry.name !== 'extensions' || !entry.isDirectory())) fail('harness_manifest_invalid', '标准目录已存在但没有有效清单；不覆盖。');
      installed = { path, projectFiles: snapshot.projectFiles, changed: [], status: 'project_files_only' };
    }
    if (!installed) {
      fields(manifest, ['schemaVersion', 'standard', 'sourceVersion', 'sourceFingerprint', 'files', 'comparison', 'evidenceFiles', 'directory', 'ruleFiles'], '标准清单');
      publicText(manifest);
      if (![1, 2].includes(manifest.schemaVersion) || manifest.standard !== 'git-setup' || !manifest.files || typeof manifest.files !== 'object' || Array.isArray(manifest.files) || !/^[a-f0-9]{64}$/.test(manifest.sourceFingerprint)
        || Object.entries(manifest.files).some(([name, value]) => { portablePath(name); return !/^[a-f0-9]{64}$/.test(value); })) fail('harness_manifest_invalid', '标准 Harness 清单无效。');
      const changed = Object.keys({ ...manifest.files, ...actual }).filter(name => name !== 'manifest.json' && !projectFile(name) && manifest.files[name] !== actual[name]);
      if (actual['repos.conf']) await registeredRepositories(root, { harnessDirectory: directory });
      const config = await readHarnessConfig(root, directory);
      installed = { path, manifest, config: config.config, configFingerprint: config.fingerprint, projectFiles: snapshot.projectFiles, changed, status: changed.length ? 'modified' : 'installed' };
    }
  } catch (error) {
    if (error.code !== 'ENOENT') installed = { status: 'invalid', code: error.code, message: error.message };
  }
  const bundle = await runtimeBundle();
  const fingerprint = digest({ root, commonDir: state.commonDir, files: snapshotHash(files), rules, directory, remotes: state.isGit ? (await inspect(root)).remotes : [], hooksPath, standard: bundle.fingerprint });
  const currentChoice = saved?.fingerprint === fingerprint ? saved.choice : null;
  return { root, directory, ruleFiles, commonDir: state.commonDir, fingerprint, files, candidates, unknown, hooksPath,
    automation: {
      localHooks: { status: !state.isGit ? 'not_applicable' : hooksPath === devNull ? 'disabled' : 'requires_behavior_review', scope: 'current_repository', path: hooksPath },
      remoteCI: { status: 'not_verified', message: 'core.hooksPath 只影响本仓 Git hooks，不决定远端 CI 是否触发；本机工作流文件也不证明平台已启用或运行成功。' },
    },
    standard: { version: bundle.version, fingerprint: bundle.fingerprint, behavior: standardBehavior }, installed,
    updateAvailable: Boolean(installed?.manifest && installed.manifest.sourceFingerprint !== bundle.fingerprint),
    choice: currentChoice, previousChoice: saved?.choice ?? null, choiceChanged: Boolean(saved && !currentChoice),
    comparison: candidates.length || unknown.length ? 'agent_review_required' : 'no_existing_entry_found_in_inspected_paths',
    note: '文件指纹只证明内容变化；Agent 必须读取调用链和项目规则后比较真实行为。' };
}

function validateComparison(input, facts) {
  const behaviors = input.comparison || [];
  if (!Array.isArray(behaviors)) fail('harness_comparison_required', '比较结果必须逐项列出实际行为。');
  const covered = new Set(), ids = new Set();
  for (const item of behaviors) {
    fields(item, ['id', 'summary', 'trigger', 'inputs', 'writes', 'failure', 'evidence', 'classification', 'decision', 'extension'], '行为比较');
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(item.id) || ids.has(item.id) || !['standard', 'configuration', 'unique', 'conflict', 'unknown'].includes(item.classification)
      || !['summary', 'trigger', 'inputs', 'writes', 'failure'].every(key => typeof item[key] === 'string' && item[key].trim()) || !Array.isArray(item.evidence) || !item.evidence.length) fail('harness_comparison_required', '每项比较必须说明触发、输入、写入、失败处理和文件证据。');
    ids.add(item.id);
    for (const file of item.evidence) {
      if (!Object.hasOwn(facts.files, file)) fail('harness_evidence_changed', '比较引用了本次未检查的文件；通过 evidenceFiles 显式补充调用链。');
      covered.add(file);
    }
    if (input.choice === 'standard' && (['conflict', 'unknown'].includes(item.classification) || (item.classification === 'unique' && !['retain', 'drop'].includes(item.decision)))) fail('harness_behavior_unresolved', '采用标准前须解决冲突和未知项，并逐项明确独有功能是否重新实现。');
    if (item.decision === 'retain' && (!item.extension || !input.extensions?.some(extension => extension.id === item.extension))) fail('harness_extension_required', '选择重新实现的功能必须对应一个已提供的项目扩展。');
  }
  if (facts.candidates.some(file => !covered.has(file.evidence)) || (facts.unknown.length && input.choice === 'standard')) fail('harness_comparison_required', '尚有入口或调用关系未完成实际行为比较；不自动采用或替换。');
  for (const extension of input.extensions || []) if (!behaviors.some(item => item.decision === 'retain' && item.extension === extension.id)
    && !facts.installed?.config?.extensions.some(previous => digest(previous) === digest(extension))) fail('harness_extension_unselected', '新增或改选扩展必须对应用户明确选择重新实现的功能。');
}

const readme = directory => `# 项目 Git 操作\n\n本目录由 git-setup 生成，可在移除 Skill 后独立运行，需要 Node.js >=20、Git >=2.29；选择 gh 时还需已登录的 GitHub CLI。\n\n请让 Agent 从本项目的 ${directory}/run.mjs 入口检查、发布或同步。无参数只检查；发布指定本地完整 refs，同步只使用 config.json 的来源、目标和完整 refs。两者分别调用，不自动互相触发，不创建提交、标签、Release、hooks 或 CI。\n\n远端地址从 Git 配置读取；config.json 中 targets 保存用户已确认的目标身份，用于检测地址变化。凭证沿用主检出或明确统筹外仓主检出的 .credentials/repositories/，每个目标独立；本目录不保存凭证或本机账号。Git helper 位于实际 Git common directory，新的 clone 需重新选择并安装本机鉴权。\n\n入口命令：check；read --remote <名称>；publish --remote <名称> --ref <完整引用>；sync。选仓加 --repository <登记路径>，外仓自身用 .；省略时只操作本项目。可加 --checkout <所选仓的检出>，相对本项目解释。每仓策略在 config.json 的 repositories 中保存，缺少时停止，不套用外仓策略。计划区分安装位置、实际检出、登记依据、远端、完整 refs 和鉴权来源；相关事实变化拒绝旧计划。auth-info/auth-prepare/auth-verify 也接受相同选仓参数。发布与同步先生成计划，再以同参数加 --apply --expected-fingerprint <指纹> 执行。有扩展时还需 --operation-id <本次稳定标识>。扩展只在对应操作全部成功后执行；中断或失败不自动重新执行，先检查 data/git-setup/ 中记录及真实结果。\n\n版本和源文件指纹在 manifest.json；升级先由 Agent 比较新旧实现及项目扩展，再按具体文件计划执行。生成器维护 runtime/、run.mjs、README.md 和清单；项目维护 config.json、可选 repos.conf 以及 extensions/ 下的扩展和资源。无扩展不创建空目录，旧目录外扩展仍有效。升级省略策略时保留已有配置、扩展和普通文件权限；明确停用扩展也保留文件。新增或改变扩展需确认内容指纹，原目标身份只有明确要求刷新时才更新。计划后文件变化会停止安装；中断后检查原记录、阶段目录和备份再继续，不覆盖后续修改。配置检查不等于远端鉴权或推送权限验证。\n`;

export async function planHarness(project, input) {
  fields(input, ['choice', 'directory', 'ruleFiles', 'evidenceFiles', 'comparison', 'sync', 'repositories', 'extensions', 'refreshTargets', 'edits'], 'Harness 选择');
  publicText(input);
  if (input.refreshTargets !== undefined && typeof input.refreshTargets !== 'boolean') fail('harness_config_invalid', 'refreshTargets 必须是布尔值。');
  if (!['keep', 'standard'].includes(input.choice)) fail('harness_choice_required', '在会话明确选择保留现有 Harness 或采用标准。');
  const state = await inspect(project);
  if (!state.initialized || state.bare || state.insideParent) fail('harness_project_required', '生成或保存 Harness 选择前先完成目标仓库初始化；不修改父仓。');
  const saved = await readChoice(state.root);
  const directory = input.directory ?? saved?.directory ?? null;
  const ruleFiles = input.ruleFiles ?? saved?.ruleFiles ?? [];
  if (input.choice === 'standard' && (!directory || !Array.isArray(ruleFiles) || !ruleFiles.length)) fail('harness_layout_required', '采用标准须明确项目内 directory 和适用的 ruleFiles，纳入本次计划。');
  if (directory) portablePath(directory);
  if (!Array.isArray(ruleFiles)) fail('harness_rules_invalid', 'ruleFiles 必须是项目相对路径列表。');
  ruleFiles.forEach(portablePath);
  if (saved?.directory && saved.directory !== directory) fail('harness_location_changed', '已登记安装位置变化；先明确旧入口的迁移方案，不自动创建第二套入口。');
  const facts = await harnessFacts(project, { evidenceFiles: input.evidenceFiles, directory, ruleFiles });
  input = { ...input, evidenceFiles: input.evidenceFiles ?? saved?.evidenceFiles ?? [] };
  if (input.choice === 'standard') input.extensions = input.extensions ?? facts.installed?.config?.extensions ?? [];
  validateComparison(input, facts);
  let config = null;
  const files = {}, edits = [], preserved = {};
  const repositories = {}, repositoryFacts = {};
  if (input.choice === 'keep' && ((input.edits?.length || input.extensions?.length) || input.sync !== undefined || input.repositories !== undefined || input.refreshTargets)) fail('harness_keep_readonly', '保留现有 Harness 时不生成脚本、不改仓库策略或扩展。');
  if (input.choice === 'standard') {
    if (facts.installed && ['modified', 'invalid'].includes(facts.installed.status)) fail('harness_existing_modified', '已有标准目录被修改或无法识别；先将实际修改纳入项目扩展，不覆盖。');
    const policies = input.repositories ?? facts.installed?.config?.repositories ?? {};
    fields(policies, Object.keys(policies), '各仓策略选择');
    for (const [name, policy] of Object.entries(policies)) {
      fields(policy, input.repositories ? ['sync'] : ['targets', 'sync'], '各仓策略选择');
      const selection = await selectRepository(state.root, { repository: name, harnessDirectory: directory });
      const actual = await inspect(selection.project.root);
      repositories[name] = { targets: input.repositories ? confirmedTargets(actual) : policy.targets, sync: policy.sync ?? null };
      repositoryFacts[name] = { relationship: selection.relationship.fingerprint, remotes: actual.remotes };
    }
    config = validateHarnessConfig({ ...(facts.installed?.config || { schemaVersion: 1, targets: confirmedTargets(state) }),
      ...(input.refreshTargets ? { targets: confirmedTargets(state) } : {}),
      sync: input.sync ?? (input.sync === null ? null : facts.installed?.config?.sync ?? null), extensions: input.extensions,
      ...(input.repositories !== undefined || Object.keys(repositories).length ? { repositories } : {}) }, directory);
    for (const extension of config.extensions) {
      await safePath(state.root, join(state.root, extension.path));
      if (digest(await readFile(join(state.root, extension.path))) !== extension.sha256) fail('harness_extension_changed', '项目扩展内容与本次选择不同。');
    }
    const bundle = await runtimeBundle();
    for (const [name, content] of Object.entries(bundle.files)) files[`runtime/${name}`] = content;
    files['run.mjs'] = projectLauncher(directory);
    if (!isDeepStrictEqual(config, facts.installed?.config)) files['config.json'] = json(config);
    files['README.md'] = readme(directory);
    for (const [name, record] of Object.entries(facts.installed?.projectFiles || {})) if (!Object.hasOwn(files, name)) preserved[name] = record;
    files['manifest.json'] = json({ schemaVersion: 2, standard: 'git-setup', directory, ruleFiles, sourceVersion: bundle.version, sourceFingerprint: bundle.fingerprint,
      files: Object.fromEntries(Object.entries(files).filter(([name]) => !projectFile(name)).map(([name, content]) => [name, digest(content)])), comparison: input.comparison || [], evidenceFiles: input.evidenceFiles || [] });
    for (const edit of input.edits || []) {
      fields(edit, ['path', 'content'], '已确认入口修改'); portablePath(edit.path);
      if (edit.path === directory || edit.path.startsWith(`${directory}/`) || !Object.hasOwn(facts.files, edit.path) || typeof edit.content !== 'string' || ruleFiles.includes(edit.path)) fail('harness_edit_invalid', '只允许修改计划内已检查的入口；规则登记由 ruleFiles 生成。');
      await safePath(state.root, join(state.root, edit.path));
      edits.push({ ...edit, before: facts.files[edit.path], after: digest(edit.content) });
    }
    edits.push(...await registrationEdits(state.root, ruleFiles, directory));
    publicText(edits);
    for (const edit of edits) edit.evidenceNames = facts.candidates.filter(item => item.path === join(state.root, edit.path)).map(item => item.evidence);
    if (new Set(edits.map(item => item.path)).size !== edits.length) fail('harness_edit_invalid', '入口修改路径重复。');
  }
  const context = { facts: facts.fingerprint, remotes: state.remotes, repositoryFacts, commonDir: state.commonDir, installedFingerprint: facts.files['@standard'] ?? null, observedFiles: facts.files, hooksPath: facts.hooksPath, standard: facts.standard.fingerprint, choiceBefore: saved };
  const modified = [...edits.map(edit => edit.path), ...(facts.installed?.config && files['config.json'] ? [`${directory}/config.json`] : [])];
  return { schemaVersion: 1, status: 'planned', choice: input.choice, project: state.root, directory, ruleFiles, input, context, config,
    fingerprint: digest({ context, input }), files, edits, preserved,
    changes: { generated: Object.keys(files).map(name => `${directory}/${name}`).filter(name => !modified.includes(name)), preserved: Object.keys(preserved).map(name => `${directory}/${name}`), modified, legacyConfig: 'preserved', gitConfig: 'repository-local Harness choice only' },
    actualWrites: 'not_performed', committed: false, pushed: false };
}

export async function applyHarness(project, input, { expectedFingerprint, planFile } = {}) {
  const plan = await planHarness(project, input);
  if (!expectedFingerprint || expectedFingerprint !== plan.fingerprint) fail('harness_plan_changed', '项目入口、标准、远端或选择发生变化；重新检查后使用当前计划。');
  if (!planFile) fail('harness_record_required', '应用前提供已忽略的 --plan-file，供中断后核对选择和实际完成步骤。');
  const { beginHarness } = await import('./harness-transaction.mjs');
  return beginHarness(plan, planFile);
}
