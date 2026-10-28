import { constants } from 'node:fs';
import { lstat, mkdir, open, readFile, readdir, realpath, rename, unlink, link } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';
import { hostname } from 'node:os';
import { validateGitHubTarget } from './github.mjs';
import { gitConfiguration } from './git-trust.mjs';

export class IssueError extends Error {
  constructor(code, message, details = {}) { super(message); this.name = 'IssueError'; this.code = code; Object.assign(this, details); }
}
const hash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const operationPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$/;
const noFollow = constants.O_NOFOLLOW || 0;

function authorization(authorized) {
  if (authorized !== true) throw new IssueError('authorization-required', '需要用户对本次具体写入的授权；配置和只读检查不构成创建授权。');
}
function validateOperation(operationId) {
  if (!operationPattern.test(operationId || '')) throw new IssueError('invalid-operation-id', 'operation-id 须为 8–80 位字母、数字、下划线或连字符。');
}
function relativeSafe(value, allowDot = false) {
  if (typeof value !== 'string' || !value || path.isAbsolute(value) || value.includes('\\') || /^[A-Za-z]:/.test(value) || value.split('/').some(part => part === '..' || (!allowDot && part === '.')) || /[\x00-\x1f]/.test(value)) {
    throw new IssueError('unsafe-path', '路径必须是项目内相对路径，不能包含 ..、反斜杠或控制字符。');
  }
  return value;
}
function filenameSafe(value) {
  relativeSafe(value);
  if (value.includes('/') || !value.endsWith('.md') || /[<>:"|?*]/.test(value) || /[ .]$/.test(value) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(value)) {
    throw new IssueError('invalid-filename', '本地 Issue 文件名必须为单个可跨平台使用的 .md 文件名。');
  }
  return value;
}
async function safeDirectory(root, relative, create = false) {
  const base = await realpath(root);
  relativeSafe(relative, true);
  let current = base;
  for (const segment of relative.split('/').filter(part => part && part !== '.')) {
    current = path.join(current, segment);
    let stat;
    try { stat = await lstat(current); }
    catch (error) {
      if (error.code !== 'ENOENT' || !create) throw error;
      try { await mkdir(current); } catch (mkdirError) { if (mkdirError.code !== 'EEXIST') throw mkdirError; }
      stat = await lstat(current);
    }
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new IssueError('unsafe-path', '目标路径包含符号链接或非目录，拒绝写入。');
  }
  return current;
}
async function regularText(file, root) {
  const absoluteRoot = await realpath(root);
  const relative = path.relative(absoluteRoot, path.resolve(file)).split(path.sep).join('/');
  relativeSafe(relative);
  await safeDirectory(absoluteRoot, path.posix.dirname(relative));
  const stat = await lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new IssueError('unsafe-file', '要求项目内普通文件，不能通过符号链接读取。');
  const handle = await open(file, constants.O_RDONLY | noFollow);
  try { return await handle.readFile('utf8'); } finally { await handle.close(); }
}

/** Journal data never stores issue text or credentials. */
async function journalDirectory(projectRoot) {
  if (!projectRoot) throw new IssueError('missing-project', '缺少操作记录所属项目的实际检出路径。');
  const root = await realpath(projectRoot);
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG|CEILING_DIRECTORIES)/.test(key)) delete env[key];
  Object.assign(env, gitConfiguration());
  const git = args => execFileSync('git', ['-C', root, ...args], { env, encoding: 'utf8', timeout: 10_000, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  let top;
  try { top = await realpath(git(['rev-parse', '--show-toplevel'])); }
  catch { throw new IssueError('journal-requires-git', '请先初始化目标项目的 Git，并明确忽略 data/git-setup/，再创建事项或远端仓库。'); }
  if (top !== root) throw new IssueError('journal-project-mismatch', '操作记录项目不是实际 Git 根目录，请重新解析项目。');
  let tracked;
  try { tracked = git(['ls-files', '--', 'data/git-setup']); }
  catch { throw new IssueError('journal-check-failed', '无法检查操作记录是否被 Git 跟踪。'); }
  if (tracked) throw new IssueError('journal-tracked', 'data/git-setup/ 已包含 Git 跟踪文件；不能写入操作记录，请先明确处理版本管理边界。');
  try { git(['check-ignore', '-q', '--', 'data/git-setup/']); }
  catch { throw new IssueError('journal-not-ignored', 'data/git-setup/ 未被 Git 忽略。请明确允许把 /data/git-setup/ 加入该项目忽略规则后重试；脚本不会自行修改 .gitignore。'); }
  return safeDirectory(root, 'data/git-setup', true);
}

async function atomicJson(file, value) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  const handle = await open(temporary, 'wx', 0o600);
  try { await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`); await handle.sync(); }
  finally { await handle.close(); }
  try { await rename(temporary, file); }
  finally { await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}

async function lock(file, action) {
  const owner = { pid: process.pid, host: hostname(), lockId: randomUUID() };
  const temporary = `${file}.owner-${owner.lockId}`;
  const out = await open(temporary, 'wx', 0o600);
  try { await out.writeFile(JSON.stringify(owner)); await out.sync(); } finally { await out.close(); }
  const readOwner = file => regularText(file, path.dirname(file)).then(JSON.parse);
  const busy = () => { throw new IssueError('operation-locked', '同一操作或编号分配正在执行，或遗留锁的主机/进程无法确认；保留现场后重试。', { lockPath: file }); };
  let rootOwner, acquired = false, selected = file;
  const chain = [];
  try {
    // Immutable successor paths prevent two recoveries from unlinking each other's lock.
    for (let depth = 0; ; depth++) {
      try { await link(temporary, selected); acquired = true; break; }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
      let previous;
      try { previous = await readOwner(selected); } catch { busy(); }
      if (depth > 64 || previous.host !== hostname() || !Number.isSafeInteger(previous.pid) || previous.pid < 1 || !/^[a-f0-9-]{36}$/.test(previous.lockId)) busy();
      let dead = false;
      try { process.kill(previous.pid, 0); } catch (error) { dead = error.code === 'ESRCH'; }
      if (!dead) busy();
      rootOwner ||= previous;
      if (selected !== file) chain.push(selected);
      selected = `${file}.recovery-${previous.lockId}`;
    }
    rootOwner ||= owner;
    if (hash(await readOwner(file)) !== hash(rootOwner)) busy();
    return await action();
  } finally {
    await unlink(temporary);
    if (acquired) {
      // A new root belongs to another operation and must never be removed here.
      let current; try { current = await readOwner(file); } catch { /* preserve unknown root */ }
      if (rootOwner && current && hash(current) === hash(rootOwner)) await unlink(file);
      for (const entry of [...chain, ...(selected === file ? [] : [selected])]) await unlink(entry).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
  }
}

function journalRoot(resolution) { return resolution.targetProject || resolution.configOwner || resolution.initiatingProject; }
async function withJournal(resolution, operationId, action) {
  validateOperation(operationId);
  const directory = await journalDirectory(journalRoot(resolution));
  return lock(path.join(directory, `${operationId}.lock`), async () => {
    const file = path.join(directory, `${operationId}.json`);
    let previous;
    try { previous = JSON.parse(await regularText(file, directory)); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    return action({ directory, file, previous, save: value => atomicJson(file, value) });
  });
}

export function contentFields(content, partial = false) {
  if (!content || typeof content !== 'object') throw new IssueError('invalid-content', '缺少事项内容。');
  if ((!partial || content.title !== undefined) && (typeof content.title !== 'string' || !content.title.trim() || /[\r\n]/.test(content.title))) throw new IssueError('invalid-title', 'Issue 标题必须是非空单行文本。');
  if ((!partial || content.body !== undefined) && typeof content.body !== 'string') throw new IssueError('invalid-body', 'Issue 正文必须是文本。');
  const out = {};
  for (const key of ['title', 'body', 'labels', 'assignees', 'state']) {
    if (content[key] === undefined) continue;
    if (['labels', 'assignees'].includes(key) && (!Array.isArray(content[key]) || content[key].some(value => typeof value !== 'string' || !value))) throw new IssueError('invalid-content', `${key} 须为字符串数组。`);
    if (key === 'state' && !['open', 'closed'].includes(content[key])) throw new IssueError('invalid-state', 'Issue 状态只能为 open 或 closed。');
    out[key] = content[key];
  }
  return out;
}
function localTarget(resolution) {
  if (resolution.backend !== 'local' || !resolution.local?.root || !resolution.local?.directory) throw new IssueError('invalid-local-target', '当前解析结果不是完整的本地 Issue 目标。');
  relativeSafe(resolution.local.directory, true);
  return resolution.local;
}
function githubTarget(resolution) {
  if (resolution.backend !== 'github') throw new IssueError('invalid-github-target', '当前解析结果不是 GitHub Issue 目标。');
  return validateGitHubTarget(resolution.github);
}
function sameOperation(previous, record) {
  if (previous.kind !== record.kind || previous.contentHash !== record.contentHash || previous.fingerprint !== record.fingerprint || JSON.stringify(previous.target) !== JSON.stringify(record.target)) {
    throw new IssueError('operation-mismatch', 'operation-id 已用于不同内容或目标，拒绝复用；请核对已有记录。');
  }
}
function slug(title) { return title.trim().toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '').slice(0, 72) || 'issue'; }
const regexEscape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
export function validateLocalRules(config) {
  const pattern = config.filename;
  if (typeof pattern !== 'string' || !pattern || /\{(?!number\}|slug\})/.test(pattern)) throw new IssueError('invalid-filename-pattern', '文件名规则仅支持 {number} 与 {slug}。');
  filenameSafe(pattern.replaceAll('{number}', '01').replaceAll('{slug}', 'issue'));
  if (pattern.includes('{number}')) {
    const { start, width } = config.numbering || {};
    if (!Number.isSafeInteger(start) || start < 1 || !Number.isSafeInteger(width) || width < 1 || width > 12 || pattern.split('{number}').length !== 2) throw new IssueError('invalid-numbering', '明确编号起点和宽度（1–12），文件名只含一个 {number}。');
  } else if (config.numbering !== undefined) throw new IssueError('invalid-numbering', '编号设置必须对应文件名中的 {number}。');
}
async function allocateName(directory, config, content) {
  if (content.filename !== undefined) return { filename: filenameSafe(content.filename) };
  const pattern = config.filename;
  if (!pattern) throw new IssueError('filename-required', '本次输入缺少项目约定的文件命名规则。');
  if (typeof pattern !== 'string' || /\{(?!number\}|slug\})/.test(pattern)) throw new IssueError('invalid-filename-pattern', '文件名规则仅支持 {number} 与 {slug}。');
  let number;
  if (pattern.includes('{number}')) {
    const { start = 1, width = 1 } = config.numbering || {};
    if (!Number.isSafeInteger(start) || start < 1 || !Number.isSafeInteger(width) || width < 1 || width > 12 || pattern.split('{number}').length !== 2) throw new IssueError('invalid-numbering', '编号起点必须为正整数，宽度为 1–12，文件名只含一个 {number}。');
    const matcher = new RegExp(`^${pattern.split(/(\{number\}|\{slug\})/).map(part => part === '{number}' ? '(\\d+)' : part === '{slug}' ? '.+' : regexEscape(part)).join('')}$`);
    let maximum = start - 1;
    for (const name of await readdir(directory)) {
      const found = name.match(matcher);
      if (found && Number.isSafeInteger(Number(found[1]))) maximum = Math.max(maximum, Number(found[1]));
    }
    if (maximum >= Number.MAX_SAFE_INTEGER) throw new IssueError('numbering-exhausted', '本地 Issue 编号已超过安全整数范围，请明确新的编号规则。');
    number = String(maximum + 1).padStart(width, '0');
  }
  return { filename: filenameSafe(pattern.replaceAll('{slug}', slug(content.title)).replaceAll('{number}', number || '')), number };
}

export async function createLocalIssue({ resolution, content, operationId = randomUUID(), authorized = false, validate = async () => {} }) {
  authorization(authorized);
  const config = localTarget(resolution);
  contentFields(content.document === undefined ? content : { title: content.title, body: '' });
  if (content.document !== undefined && (typeof content.document !== 'string' || !content.document.trim())) throw new IssueError('invalid-content', '本地创建的完整 document 不能为空。');
  const root = await realpath(config.root);
  const target = { root, directory: config.directory };
  const requested = { title: content.title, body: content.body, document: content.document, filename: content.filename || null, rule: config.filename || null, numbering: config.numbering || null };
  // Read the template before allocating a journal so a malformed template has no side effects.
  const template = config.template ? await regularText(config.template, config.templateRoot || resolution.configOwner) : content.document === undefined ? '# {title}\n\n{body}\n' : null;
  if (config.templateFingerprint && hash(template) !== config.templateFingerprint) throw new IssueError('template-changed', '本地 Issue 模板自目标解析后发生变化，请重新解析再创建。');
  if (content.document === undefined && (!template.includes('{title}') || !template.includes('{body}'))) throw new IssueError('invalid-template', '正文渲染需要含 {title} 和 {body} 的已有模板；否则提交完整 document。');
  requested.templateHash = hash(template);
  const record = { version: 1, kind: 'local-issue', operationId, target, contentHash: hash(requested), fingerprint: resolution.fingerprint, createdAt: new Date().toISOString(), status: 'prepared' };
  return withJournal(resolution, operationId, async journal => {
    if (journal.previous) {
      sameOperation(journal.previous, record);
      return recoverLocal(journal.previous, journal);
    }
    return lock(path.join(journal.directory, 'local-numbering.lock'), async () => {
      const directory = await safeDirectory(root, config.directory, true);
      const { filename, number } = await allocateName(directory, config, content);
      record.filePath = path.join(directory, filename);
      const variables = { title: content.title, body: content.body, number: number || '', slug: slug(content.title), date: record.createdAt.slice(0, 10) };
      const output = content.document ?? template.replace(/\{(title|body|number|slug|date)\}/g, (_match, name) => variables[name]);
      record.outputHash = hash(output);
      await journal.save(record);
      let file;
      try {
        await validate();
        await safeDirectory(root, config.directory);
        file = await open(record.filePath, 'wx', 0o644);
        await file.writeFile(output); await file.sync();
      } catch (error) {
        record.status = error.code === 'EEXIST' ? 'conflict' : 'ambiguous';
        await journal.save(record);
        throw new IssueError(error.code === 'EEXIST' ? 'file-exists' : 'local-write-incomplete', error.code === 'EEXIST' ? '本地事项文件已存在，拒绝覆盖；请按项目编号规则重新选择文件名。' : '本地写入未完成，请按 operation-id 回读恢复；不能把未完成文件当作成功。', { operationId, filePath: record.filePath });
      } finally { await file?.close(); }
      return recoverLocal(record, journal);
    });
  });
}

async function recoverLocal(record, journal) {
  if (record.status === 'conflict') throw new IssueError('file-exists', '该操作遇到已有文件冲突，没有覆盖；请核对内容后使用新 operation-id。', { operationId: record.operationId, filePath: record.filePath });
  if (!record.filePath) throw new IssueError('local-write-incomplete', '操作记录没有已分配文件路径，需要人工核对。');
  let output;
  try { output = await regularText(record.filePath, record.target.root); }
  catch (error) { if (error.code === 'ENOENT') throw new IssueError('local-write-incomplete', '操作记录存在但本地事项文件尚未完成；请核实后明确重建，脚本不会自动换名。', { operationId: record.operationId }); throw error; }
  if (hash(output) !== record.outputHash) throw new IssueError('local-content-mismatch', '本地事项内容与本次记录不一致，可能发生中断或后续编辑；请核对，脚本不会覆盖。', { filePath: record.filePath, operationId: record.operationId });
  record.status = 'verified';
  record.verifiedAt = new Date().toISOString();
  await journal.save(record);
  return { status: 'verified', backend: 'local', operationId: record.operationId, filePath: record.filePath, message: '已按当前项目规则将 Issue 保存到本地' };
}

export async function previewLocalEdit(resolution, content) {
  const config = localTarget(resolution), existing = resolution.existing;
  const original = await regularText(existing.file, config.root);
  if (hash(original) !== existing.sha256) throw new IssueError('local-content-mismatch', '原事项内容已变化，先读取实际文件；不覆盖后续用户修改。');
  let output;
  if (resolution.action === 'update') {
    if (typeof content?.document !== 'string' || !content.document.trim()) throw new IssueError('invalid-content', '本地更新需要 Agent 按原规则准备的完整 document。');
    output = content.document;
  } else {
    if (typeof content?.body !== 'string' || !content.body.trim()) throw new IssueError('invalid-body', '本地回复需要非空正文。');
    const heading = config.commentsHeading;
    if (typeof heading !== 'string' || !/^#{1,6} [^\r\n]+$/.test(heading)) throw new IssueError('invalid-comments-heading', '必须提供规则约定的完整回复标题行。');
    const lines = original.split(/\r?\n/), index = lines.indexOf(heading);
    const level = heading.indexOf(' ');
    if (index < 0 || lines.slice(index + 1).some(line => new RegExp(`^#{1,${level}} `).test(line))) throw new IssueError('comments-section-mismatch', '回复标题不是原事项最后一个对应层级的章节；Agent 需按原格式准备明确更新。');
    output = original + (original.endsWith('\n') ? '\n' : '\n\n') + content.body + '\n';
  }
  return { original, output };
}

