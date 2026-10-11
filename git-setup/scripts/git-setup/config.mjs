import { promises as fs } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash, randomUUID } from 'node:crypto';

const exec = promisify(execFile);
const CONFIG = '.git-setup/project.json';
const ENV_KEYS = ['GIT_SETUP_ISSUE_JSON', 'GIT_SETUP_GITHUB_AUTH', 'GIT_SETUP_GITHUB_TOKEN', 'GIT_SETUP_GITHUB_HOST', 'GIT_SETUP_GITHUB_REPOSITORY'];
const AUTH_KEYS = ENV_KEYS.slice(1);
const hash = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const fail = (code, message) => { const error = new Error(message); error.code = code; throw error; };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const nonempty = value => typeof value === 'string' && value.trim() !== '';
const gitEnv = () => {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', LC_ALL: 'C' };
  for (const key of Object.keys(env)) if (/^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG|CEILING_DIRECTORIES)/.test(key)) delete env[key];
  return env;
};

async function git(cwd, args, optional = false) {
  try { return (await exec('git', ['-C', cwd, ...args], { env: gitEnv(), encoding: 'utf8', timeout: 15000, maxBuffer: 2 * 1024 * 1024 })).stdout.trim(); }
  catch (error) {
    if (optional && typeof error.code === 'number' && !error.killed) return null;
    fail('GIT_INSPECTION_FAILED', `Git inspection failed in ${cwd}; check Git availability, ownership and repository state.`);
  }
}

function keys(value, allowed, label) {
  if (!object(value)) fail('CONFIG_INVALID', `${label} must be an object.`);
  const unknown = Object.keys(value).filter(key => !allowed.includes(key));
  if (unknown.length) fail('CONFIG_INVALID', `${label} contains unsupported fields; compare it with the documented schema.`);
}

function relative(value, label, allowDot = false) {
  if (!nonempty(value) || /[\0\r\n\\]/.test(value) || path.posix.isAbsolute(value) || /^[A-Za-z]:/.test(value) || value.split('/').some(part => part === '..' || part === '' || (part === '.' && !allowDot))) {
    fail('CONFIG_INVALID', `${label} must be a portable relative path without parent traversal.`);
  }
  if (!allowDot && value === '.') fail('CONFIG_INVALID', `${label} must name a child path.`);
  return value;
}

function checkoutPath(value) {
  if (!nonempty(value) || /[\0\r\n\\]/.test(value) || path.posix.isAbsolute(value) || /^[A-Za-z]:/.test(value) || value.includes('://') || value.split('/').some(part => part === '')) fail('CONFIG_INVALID', 'issues.local.checkout must be an explicit portable relative checkout path.');
}

function identity(value, label) {
  keys(value, ['host', 'repository'], label);
  if (!nonempty(value.host) || !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(value.host) || value.host.includes('..')) fail('CONFIG_INVALID', `${label}.host must be a hostname, without a URL scheme or credentials.`);
  if (!nonempty(value.repository) || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(value.repository) || value.repository.split('/').some(part => part === '.' || part === '..')) fail('CONFIG_INVALID', `${label}.repository must be owner/name.`);
}

function noSecrets(value, label = 'config') {
  if (Array.isArray(value)) return value.forEach((item, index) => noSecrets(item, `${label}[${index}]`));
  if (object(value)) return Object.entries(value).forEach(([key, item]) => {
    if (/(?:token|password|secret|authorization|credential|api[_-]?key)/i.test(key)) fail('CONFIG_SECRET', `${label} cannot contain credential fields.`);
    noSecrets(key, label);
    noSecrets(item, `${label}.${key}`);
  });
  if (typeof value === 'string' && (/\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b/.test(value) || /-----BEGIN [^-]*PRIVATE KEY-----/.test(value) || /https?:\/\/[^/\s]*@/i.test(value) || /[?&](?:token|access_token|api_key|key|password)=/i.test(value))) fail('CONFIG_SECRET', `${label} contains a credential-like value; shared configuration must not contain secrets.`);
}

