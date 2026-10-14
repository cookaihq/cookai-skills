import { readFile, lstat, mkdir, rename, rm, open, writeFile, link } from 'node:fs/promises';
import { join, dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { hostname } from 'node:os';
import { harnessFacts, planHarness } from './harness.mjs';
import { inspect, updateLocalConfig, redact } from './git.mjs';
import { localGit } from './auth-roots.mjs';
import { choiceKey, readChoice } from './harness-config.mjs';
import { selectRepository } from './repository-relationship.mjs';
import { verifyEdit, ruleSnapshot } from './harness-rules.mjs';
import { saveSetupRecord } from './setup-record.mjs';
import { digest, writePackage, safePath, runtimeFail as fail } from './runtime-bundle.mjs';
import { installationFiles, plannedInstallation, copyProjectFiles } from './harness-files.mjs';

const hashFiles = files => digest(Object.entries(files).sort(([a], [b]) => a.localeCompare(b)));
const lockPath = plan => join(plan.context.commonDir, 'git-setup-harness.lock');
const generatedHash = plan => plannedInstallation(plan).fingerprint;
async function assertOwner(tx) {
  await safePath(tx.plan.context.commonDir, lockPath(tx.plan));
  let actual;
  try { actual = JSON.parse(await readFile(lockPath(tx.plan), 'utf8')); } catch { fail('harness_recovery_changed', '原事务锁已消失或无法读取；不继续写入。'); }
  if (digest(actual) !== digest(tx.owner)) fail('harness_recovery_changed', '原事务锁已由其他操作持有；不删除或覆盖。');
}
async function snapshot(root, path) {
  if (!path) return null;
  await safePath(root, path);
  try { await lstat(path); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  return (await installationFiles(path)).fingerprint;
}
const requestFor = plan => ({ project: plan.project, input: plan.input, expectedFingerprint: plan.fingerprint });
async function checkpoint(tx, status = 'applying', extra = {}) {
  return saveSetupRecord(tx.file, { request: requestFor(tx.plan), transaction: tx,
    result: { status, complete: false, changes: tx.plan.changes, completed: tx.completed, stage: tx.stage, backup: tx.backup, ...extra, committed: false, pushed: false } });
}

async function observe(tx) {
  const { plan } = tx, root = plan.project;
  for (const [kind, path] of [['stage', tx.stage], ['backup', tx.backup]]) {
    if (path && (!plan.directory || dirname(path) !== dirname(join(root, plan.directory)) || !path.startsWith(join(dirname(path), `.git-setup-${kind}-`)))) fail('harness_recovery_invalid', '记录中的残留目录不属于原安装位置。');
  }
  const facts = await harnessFacts(root, { directory: plan.directory, ruleFiles: plan.ruleFiles, evidenceFiles: plan.input.evidenceFiles, recoveryPaths: [tx.stage, tx.backup].filter(Boolean) });
  const state = await inspect(root);
  const ignored = new Set(['@standard', ...plan.edits.flatMap(edit => [edit.path, ...(edit.evidenceNames || []), ...(edit.aliases || []).map(alias => alias.path)])]);
  // Installation evidence moves with the directory; the full snapshots below
  // verify it at the current, stage and backup locations during rename recovery.
  const unrelated = files => Object.fromEntries(Object.entries(files).filter(([name]) => !ignored.has(name)
    && !(plan.directory && (name === plan.directory || name.startsWith(`${plan.directory}/`)))));
  if (state.root !== root || facts.commonDir !== plan.context.commonDir || digest(state.remotes) !== digest(plan.context.remotes)
    || facts.hooksPath !== plan.context.hooksPath || facts.standard.fingerprint !== plan.context.standard
    || hashFiles(unrelated(facts.files)) !== hashFiles(unrelated(plan.context.observedFiles))) fail('harness_recovery_changed', '原计划的仓库、目标、标准或入口证据发生变化；保留现场，先重新审阅。');
  const edits = [];
  for (const edit of plan.edits) {
    let applied = false;
    try { await verifyEdit(root, edit); }
    catch {
      try { await verifyEdit(root, edit, true); applied = true; }
      catch { fail('harness_recovery_changed', '中断后入口原文或规则实体变化；不覆盖用户修改。'); }
    }
    edits.push({ path: edit.path, applied });
  }
  const choice = await readChoice(root);
  if (digest(choice) !== digest(plan.context.choiceBefore) && !(choice?.choice === plan.choice && choice.fingerprint === facts.fingerprint && choice.directory === plan.directory)) fail('harness_recovery_changed', '本机 Harness 选择已经改变；不覆盖新的选择。');
  const directory = plan.directory ? join(root, plan.directory) : null;
  const current = await snapshot(root, directory), stage = await snapshot(root, tx.stage), backup = await snapshot(root, tx.backup);
  const expected = generatedHash(plan);
  if (plan.choice === 'standard' && (![null, plan.context.installedFingerprint, expected].includes(current)
    || (stage !== null && stage !== expected) || (backup !== null && backup !== plan.context.installedFingerprint)
    || (current === null && plan.context.installedFingerprint !== null && backup === null))) fail('harness_recovery_changed', '生成目录、临时目录或备份与原计划不符；保留残留供核查。');
  for (const [repository, expectedFacts] of Object.entries(plan.context.repositoryFacts || {})) {
    try {
      const selection = await selectRepository(root, { repository, harnessDirectory: plan.directory,
        registrySourceDirectory: current !== null ? directory : stage !== null ? tx.stage : tx.backup });
      const actual = await inspect(selection.project.root);
      if (digest({ relationship: selection.relationship.fingerprint, remotes: actual.remotes }) !== digest(expectedFacts)) throw new Error('changed');
    } catch { fail('harness_recovery_changed', '原计划的子仓登记、检出或远端发生变化；保留现场，不继续应用。'); }
  }
  return { current, stage, backup, edits, choice, factsFingerprint: facts.fingerprint };
}

async function finish(tx, observation) {
  const { plan } = tx, root = plan.project;
  const directory = plan.directory ? join(root, plan.directory) : null;
  try {
    if (plan.choice === 'standard') {
      if (observation.current !== generatedHash(plan)) {
        await safePath(root, directory); await mkdir(dirname(directory), { recursive: true });
        if (!tx.stage) { tx.stage = join(dirname(directory), `.git-setup-stage-${randomUUID()}`); await checkpoint(tx); }
        if (!observation.stage) {
          await mkdir(tx.stage, { mode: 0o700 }); await writePackage(tx.stage, plan.files);
          await copyProjectFiles(observation.current !== null ? directory : tx.backup, tx.stage, plan.preserved || {});
        }
        await observe(tx); // Recheck originals after writing the isolated package.
        if (observation.current !== null) {
          if (observation.backup !== null) fail('harness_recovery_changed', '原目录和备份同时存在；先核对现场，不覆盖备份。');
          if (!tx.backup) { tx.backup = join(dirname(directory), `.git-setup-backup-${randomUUID()}`); await checkpoint(tx); }
          await rename(directory, tx.backup);
        }
        await rename(tx.stage, directory); tx.stage = null;
      }
      if (!tx.completed.includes('generated')) tx.completed.push('generated');
      await checkpoint(tx);
      for (const edit of plan.edits) {
        const path = join(root, edit.path);
        const current = await ruleSnapshot(root, edit.path);
        if (current.before !== edit.after) {
          await verifyEdit(root, edit);
          await mkdir(dirname(path), { recursive: true });
          const temporary = `${path}.git-setup-${randomUUID()}`;
          try {
            await writeFile(temporary, edit.content, { flag: 'wx', mode: edit.mode ?? current.mode });
            await verifyEdit(root, edit);
            await rename(temporary, path);
          } finally { await rm(temporary, { force: true }); }
        }
        await verifyEdit(root, edit, true);
        if (!tx.completed.includes(edit.path)) tx.completed.push(edit.path);
        await checkpoint(tx);
      }
    }
    await observe(tx);
    const facts = await harnessFacts(root, { directory: plan.directory, ruleFiles: plan.ruleFiles, evidenceFiles: plan.input.evidenceFiles, recoveryPaths: [tx.stage, tx.backup].filter(Boolean) });
    if (plan.choice === 'standard' && facts.installed?.status !== 'installed') fail('harness_readback', '生成产物回读不一致。');
    const choice = { choice: plan.choice, directory: plan.directory, ruleFiles: plan.ruleFiles, fingerprint: facts.fingerprint, evidenceFiles: plan.input.evidenceFiles || [], comparison: plan.input.comparison || [] };
    await updateLocalConfig(root, {}, async file => { await localGit(root, ['config', '--file', file, choiceKey(root), JSON.stringify(choice)]); });
    if (digest(await readChoice(root)) !== digest(choice)) fail('harness_readback', '保存的选择回读不一致。');
    if (!tx.completed.includes('choice')) tx.completed.push('choice');
    await checkpoint(tx);
    if (tx.backup) {
      if (await snapshot(root, tx.backup) !== null) await rm(tx.backup, { recursive: true });
      tx.backup = null;
    }
    const result = { status: 'configured', choice: plan.choice, complete: true, fingerprint: facts.fingerprint, changes: plan.changes, completed: tx.completed, committed: false, pushed: false };
    await saveSetupRecord(tx.file, { request: requestFor(plan), transaction: tx, result });
    await assertOwner(tx);
    await rm(lockPath(plan));
    return result;
  } catch (error) {
    const extra = { code: error.code, message: redact(error.message) };
    await checkpoint(tx, 'incomplete', extra);
    return { status: 'incomplete', complete: false, completed: tx.completed, stage: tx.stage, backup: tx.backup, ...extra, committed: false, pushed: false };
  }
}

/** Saving a new preview must never replace an unfinished generation receipt. */
export async function saveHarnessPreview(file, plan) {
  return saveSetupRecord(file, { request: { project: plan.project, input: plan.input }, result: plan }, {
    beforeWrite: async () => {
      try {
        await lstat(lockPath(plan));
        fail('harness_recovery_required', '已有生成或恢复锁；先核对原记录，不用新预览覆盖。');
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      let previous;
      try { previous = JSON.parse(await readFile(resolve(file), 'utf8')); }
      catch (error) { if (error.code === 'ENOENT') return; fail('harness_recovery_invalid', '原记录无法读取；不覆盖。'); }
      if (previous.transaction && previous.result?.complete !== true) fail('harness_recovery_required', '原生成尚未完成；先恢复或核对现场，不覆盖阶段记录。');
    },
  });
}

export async function beginHarness(plan, file) {
  const tx = { plan, file: resolve(file), owner: { id: randomUUID(), pid: process.pid, host: hostname() }, completed: [], stage: null, backup: null };
  let lock, recorded = false;
  try {
    lock = await open(lockPath(plan), 'wx', 0o600);
    await lock.writeFile(JSON.stringify(tx.owner));
    if ((await planHarness(plan.project, plan.input)).fingerprint !== plan.fingerprint) fail('harness_plan_changed', '应用开始前项目事实变化；未生成文件。');
    await checkpoint(tx); recorded = true;
    return await finish(tx, await observe(tx));
  } catch (error) {
    if (recorded) await checkpoint(tx, 'incomplete', { code: error.code, message: redact(error.message) });
    return { status: 'incomplete', complete: false, code: error.code, message: redact(error.message), committed: false, pushed: false };
  } finally {
    if (lock) { await lock.close(); if (!recorded) await rm(lockPath(plan)); }
  }
}

export async function recoverHarness(project, file, { apply = false, expectedFingerprint } = {}) {
  file = resolve(file);
  await safePath(dirname(file), file);
  let record;
  try { record = JSON.parse(await readFile(file, 'utf8')); } catch { fail('harness_recovery_invalid', '无法读取原生成记录；不猜测恢复步骤。'); }
  const tx = record.transaction;
  if (!tx || tx.plan?.project !== resolve(project) || tx.file !== file || digest(record.request) !== digest(requestFor(tx.plan))) fail('harness_recovery_invalid', '恢复记录不属于本项目或原计划。');
  let owner;
  try { owner = JSON.parse(await readFile(lockPath(tx.plan), 'utf8')); } catch (error) {
    if (error.code === 'ENOENT' && record.result.complete) return { status: 'previous_completed', verification: 'historical_receipt_only', actualWrites: 'not_performed' };
    fail('harness_recovery_invalid', '原锁无法核对；保留现场，不自动重建锁。');
  }
  if (digest(owner) !== digest(tx.owner) || owner.host !== hostname()) fail('harness_recovery_busy', '锁不属于本机原操作；先核对持有者。');
  // Each dead owner has one exclusive successor. Never unlink a stale lock to
  // take ownership: concurrent recoveries compete for the same successor path.
  const recoveryLocks = [];
  let nextLock;
  for (let depth = 0; ; depth++) {
    if (depth > 64 || !/^[a-f0-9-]{36}$/.test(owner.id) || !Number.isSafeInteger(owner.pid) || owner.pid < 1 || owner.host !== hostname()) fail('harness_recovery_invalid', '恢复锁的持有者或链无法核对。');
    let active = true;
    try { process.kill(owner.pid, 0); } catch (error) { if (error.code === 'ESRCH') active = false; }
    if (active) fail('harness_recovery_busy', '原操作或恢复进程仍存在；不能并发恢复。');
    nextLock = `${lockPath(tx.plan)}.recovery-${owner.id}`;
    try {
      await safePath(tx.plan.context.commonDir, nextLock);
      owner = JSON.parse(await readFile(nextLock, 'utf8'));
      if (owner.transaction !== tx.owner.id) fail('harness_recovery_invalid', '恢复锁不属于原事务。');
      recoveryLocks.push({ path: nextLock, owner });
    } catch (error) { if (error.code === 'ENOENT') break; throw error; }
  }
  const observation = await observe(tx);
  const fingerprint = digest({ transaction: digest(tx), observation, recoveryLocks });
  const result = { status: 'recovery_ready', fingerprint, project, choice: tx.plan.choice, directory: tx.plan.directory, completed: tx.completed, stage: tx.stage, backup: tx.backup, recoveryLocks, observation, actualWrites: 'not_performed' };
  if (!apply) return result;
  if (!expectedFingerprint || fingerprint !== expectedFingerprint) fail('harness_recovery_changed', '恢复现场与检查时不同；重新核对，不覆盖文件。');
  const successor = { id: randomUUID(), pid: process.pid, host: hostname(), transaction: tx.owner.id };
  const temporary = `${nextLock}.owner-${successor.id}`;
  let acquired = false, completed = false;
  try {
    await writeFile(temporary, JSON.stringify(successor), { flag: 'wx', mode: 0o600 });
    await link(temporary, nextLock); acquired = true; // O_EXCL with complete metadata.
    await assertOwner(tx); // The original transaction may have finished while this process waited.
    if (digest(JSON.parse(await readFile(file, 'utf8'))) !== digest(record) || digest(await observe(tx)) !== digest(observation)) fail('harness_recovery_changed', '恢复开始前现场变化。');
    const outcome = await finish(tx, observation);
    completed = outcome.complete === true;
    return outcome;
  } finally {
    await rm(temporary, { force: true });
    if (acquired && completed) {
      // finish removed the original lock only after saving the completed receipt.
      // These immutable paths belong to this old transaction, never a new apply.
      for (const path of [...recoveryLocks.map(item => item.path), nextLock]) await rm(path);
    }
  }
}