export async function editLocalIssue({ resolution, content, operationId, authorized = false, validate = async () => {} }) {
  authorization(authorized);
  const config = localTarget(resolution), filePath = resolution.existing.file;
  const record = { version: 1, kind: `local-${resolution.action}`, operationId, target: { root: config.root, directory: config.directory }, contentHash: hash({ content, existing: resolution.existing }), fingerprint: resolution.fingerprint, filePath, createdAt: new Date().toISOString(), status: 'prepared' };
  return withJournal(resolution, operationId, async journal => {
    if (journal.previous) { sameOperation(journal.previous, record); return recoverLocal(journal.previous, journal); }
    return lock(path.join(journal.directory, `local-item-${hash(filePath)}.lock`), async () => {
      const { original, output } = await previewLocalEdit(resolution, content);
      record.outputHash = hash(output);
      record.pendingPath = `${filePath}.${operationId}.tmp`;
      await journal.save(record);
      await validate();
      await safeDirectory(config.root, path.relative(config.root, path.dirname(filePath)).split(path.sep).join('/') || '.');
      const temporary = await open(record.pendingPath, 'wx', (await lstat(filePath)).mode & 0o777);
      try { await temporary.writeFile(output); await temporary.sync(); } finally { await temporary.close(); }
      await validate();
      if (await regularText(filePath, config.root) !== original) throw new IssueError('local-content-mismatch', '写入前原事项已变化；保留原文件和临时结果供核对。');
      await rename(record.pendingPath, filePath);
      return recoverLocal(record, journal);
    });
  });
}