export function validateIssue(issue) {
  keys(issue, ['backend', 'target', 'github', 'local'], 'issues');
  if (!['local', 'github'].includes(issue.backend)) fail('ISSUE_BACKEND_UNSUPPORTED', 'Issue backend must be local or github; other online platforms are not supported.');
  if (!['current', 'outer', 'specified'].includes(issue.target)) fail('CONFIG_INVALID', 'issues.target must be current, outer or specified.');
  if (issue.backend === 'github') {
    if (issue.local !== undefined) fail('CONFIG_INVALID', 'GitHub issues cannot contain local settings.');
    if (issue.github !== undefined) identity(issue.github, 'issues.github');
    if (issue.target === 'specified' && !issue.github) fail('CONFIG_INCOMPLETE', 'Specified GitHub issue targets require issues.github.');
  } else {
    if (issue.github !== undefined) fail('CONFIG_INVALID', 'Local issues cannot contain GitHub settings.');
    keys(issue.local, ['directory', 'checkout', 'template', 'filename', 'numbering'], 'issues.local');
    relative(issue.local.directory, 'issues.local.directory', true);
    if (issue.local.checkout !== undefined) checkoutPath(issue.local.checkout);
    if (issue.target === 'specified' && !issue.local.checkout) fail('CONFIG_INCOMPLETE', 'Specified local issue targets require a relative checkout path.');
    if (issue.local.template !== undefined) relative(issue.local.template, 'issues.local.template');
    if (issue.local.filename !== undefined && (!nonempty(issue.local.filename) || /[\/\\\0\r\n]/.test(issue.local.filename) || !issue.local.filename.endsWith('.md') || /\{(?!number\}|slug\})/.test(issue.local.filename))) fail('CONFIG_INVALID', 'issues.local.filename must be a Markdown filename with only {number} and {slug} placeholders.');
    if (issue.local.numbering !== undefined) {
      keys(issue.local.numbering, ['start', 'width'], 'issues.local.numbering');
      for (const field of ['start', 'width']) if (issue.local.numbering[field] !== undefined && (!Number.isSafeInteger(issue.local.numbering[field]) || issue.local.numbering[field] < 1 || (field === 'width' && issue.local.numbering[field] > 12))) fail('CONFIG_INVALID', `issues.local.numbering.${field} must be a positive integer${field === 'width' ? ' at most 12' : ''}.`);
      if (!issue.local.filename?.includes('{number}')) fail('CONFIG_INVALID', 'Numbering requires a filename containing {number}.');
    }
  }
  noSecrets(issue, 'issues');
  return issue;
}

function validateConfig(config) {
  keys(config, ['schemaVersion', 'repository', 'issues', 'workspace', 'remotes', 'sync'], 'project');
  if (config.schemaVersion !== 1) fail('CONFIG_VERSION_UNSUPPORTED', 'project.schemaVersion must be 1.');
  noSecrets(config);
  if (config.repository !== undefined) identity(config.repository, 'repository');
  if (config.issues !== undefined) validateIssue(config.issues);
  if (config.remotes !== undefined) {
    if (!object(config.remotes)) fail('CONFIG_INVALID', 'remotes must map remote names to URLs.');
    for (const [name, url] of Object.entries(config.remotes)) if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name) || !nonempty(url) || /[\0\r\n]/.test(url)) fail('CONFIG_INVALID', 'remotes must contain valid names and single-line URLs.');
  }
  if (config.sync !== undefined) {
    keys(config.sync, ['source', 'targets', 'refs'], 'sync');
    if (!nonempty(config.sync.source) || !Array.isArray(config.sync.targets) || !config.sync.targets.length || config.sync.targets.some(target => !nonempty(target)) || !Array.isArray(config.sync.refs) || !config.sync.refs.length || config.sync.refs.some(ref => !/^refs\/(heads|tags)\/.+/.test(ref) || /[\s\0~^:?*\[\\]/.test(ref) || ref.includes('..') || ref.includes('@{') || ref.endsWith('/'))) fail('CONFIG_INVALID', 'sync requires a source, nonempty targets and explicit refs/heads/* or refs/tags/* refs.');
    if (new Set(config.sync.targets).size !== config.sync.targets.length || new Set(config.sync.refs).size !== config.sync.refs.length) fail('CONFIG_INVALID', 'sync cannot contain duplicate targets or refs.');
  }
  if (config.workspace !== undefined) {
    keys(config.workspace, ['repositories', 'issuePolicy'], 'workspace');
    if (!Array.isArray(config.workspace.repositories)) fail('CONFIG_INVALID', 'workspace.repositories must be an array.');
    const seen = new Set();
    for (const entry of config.workspace.repositories) {
      keys(entry, ['path', 'issues'], 'workspace repository');
      relative(entry.path, 'workspace repository path');
      if (seen.has(entry.path)) fail('CONFIG_INVALID', 'workspace repository paths must be unique.');
      seen.add(entry.path);
      if (!['inherit', 'independent'].includes(entry.issues)) fail('CONFIG_INVALID', 'workspace repository issues must be inherit or independent.');
    }
    if (config.workspace.issuePolicy !== undefined) {
      keys(config.workspace.issuePolicy, ['allowIndependent', 'backend'], 'workspace.issuePolicy');
      if (config.workspace.issuePolicy.allowIndependent !== undefined && typeof config.workspace.issuePolicy.allowIndependent !== 'boolean') fail('CONFIG_INVALID', 'allowIndependent must be boolean.');
      if (config.workspace.issuePolicy.backend !== undefined && !['local', 'github'].includes(config.workspace.issuePolicy.backend)) fail('CONFIG_INVALID', 'issuePolicy.backend must be local or github.');
    }
  }
  return config;
}