const sameGitHubUrl = (actual, expected) => typeof actual === 'string' && actual.toLowerCase() === expected.toLowerCase();
function issueIdentity(issue, target, number) {
  if (!issue || issue.pull_request || !Number.isSafeInteger(issue.number) || issue.number < 1 || typeof issue.html_url !== 'string') throw new IssueError('invalid-issue-response', 'GitHub 响应不是可验证的 Issue。');
  if (number !== undefined && issue.number !== number) throw new IssueError('issue-target-mismatch', 'GitHub 回读了不同的事项编号。');
  const expected = `https://github.com/${target.repository}/issues/${issue.number}`;
  if (!sameGitHubUrl(issue.html_url, expected) || !sameGitHubUrl(issue.repository_url, `https://api.github.com/repos/${target.repository}`)) throw new IssueError('issue-target-mismatch', 'GitHub 返回事项不属于已解析目标仓库，拒绝报告成功。');
  return issue;
}
function verifyContent(issue, content) {
  if (content.title !== undefined && issue.title !== content.title || content.body !== undefined && (issue.body || '') !== content.body || content.state !== undefined && issue.state !== content.state) throw new IssueError('issue-content-mismatch', 'GitHub 回读内容与本次写入不一致，需要人工核对。');
  for (const field of ['labels', 'assignees']) {
    if (!content[field]) continue;
    const actual = (issue[field] || []).map(value => typeof value === 'string' ? value : field === 'labels' ? value.name : value.login).sort();
    if (JSON.stringify(actual) !== JSON.stringify([...content[field]].sort())) throw new IssueError('issue-content-mismatch', `GitHub 回读 ${field} 与请求不一致，可能因权限被平台忽略。`);
  }
}
function contentChecks(content) {
  return Object.fromEntries(Object.entries(content).map(([field, value]) => [field, hash(['labels', 'assignees'].includes(field) ? [...value].sort() : value)]));
}
function verifyRecordedContent(issue, record) {
  for (const [field, expected] of Object.entries(record.contentChecks || {})) {
    let value = issue[field];
    if (['labels', 'assignees'].includes(field)) value = (value || []).map(item => typeof item === 'string' ? item : field === 'labels' ? item.name : item.login).sort();
    if (field === 'body') value ||= '';
    if (value === undefined || hash(value) !== expected) throw new IssueError('issue-content-mismatch', '已找到本次操作标识，但回读内容发生变化；请核对，不自动重新创建。');
  }
}