async function readOptional(file) {
  try { return await fs.readFile(file, 'utf8'); }
  catch (error) { if (error.code === 'ENOENT') return null; fail('CONFIG_UNREADABLE', `Cannot read configuration file ${file}.`); }
}

function parseJson(text, label) {
  try { return JSON.parse(text); }
  catch { fail('CONFIG_INVALID_JSON', `${label} is not valid JSON; its contents are not included in this error.`); }
}

async function canonical(directory) {
  try { const resolved = await fs.realpath(path.resolve(directory)); if (!(await fs.stat(resolved)).isDirectory()) throw new Error(); return resolved; }
  catch { fail('PROJECT_UNAVAILABLE', `Project directory is unavailable: ${path.resolve(directory)}.`); }
}

/** Read-only Git facts. Directory nesting alone never creates a workspace relationship. */
export async function inspectProject(project) {
  const requestedPath = await canonical(project);
  let root;
  try { root = (await exec('git', ['-C', requestedPath, 'rev-parse', '--show-toplevel'], { env: gitEnv(), encoding: 'utf8', timeout: 15000 })).stdout.trim(); }
  catch (error) {
    if (typeof error.code === 'number' && /not a git repository/i.test(error.stderr || '')) return { requestedPath, root: requestedPath, isGit: false, gitDir: null, commonDir: null, isWorktree: false, superprojectRoot: null, remotes: [] };
    fail('GIT_INSPECTION_FAILED', `Cannot inspect project ${requestedPath}; check Git installation, repository state and ownership.`);
  }
  root = await canonical(root);
  const gitDir = await fs.realpath(await git(root, ['rev-parse', '--absolute-git-dir']));
  const commonDir = await fs.realpath(path.resolve(root, await git(root, ['rev-parse', '--git-common-dir'])));
  const parent = await git(root, ['rev-parse', '--show-superproject-working-tree']);
  const superprojectRoot = parent ? await canonical(parent) : null;
  const names = (await git(root, ['remote'])).split('\n').filter(Boolean);
  const remotes = [];
  for (const name of names) {
    const fetchUrls = (await git(root, ['config', '--get-all', `remote.${name}.url`], true) || '').split('\n').filter(Boolean);
    const pushUrls = (await git(root, ['config', '--get-all', `remote.${name}.pushurl`], true) || '').split('\n').filter(Boolean);
    // Inspection output must not expose credentials embedded in legacy remote URLs.
    const redact = url => url.replace(/(https?:\/\/)[^/\s]*@/gi, '$1[redacted]@').replace(/([?&](?:token|access_token|api_key|key|password)=)[^&]*/gi, '$1[redacted]');
    remotes.push({ name, fetchUrls: fetchUrls.map(redact), pushUrls: (pushUrls.length ? pushUrls : fetchUrls).map(redact) });
  }
  return { requestedPath, root, isGit: true, gitDir, commonDir, isWorktree: gitDir !== commonDir, superprojectRoot, remotes };
}

/** Shared config only. Never reads parent configs or credential values. */
export async function loadProject(project, _options = {}) {
  const inspected = await inspectProject(project);
  const configPath = path.join(inspected.root, CONFIG);
  const raw = await readOptional(configPath);
  const config = raw === null ? null : validateConfig(parseJson(raw, configPath));
  return { project: inspected, config, configPath, exists: raw !== null, fingerprint: hash(raw ?? 'missing') };
}

/** Write a complete validated non-secret record; no Git index, commit or push mutation. */
export async function writeProject(project, input) {
  validateConfig(input);
  const inspected = await inspectProject(project);
  const configPath = path.join(inspected.root, CONFIG);
  const raw = await readOptional(configPath);
  const before = { configPath, fingerprint: hash(raw ?? 'missing') };
  const directory = path.dirname(before.configPath);
  await fs.mkdir(directory, { recursive: true });
  if (await fs.realpath(directory) !== directory) fail('CONFIG_SYMLINK', 'Shared config directory must not be a symlink.');
  try { if ((await fs.lstat(before.configPath)).isSymbolicLink()) fail('CONFIG_SYMLINK', 'Shared config file must not be a symlink.'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const temp = path.join(directory, `.project.${randomUUID()}.json`);
  const lockPath = path.join(directory, '.project-write.lock');
  let lock;
  try {
    try { lock = await fs.open(lockPath, 'wx', 0o600); }
    catch (error) { if (error.code === 'EEXIST') fail('CONFIG_WRITE_BUSY', 'Another configuration write is active, or an interrupted write left .project-write.lock; inspect that operation before retrying.'); throw error; }
    await fs.writeFile(temp, `${JSON.stringify(input, null, 2)}\n`, { flag: 'wx', mode: 0o644 });
    const current = await readOptional(before.configPath);
    if (hash(current ?? 'missing') !== before.fingerprint) fail('CONFIG_CHANGED', 'Shared config changed during this write; read it again before retrying.');
    await fs.rename(temp, before.configPath);
  } finally {
    await fs.rm(temp, { force: true });
    if (lock) { await lock.close(); await fs.rm(lockPath, { force: true }); }
  }
  return loadProject(project);
}

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (!match || !ENV_KEYS.includes(match[1])) continue;
    let value = match[2];
    if (value.length >= 2 && ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))) value = value.slice(1, -1);
    values[match[1]] = value;
  }
  return values;
}

/** Internal values may contain secrets; expose sources/presence only in user-facing output. */
export async function loadEnvironment(project, options = {}) {
  const root = await canonical(project);
  const layers = [{ path: 'process', values: options.env ?? process.env }];
  const files = ['.env.git-setup', '.env.local', '.env'].map(name => path.join(root, name));
  if (options.useGlobalConfig) files.push(options.globalConfigPath ?? path.join(os.homedir(), '.config/git-setup/.env'));
  for (const file of files) {
    const raw = await readOptional(file);
    if (raw !== null) layers.push({ path: file, values: parseEnv(raw) });
  }
  const values = {}, sources = {};
  for (const layer of layers) {
    if (values.GIT_SETUP_ISSUE_JSON === undefined && nonempty(layer.values.GIT_SETUP_ISSUE_JSON)) { values.GIT_SETUP_ISSUE_JSON = layer.values.GIT_SETUP_ISSUE_JSON; sources.GIT_SETUP_ISSUE_JSON = layer.path; }
  }
  const auth = options.issueOnly ? null : layers.find(layer => AUTH_KEYS.some(key => nonempty(layer.values[key])));
  if (auth) {
    for (const key of AUTH_KEYS) if (nonempty(auth.values[key])) { values[key] = auth.values[key]; sources[key] = auth.path; }
    const mode = values.GIT_SETUP_GITHUB_AUTH ?? (values.GIT_SETUP_GITHUB_TOKEN ? 'token' : null);
    if (!['token', 'gh'].includes(mode)) fail('CREDENTIAL_GROUP_INCOMPLETE', 'GitHub auth group must explicitly select gh or provide a token with its host and repository in the same source.');
    if (mode === 'token' && (!values.GIT_SETUP_GITHUB_TOKEN || !values.GIT_SETUP_GITHUB_HOST || !values.GIT_SETUP_GITHUB_REPOSITORY)) fail('CREDENTIAL_GROUP_INCOMPLETE', 'Token, GitHub host and repository must be nonempty in the same configuration source.');
    if (mode === 'gh' && values.GIT_SETUP_GITHUB_TOKEN) fail('CREDENTIAL_GROUP_CONFLICT', 'gh authentication and an explicit token cannot share one auth group.');
    values.GIT_SETUP_GITHUB_AUTH = mode;
    sources.GIT_SETUP_GITHUB_AUTH = auth.path;
    if (values.GIT_SETUP_GITHUB_HOST && !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(values.GIT_SETUP_GITHUB_HOST)) fail('CREDENTIAL_GROUP_INVALID', 'Configured GitHub host is not a hostname.');
    if (values.GIT_SETUP_GITHUB_REPOSITORY && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(values.GIT_SETUP_GITHUB_REPOSITORY)) fail('CREDENTIAL_GROUP_INVALID', 'Configured GitHub repository must be owner/name.');
  }
  return { values, sources };
}