export async function createRemoteIssue({ resolution, content, operationId = randomUUID(), client, authorized = false, validate = async () => {} }) {
  authorization(authorized);
  const target = githubTarget(resolution);
  const fields = contentFields(content);
  const contentHash = hash(fields);
  const marker = `<!-- git-setup-operation:${operationId} sha256:${contentHash} -->`;
  const payload = { ...fields, body: `${fields.body}\n\n${marker}` };
  const record = { version: 1, kind: 'github-issue', operationId, target: { host: target.host, repository: target.repository }, contentHash, contentChecks: contentChecks(payload), marker, fingerprint: resolution.fingerprint, createdAt: new Date().toISOString(), status: 'prepared' };
  return withJournal(resolution, operationId, async journal => {
    if (journal.previous) { sameOperation(journal.previous, record); return recoverRemote(journal.previous, journal, client); }
    const repository = await client.getRepository(target.repository);
    if (repository.full_name?.toLowerCase() !== target.repository.toLowerCase() || repository.has_issues !== true || repository.archived === true) throw new IssueError('issues-unavailable', '目标仓库身份不匹配、未启用 Issues 或已经归档。');
    await journal.save(record);
    try {
      await validate();
      const result = issueIdentity(await client.createIssue(target.repository, payload, { authorized }), target);
      record.number = result.number;
      record.url = result.html_url;
      record.status = 'created-unverified';
      await journal.save(record);
      const actual = issueIdentity(await client.getIssue(target.repository, result.number), target, result.number);
      verifyContent(actual, payload);
      record.status = 'verified'; record.verifiedAt = new Date().toISOString();
      await journal.save(record);
      return { status: 'verified', backend: 'github', operationId, number: actual.number, url: actual.html_url };
    } catch (error) {
      record.status = error.status >= 400 && error.status < 500 && !error.ambiguous ? 'rejected' : 'ambiguous';
      record.errorCode = error.code || 'unknown';
      await journal.save(record);
      error.operationId = operationId;
      throw error;
    }
  });
}

async function recoverRemote(record, journal, client, { maxPages = 20, budgetMs = 120_000 } = {}) {
  if (!client) throw new IssueError('missing-client', 'GitHub 恢复需要目标仓库对应的鉴权。');
  const target = validateGitHubTarget(record.target);
  const matches = [];
  const deadline = Date.now() + budgetMs;
  let complete = Boolean(record.number);
  if (record.number) {
    matches.push(issueIdentity(await client.getIssue(target.repository, record.number, { deadline }), target, record.number));
  } else {
    const since = new Date(Date.parse(record.createdAt) - 5 * 60_000).toISOString();
    for (let page = 1; page <= maxPages && Date.now() < deadline; page++) {
      const issues = await client.listIssues(target.repository, { page, since, deadline });
      if (!Array.isArray(issues)) throw new IssueError('invalid-issue-response', 'GitHub 列表响应无效。');
      for (const issue of issues) if (!issue.pull_request && typeof issue.body === 'string' && issue.body.includes(record.marker)) matches.push(issueIdentity(issue, target));
      if (issues.length < 100) { complete = true; break; }
    }
  }
  const unique = [...new Map(matches.map(issue => [issue.number, issue])).values()];
  if (!complete || unique.length !== 1 || !unique[0].body?.includes(record.marker)) {
    record.status = unique.length > 1 ? 'multiple-matches' : 'ambiguous';
    await journal.save(record);
    return { status: record.status, operationId: record.operationId, retryAllowed: false, message: '尚不能唯一核实此前创建结果；一次查询为空不能证明未创建，禁止自动重发。' };
  }
  const actual = issueIdentity(await client.getIssue(target.repository, unique[0].number, { deadline }), target, unique[0].number);
  if (!actual.body?.includes(record.marker)) throw new IssueError('operation-marker-mismatch', '回读事项缺少本次唯一操作标识。');
  verifyRecordedContent(actual, record);
  record.number = actual.number; record.url = actual.html_url; record.status = 'verified'; record.verifiedAt = new Date().toISOString();
  await journal.save(record);
  return { status: 'verified', backend: 'github', operationId: record.operationId, number: actual.number, url: actual.html_url, recovered: true };
}