async function registeredRelationship(initiating, workspace) {
  let registration = null;
  for (const entry of workspace.config?.workspace?.repositories ?? []) {
    const candidate = path.resolve(workspace.project.root, entry.path);
    let info;
    try { info = await inspectProject(candidate); } catch (error) { if (error.code === 'PROJECT_UNAVAILABLE') continue; throw error; }
    if (info.root !== candidate) continue;
    if (info.root === initiating.root || (info.isGit && initiating.isGit && info.commonDir === initiating.commonDir)) {
      if (registration) fail('WORKSPACE_AMBIGUOUS', 'Multiple workspace entries identify the initiating repository.');
      registration = { ...entry, evidence: 'project-config', workspace: workspace.project.root };
    }
  }
  if (registration) return registration;
  const modules = await readOptional(path.join(workspace.project.root, '.gitmodules'));
  if (modules !== null) {
    const output = await git(workspace.project.root, ['config', '--file', '.gitmodules', '--get-regexp', '^submodule\..*\.path$'], true);
    for (const line of (output || '').split('\n')) {
      const modulePath = line.replace(/^\S+\s+/, '');
      if (!modulePath) continue;
      relative(modulePath, '.gitmodules path');
      const candidate = path.resolve(workspace.project.root, modulePath);
      let info;
      try { info = await inspectProject(candidate); } catch (error) { if (error.code === 'PROJECT_UNAVAILABLE') continue; throw error; }
      if (info.root === candidate && (info.root === initiating.root || (info.isGit && initiating.isGit && info.commonDir === initiating.commonDir))) return { path: modulePath, issues: 'inherit', evidence: '.gitmodules', workspace: workspace.project.root };
    }
  }
  fail('WORKSPACE_UNREGISTERED', 'The explicit or detected workspace does not register this repository; directory nesting is insufficient.');
}

async function workspaceFor(initiating, options) {
  let workspacePath = options.workspace;
  if (!workspacePath && initiating.superprojectRoot) {
    const parent = await inspectProject(initiating.superprojectRoot);
    if (parent.superprojectRoot) fail('WORKSPACE_AMBIGUOUS', 'Multiple superprojects exist; explicitly select the registered workspace.');
    workspacePath = parent.root;
  }
  if (!workspacePath) return null;
  const workspace = await loadProject(workspacePath);
  if (workspace.project.root === initiating.root) return null;
  const relationship = await registeredRelationship(initiating, workspace);
  return { ...workspace, relationship };
}