export async function recoverIssue({ resolution, operationId, client, validate = async () => {} }) {
  return withJournal(resolution, operationId, async journal => {
    const record = journal.previous;
    if (!record) throw new IssueError('operation-not-found', '该项目没有此 operation-id 的操作记录。');
    if (record.fingerprint !== resolution.fingerprint) throw new IssueError('operation-mismatch', '规则、模板、关系、目标或本次输入与原操作不同，不能沿用旧记录。');
    await validate();
    if (['local-issue', 'local-update', 'local-comment'].includes(record.kind)) {
      const target = localTarget(resolution);
      if (await realpath(target.root) !== record.target.root || target.directory !== record.target.directory) throw new IssueError('operation-target-changed', '当前解析目标与原本地操作不同；请显式解析原目标再恢复。');
      return recoverLocal(record, journal);
    }
    if (['github-issue', 'github-update', 'github-comment'].includes(record.kind)) {
      const target = githubTarget(resolution);
      if (target.host !== record.target.host || target.repository.toLowerCase() !== record.target.repository.toLowerCase()) throw new IssueError('operation-target-changed', '当前解析目标与原 GitHub 操作不同；不能转移恢复目标。');
      return record.kind === 'github-issue' ? recoverRemote(record, journal, client) : recoverRemoteEdit(record, journal, client);
    }
    throw new IssueError('unsupported-recovery', '该记录不是 Issue 创建记录。');
  });
}