function githubFromRemotes(project) {
  const candidates = new Map();
  for (const remote of project.remotes) for (const url of [...remote.fetchUrls, ...remote.pushUrls]) {
    const match = /^(?:https:\/\/github\.com\/|ssh:\/\/git@github\.com\/|git@github\.com:)([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?)(?:\.git)?\/?$/.exec(url);
    if (match) candidates.set(match[1].toLowerCase(), { host: 'github.com', repository: match[1] });
  }
  if (candidates.size !== 1) fail('GITHUB_TARGET_AMBIGUOUS', 'No unique GitHub repository is configured; explicitly set the target host and owner/name.');
  return [...candidates.values()][0];
}

async function assertContained(base, relativePath, label, mustExist = false) {
  const absolute = path.resolve(base, relativePath);
  let existing = absolute;
  while (true) {
    try {
      const resolved = await fs.realpath(existing);
      const rest = path.relative(existing, absolute);
      const final = path.resolve(resolved, rest);
      if (final !== base && !final.startsWith(`${base}${path.sep}`)) fail('PATH_OUTSIDE_PROJECT', `${label} resolves outside its approved project directory.`);
      if (mustExist && existing !== absolute) fail('PROJECT_UNAVAILABLE', `${label} does not exist.`);
      return final;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const parent = path.dirname(existing);
      if (parent === existing) throw error;
      existing = parent;
    }
  }
}

/** Resolve first, then use this exact result; call again before a write to compare fingerprints. */
export async function resolveIssue(project, options = {}) {
  const initiating = await inspectProject(project);
  const workspace = await workspaceFor(initiating, options);
  const independent = workspace?.relationship.issues === 'independent';
  const policy = workspace?.config?.workspace?.issuePolicy;
  if (independent && policy?.allowIndependent === false) fail('ISSUE_POLICY_CONFLICT', 'Project rules prohibit independent child Issue configuration.');
  const owner = workspace && !independent ? workspace : await loadProject(initiating.root);
  if (independent && (!owner.exists || !owner.config?.issues)) fail('INDEPENDENT_CONFIG_MISSING', 'Registered independent child Issue configuration is missing; it cannot fall back to the workspace.');
  const environment = await loadEnvironment(owner.project.root, { ...options, issueOnly: true });
  let issue = owner.config?.issues;
  let source = { kind: 'project', path: owner.configPath };
  if (environment.values.GIT_SETUP_ISSUE_JSON !== undefined) {
    issue = parseJson(environment.values.GIT_SETUP_ISSUE_JSON, 'GIT_SETUP_ISSUE_JSON');
    source = { kind: 'environment', path: environment.sources.GIT_SETUP_ISSUE_JSON };
  }
  if (!issue && options.projectRuleIssue) { issue = options.projectRuleIssue; source = { kind: 'project-rule', path: null }; }
  if (options.issueOverride) { issue = options.issueOverride; source = { kind: 'request', path: null }; }
  if (!issue) fail('ISSUE_CONFIG_REQUIRED', 'No Issue configuration or explicit project rule is available; choose local or GitHub and a target before creating.');
  validateIssue(issue);
  for (const restriction of [policy, owner.config?.workspace?.issuePolicy]) {
    if (restriction?.backend && issue.backend !== restriction.backend) fail('ISSUE_POLICY_CONFLICT', 'The selected Issue backend conflicts with the project restriction.');
  }
  let target = initiating;
  if (issue.target === 'outer') {
    if (!workspace) fail('OUTER_REPOSITORY_MISSING', 'Issue target is outer, but no verified workspace relationship exists.');
    target = workspace.project;
  }
  let local = null, github = null;
  if (issue.backend === 'local') {
    if (issue.local.checkout) {
      const checkout = await canonical(path.resolve(owner.project.root, issue.local.checkout));
      const candidate = await inspectProject(checkout);
      if (candidate.root !== checkout || !candidate.isGit) fail('LOCAL_CHECKOUT_INVALID', 'An explicit local checkout must identify an existing Git repository root.');
      if (issue.target !== 'specified' && candidate.root !== target.root && (!candidate.isGit || !target.isGit || candidate.commonDir !== target.commonDir)) fail('LOCAL_CHECKOUT_MISMATCH', 'The configured checkout is not the selected target repository.');
      target = candidate;
    }
    await assertContained(target.root, issue.local.directory, 'Local Issue directory');
    local = { ...issue.local, root: target.root };
    if (issue.local.template) {
      local.template = await assertContained(owner.project.root, issue.local.template, 'Issue template', true);
      const template = await readOptional(local.template);
      if (template === null) fail('TEMPLATE_MISSING', 'The configured Issue template is missing.');
      local.templateFingerprint = hash(template);
    }
  } else {
    if (issue.github) github = { ...issue.github };
    else {
      const targetConfig = target.root === owner.project.root ? owner : await loadProject(target.root);
      github = targetConfig.config?.repository ?? githubFromRemotes(target);
    }
    identity(github, 'resolved GitHub target');
    if (issue.target === 'specified') target = owner.project;
  }
  const relationship = workspace?.relationship ?? null;
  const result = { backend: issue.backend, initiatingProject: initiating.root, configOwner: owner.project.root, targetProject: target.root, targetRepository: github, github, local, issue: structuredClone(issue), source, relationship };
  result.fingerprint = hash({ result, owner: owner.fingerprint, workspace: workspace?.fingerprint ?? null, initiating: { commonDir: initiating.commonDir, remotes: initiating.remotes }, target: { commonDir: target.commonDir, remotes: target.remotes } });
  return result;
}