function commentIdentity(comment, target, number, id) {
  if (!Number.isSafeInteger(comment?.id) || comment.id < 1 || (id !== undefined && comment.id !== id)
      || !sameGitHubUrl(comment.issue_url, `https://api.github.com/repos/${target.repository}/issues/${number}`)
      || !sameGitHubUrl(comment.html_url, `https://github.com/${target.repository}/issues/${number}#issuecomment-${comment.id}`)) {
    throw new IssueError('comment-mismatch', '回复回读不属于原 Issue、目标仓库或回复编号。');
  }
  return comment;
}

async function recoverRemoteEdit(record, journal, client) {
  const target = validateGitHubTarget(record.target), deadline = Date.now() + 120_000;
  let actual;
  if (record.kind === 'github-update') {
    actual = issueIdentity(await client.getIssue(target.repository, record.number, { deadline }), target, record.number);
  } else if (record.commentId) {
    actual = commentIdentity(await client.getComment(target.repository, record.commentId, { deadline }), target, record.number, record.commentId);
  } else {
    const matches = new Map();
    let complete = false;
    for (let page = 1; page <= 20 && Date.now() < deadline; page++) {
      const comments = await client.listComments(target.repository, record.number, { page, since: new Date(Date.parse(record.createdAt) - 300_000).toISOString(), deadline });
      if (!Array.isArray(comments)) throw new IssueError('comment-mismatch', 'GitHub 回复列表无效。');
      for (const comment of comments) if (comment.body?.includes(record.marker)) matches.set(comment.id, commentIdentity(comment, target, record.number));
      if (comments.length < 100) { complete = true; break; }
    }
    if (!complete || matches.size !== 1) {
      record.status = matches.size > 1 ? 'multiple-matches' : 'ambiguous';
      await journal.save(record);
      return { status: record.status, operationId: record.operationId, retryAllowed: false, message: '不能唯一核实原回复；不重复 POST。' };
    }
    const id = [...matches.keys()][0];
    actual = commentIdentity(await client.getComment(target.repository, id, { deadline }), target, record.number, id);
  }
  verifyRecordedContent(actual, record);
  if (record.kind === 'github-comment') {
    if (!actual.body?.includes(record.marker)) throw new IssueError('operation-marker-mismatch', '回复缺少原操作标识。');
    record.commentId = actual.id;
  }
  record.status = 'verified'; record.url = actual.html_url; record.verifiedAt = new Date().toISOString();
  await journal.save(record);
  return { status: 'verified', backend: 'github', operationId: record.operationId, number: record.number, url: record.url, recovered: true };
}

async function editRemote({ resolution, number, fields, operationId, client, authorized, validate }, kind) {
  authorization(authorized);
  const target = githubTarget(resolution), contentHash = hash(fields);
  const marker = kind === 'github-comment' ? `<!-- git-setup-operation:${operationId} sha256:${contentHash} -->` : undefined;
  const payload = marker ? { body: `${fields.body}\n\n${marker}` } : fields;
  const record = { version: 1, kind, operationId, number, target: { host: target.host, repository: target.repository }, contentHash: hash({ number, fields }), contentChecks: contentChecks(payload), marker, fingerprint: resolution.fingerprint, createdAt: new Date().toISOString(), status: 'prepared' };
  return withJournal(resolution, operationId, async journal => {
    if (journal.previous) { sameOperation(journal.previous, record); return recoverRemoteEdit(journal.previous, journal, client); }
    issueIdentity(await client.getIssue(target.repository, number), target, number);
    await journal.save(record);
    try {
      await validate();
      if (kind === 'github-update') issueIdentity(await client.updateIssue(target.repository, number, payload, { authorized }), target, number);
      else {
        const result = commentIdentity(await client.createComment(target.repository, number, payload.body, { authorized }), target, number);
        record.commentId = result.id;
      }
      record.status = 'written-unverified'; await journal.save(record);
      return await recoverRemoteEdit(record, journal, client);
    } catch (error) {
      record.status = error.status >= 400 && error.status < 500 && !error.ambiguous ? 'rejected' : 'ambiguous';
      record.errorCode = error.code || 'unknown'; await journal.save(record);
      error.operationId = operationId; throw error;
    }
  });
}

export async function updateRemoteIssue({ content, operationId = randomUUID(), validate = async () => {}, ...options }) {
  const fields = contentFields(content, true);
  if (!Object.keys(fields).length) throw new IssueError('empty-update', '没有要求更新的 Issue 字段。');
  return editRemote({ ...options, fields, operationId, validate }, 'github-update');
}

export async function commentRemoteIssue({ body, operationId = randomUUID(), validate = async () => {}, ...options }) {
  if (typeof body !== 'string' || !body.trim()) throw new IssueError('invalid-body', '回复正文必须是非空文本。');
  return editRemote({ ...options, fields: { body }, operationId, validate }, 'github-comment');
}

export async function createRemoteRepository({ projectRoot, owner, name, private: isPrivate, operationId = randomUUID(), client, authorized = false }) {
  authorization(authorized);
  const target = validateGitHubTarget({ host: client.host, repository: `${owner}/${name}` });
  if (typeof isPrivate !== 'boolean') throw new IssueError('visibility-required', '创建仓库必须明确 public 或 private。');
  const record = { version: 1, kind: 'github-repository', operationId, target: { host: target.host, repository: target.repository }, contentHash: hash({ private: isPrivate }), private: isPrivate, createdAt: new Date().toISOString(), status: 'prepared' };
  return withJournal({ targetProject: projectRoot }, operationId, async journal => {
    if (journal.previous) {
      sameOperation(journal.previous, record);
      // Existence alone cannot prove who created a repository after an ambiguous POST.
      const previous = journal.previous;
      const existing = await client.getRepository(target.repository);
      if (previous.repositoryId && existing.id === previous.repositoryId && existing.private === isPrivate) return { status: 'verified', operationId, url: existing.html_url, recovered: true };
      return { status: 'ambiguous', operationId, retryAllowed: false, observedUrl: existing.html_url, message: '目标仓库已存在，但无法证明对应本次创建；请核对，不自动重发。' };
    }
    await journal.save(record);
    try {
      const result = await client.createRepository({ owner, name, private: isPrivate, authorized });
      if (result.status === 'already-exists') {
        record.status = 'already-exists'; await journal.save(record);
        return { status: 'already-exists', operationId, url: result.repository.html_url, message: '仓库已经存在，没有创建或修改可见性。' };
      }
      record.repositoryId = result.repository.id; record.status = 'created-unverified'; await journal.save(record);
      const actual = await client.getRepository(target.repository);
      if (actual.full_name?.toLowerCase() !== target.repository.toLowerCase() || actual.id !== record.repositoryId || actual.private !== isPrivate) throw new IssueError('repository-mismatch', '创建后的仓库身份或可见性回读不一致。');
      record.status = 'verified'; await journal.save(record);
      return { status: 'verified', operationId, url: actual.html_url, repository: actual.full_name, private: actual.private };
    } catch (error) {
      record.status = error.status >= 400 && error.status < 500 && !error.ambiguous ? 'rejected' : 'ambiguous';
      record.errorCode = error.code || 'unknown'; await journal.save(record);
      error.operationId = operationId; throw error;
    }
  });
}
