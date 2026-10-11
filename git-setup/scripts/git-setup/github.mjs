import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

export const API_VERSION = '2022-11-28';
export const REQUEST_TIMEOUT_MS = 30_000;
const MAX_ATTEMPTS = 3;
const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

export class GitHubError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'GitHubError';
    this.code = code;
    Object.assign(this, details);
  }
}

export function validateGitHubTarget(target) {
  if (target?.host !== 'github.com') {
    throw new GitHubError('unsupported-host', '首版在线 Issue / 仓库 API 仅支持 github.com；不会向其他主机发送凭证。');
  }
  if (typeof target.repository !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9_.-]+$/.test(target.repository)) {
    throw new GitHubError('invalid-repository', 'GitHub 目标须为完整 owner/repository，不能包含 URL、凭证、查询参数或路径片段。');
  }
  const [owner, repo] = target.repository.split('/');
  if (repo === '.' || repo === '..' || repo.endsWith('.git')) {
    throw new GitHubError('invalid-repository', 'GitHub repository 应使用仓库名称，不带 .git 后缀。');
  }
  return { host: 'github.com', repository: `${owner}/${repo}`, owner, repo };
}

export function authFromEnvironment(values, target) {
  const selected = validateGitHubTarget(target);
  const method = values.GIT_SETUP_GITHUB_AUTH || (values.GIT_SETUP_GITHUB_TOKEN ? 'token' : undefined);
  if (!['token', 'gh'].includes(method)) {
    throw new GitHubError('missing-auth', '请配置 GIT_SETUP_GITHUB_AUTH=token 或 gh；Token 模式还需要同层 TOKEN、HOST 和 REPOSITORY 字段。');
  }
  if (method === 'gh' && values.GIT_SETUP_GITHUB_TOKEN) throw new GitHubError('conflicting-auth', 'gh 模式不能同时携带 Skill Token。');
  const auth = {
    method,
    host: values.GIT_SETUP_GITHUB_HOST,
    repository: values.GIT_SETUP_GITHUB_REPOSITORY,
    ...(method === 'token' ? { token: values.GIT_SETUP_GITHUB_TOKEN } : {}),
  };
  validateAuth(auth, selected);
  return auth;
}

function validateAuth(auth, target) {
  if (!auth || !['token', 'gh'].includes(auth.method)) throw new GitHubError('missing-auth', '未选择 GitHub Token 或 gh 鉴权。');
  if (auth.method === 'token' && (typeof auth.token !== 'string' || !auth.token || !auth.host || !auth.repository)) {
    throw new GitHubError('incomplete-auth', 'Token、HOST、REPOSITORY 必须在同一配置来源中完整提供。');
  }
  if (auth.method === 'token' && /[\r\n]/.test(auth.token)) throw new GitHubError('invalid-token', 'Token 格式无效；不能包含换行。');
  if (auth.method === 'gh' && auth.token) throw new GitHubError('conflicting-auth', 'gh 模式不能同时携带 Skill Token。');
  if (auth.host && auth.host !== target.host) throw new GitHubError('auth-target-mismatch', '凭证绑定主机与本次 GitHub 目标不一致。');
  if (auth.repository && auth.repository.toLowerCase() !== target.repository.toLowerCase()) {
    throw new GitHubError('auth-target-mismatch', '凭证绑定仓库与本次 GitHub 目标不一致。');
  }
}

function requireAuthorization(authorized) {
  if (authorized !== true) throw new GitHubError('authorization-required', '本次 GitHub 写入需要明确的操作授权。');
}

function numberPath(number) {
  if (!Number.isSafeInteger(number) || number < 1) throw new GitHubError('invalid-number', 'Issue 编号必须为正整数。');
  return number;
}

function retryDelay(headers, attempt, now) {
  const value = headers['retry-after'];
  if (value !== undefined) {
    const seconds = Number(value);
    const parsed = Number.isFinite(seconds) ? seconds : (Date.parse(value) - now()) / 1000;
    if (Number.isFinite(parsed)) return Math.min(60, Math.max(0, parsed)) * 1000;
  }
  if (headers['x-ratelimit-remaining'] === '0' && headers['x-ratelimit-reset']) {
    const reset = Number(headers['x-ratelimit-reset']);
    if (Number.isFinite(reset)) return Math.min(60_000, Math.max(0, reset * 1000 - now()));
  }
  return 1000 * 2 ** attempt;
}

function normalizeHeaders(headers = {}) {
  return Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), String(value)]));
}

async function fetchTransport({ url, method, headers, body, timeoutMs }) {
  const response = await fetch(url, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    redirect: 'manual', signal: AbortSignal.timeout(timeoutMs),
  });
  let size = 0;
  const chunks = [];
  for await (const chunk of response.body || []) {
    size += chunk.length;
    if (size > MAX_RESPONSE_BYTES) throw new GitHubError('response-too-large', 'GitHub 响应超过大小上限。');
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString('utf8');
  let data;
  try { data = raw ? JSON.parse(raw) : null; } catch { throw new GitHubError('invalid-response', 'GitHub 未返回有效 JSON。'); }
  return { status: response.status, headers: Object.fromEntries(response.headers), data };
}

function runGh(args, { input, timeoutMs, spawnImpl = spawn } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawnImpl('gh', args, {
      stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
      env: { ...process.env, GH_HOST: 'github.com', GH_PROMPT_DISABLED: '1', GH_PAGER: 'cat', GH_TOKEN: '', GITHUB_TOKEN: '' },
    });
    const output = [];
    let size = 0;
    let stderr = '';
    let exceeded = false;
    const timer = setTimeout(() => { exceeded = true; child.kill(); }, timeoutMs);
    child.stdout.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_RESPONSE_BYTES) { exceeded = true; child.kill(); } else output.push(chunk);
    });
    child.stderr.on('data', chunk => { if (stderr.length < 8192) stderr += chunk.toString(); });
    child.on('error', error => { clearTimeout(timer); reject(new GitHubError(error.code === 'ENOENT' ? 'missing-gh' : 'gh-failed', error.code === 'ENOENT' ? 'gh 未安装；请安装 GitHub CLI 后重试。' : '无法启动 gh。')); });
    child.on('close', code => {
      clearTimeout(timer);
      if (exceeded) return reject(new GitHubError('timeout', 'gh 调用超时或响应超过大小上限。'));
      resolve({ code, stdout: Buffer.concat(output).toString('utf8'), stderr });
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

function ghTransportFactory(spawnImpl) {
  let checked = false;
  return async ({ path, method, body, timeoutMs }) => {
    const startedAt = Date.now();
    if (!checked) {
      const probe = await runGh(['--version'], { timeoutMs, spawnImpl });
      if (probe.code !== 0) throw new GitHubError('invalid-gh', 'gh --version 执行失败。');
      checked = true;
    }
    const args = ['api', '--hostname', 'github.com', '--method', method, '--include', '-H', `X-GitHub-Api-Version: ${API_VERSION}`, '-H', 'Accept: application/vnd.github+json', path];
    if (body !== undefined) args.push('--input', '-');
    const remaining = timeoutMs - (Date.now() - startedAt);
    if (remaining <= 0) throw new GitHubError('timeout', 'gh 预检已用完本次调用的时间预算。');
    const result = await runGh(args, { input: body === undefined ? undefined : JSON.stringify(body), timeoutMs: remaining, spawnImpl });
    const match = result.stdout.match(/^HTTP\/[^ ]+ (\d{3})[^\r\n]*\r?\n([\s\S]*?)\r?\n\r?\n([\s\S]*)$/);
    if (!match) {
      const transient = /timeout|timed out|connection reset|broken pipe|no such host|tls handshake|temporary failure/i.test(result.stderr);
      throw new GitHubError(transient ? 'network' : 'gh-failed', transient ? 'gh 网络调用失败。' : 'gh API 未返回可验证的 HTTP 响应；请核对 github.com 登录状态和权限。');
    }
    const headers = {};
    for (const line of match[2].split(/\r?\n/)) {
      const index = line.indexOf(':');
      if (index > 0) headers[line.slice(0, index).toLowerCase()] = line.slice(index + 1).trim();
    }
    let data;
    try { data = match[3].trim() ? JSON.parse(match[3]) : null; } catch { throw new GitHubError('invalid-response', 'gh 返回了无效 JSON。'); }
    return { status: Number(match[1]), headers, data };
  };
}

/** Only fixed github.com endpoints are reachable; raw URLs are never accepted. */
export function createGitHubClient({ auth, host = 'github.com', transport, sleep = delay, now = Date.now, onRetry = event => process.stderr.write(`${JSON.stringify(event)}\n`), spawnImpl, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  if (host !== 'github.com') throw new GitHubError('unsupported-host', '首版 API 仅支持 github.com。');
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 120_000) throw new GitHubError('invalid-timeout', 'API 超时必须为 1–120000 毫秒。');
  const send = transport || (auth?.method === 'gh' ? ghTransportFactory(spawnImpl) : fetchTransport);
  async function request(method, path, target, body, authorized, deadline) {
    const selected = validateGitHubTarget({ host, repository: target });
    validateAuth(auth, selected);
    const writing = method !== 'GET';
    if (writing) requireAuthorization(authorized);
    const headers = { Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': API_VERSION, 'User-Agent': 'git-setup-skill', 'Content-Type': 'application/json' };
    if (auth.method === 'token') headers.Authorization = `Bearer ${auth.token}`;
    for (let attempt = 0; attempt < (writing ? 1 : MAX_ATTEMPTS); attempt++) {
      const remaining = deadline === undefined ? timeoutMs : Math.min(timeoutMs, deadline - now());
      if (remaining <= 0) throw new GitHubError('budget-exhausted', 'GitHub 查询已达到总时间预算。');
      let response;
      try {
        response = await send({ url: `https://api.github.com${path}`, path, method, headers, body, timeoutMs: remaining });
      } catch (error) {
        if (writing) throw new GitHubError('ambiguous', 'GitHub 写入结果不明；先查询恢复记录，禁止自动重复提交。', { ambiguous: true });
        const transient = !(error instanceof GitHubError) || ['network', 'timeout'].includes(error.code);
        if (transient && attempt + 1 < MAX_ATTEMPTS) {
          const waitMs = 1000 * 2 ** attempt;
          if (deadline !== undefined && now() + waitMs >= deadline) throw new GitHubError('budget-exhausted', 'GitHub 查询已达到总时间预算。');
          onRetry({ operation: 'github-read', attempt: attempt + 2, maxAttempts: MAX_ATTEMPTS, waitMs, reason: 'network' });
          await sleep(waitMs); continue;
        }
        if (error instanceof GitHubError) throw error;
        throw new GitHubError('network', 'GitHub 查询在限定尝试次数内未完成；错误详情已隐藏以保护凭证。');
      }
      response.headers = normalizeHeaders(response.headers);
      if (response.status >= 200 && response.status < 300) return response.data;
      if (response.status >= 300 && response.status < 400) throw new GitHubError('redirect-refused', 'GitHub 返回重定向；请核对仓库是否改名或迁移，更新目标后重试。', { status: response.status });
      const rateLimited = response.status === 429 || (response.status === 403 && (response.headers['retry-after'] !== undefined || response.headers['x-ratelimit-remaining'] === '0'));
      const transient = rateLimited || response.status >= 500;
      if (writing && transient) throw new GitHubError('ambiguous', `GitHub 写入返回 HTTP ${response.status}；结果需要恢复查询，禁止盲重试。`, { status: response.status, ambiguous: true });
      if (transient && attempt + 1 < MAX_ATTEMPTS) {
        const waitMs = rateLimited ? retryDelay(response.headers, attempt, now) : 1000 * 2 ** attempt;
        if (deadline !== undefined && now() + waitMs >= deadline) throw new GitHubError('budget-exhausted', 'GitHub 查询已达到总时间预算。');
        onRetry({ operation: 'github-read', attempt: attempt + 2, maxAttempts: MAX_ATTEMPTS, waitMs, reason: `HTTP ${response.status}` });
        await sleep(waitMs); continue;
      }
      const hint = response.status === 404 ? '目标不存在或当前身份无权访问，不能据此认定仓库不存在。' : response.status === 401 || response.status === 403 ? '请核对目标对应的身份、权限与组织限制；不自动更换凭证或目标。' : '请检查输入、平台限制和请求权限。';
      throw new GitHubError('http-error', `GitHub HTTP ${response.status}。${hint}`, { status: response.status });
    }
  }
  const repoPath = repository => {
    validateGitHubTarget({ host, repository });
    return `/repos/${repository.split('/').map(encodeURIComponent).join('/')}`;
  };
  return {
    host,
    getRepository: repository => request('GET', repoPath(repository), repository),
    getIssue: (repository, number, { deadline } = {}) => request('GET', `${repoPath(repository)}/issues/${numberPath(number)}`, repository, undefined, undefined, deadline),
    listIssues: (repository, { page = 1, since, deadline } = {}) => {
      if (!Number.isSafeInteger(page) || page < 1) throw new GitHubError('invalid-page', '页码必须为正整数。');
      const query = new URLSearchParams({ state: 'all', per_page: '100', page: String(page), sort: 'created', direction: 'desc' });
      if (since) query.set('since', since);
      return request('GET', `${repoPath(repository)}/issues?${query}`, repository, undefined, undefined, deadline);
    },
    createIssue: (repository, content, { authorized } = {}) => request('POST', `${repoPath(repository)}/issues`, repository, content, authorized),
    updateIssue: (repository, number, content, { authorized } = {}) => request('PATCH', `${repoPath(repository)}/issues/${numberPath(number)}`, repository, content, authorized),
    createComment: (repository, number, body, { authorized } = {}) => request('POST', `${repoPath(repository)}/issues/${numberPath(number)}/comments`, repository, { body }, authorized),
    getComment: (repository, id) => request('GET', `${repoPath(repository)}/issues/comments/${numberPath(id)}`, repository),
    async createRepository({ owner, name, private: isPrivate, authorized }) {
      requireAuthorization(authorized);
      const repository = `${owner}/${name}`;
      validateGitHubTarget({ host, repository });
      if (typeof isPrivate !== 'boolean') throw new GitHubError('visibility-required', '必须明确指定仓库 public 或 private。');
      try {
        const existing = await request('GET', repoPath(repository), repository);
        return { status: 'already-exists', repository: existing };
      } catch (error) { if (error.status !== 404) throw error; }
      // 404 can mean hidden. The explicit create operation never updates an existing repository.
      const account = await request('GET', `/users/${encodeURIComponent(owner)}`, repository);
      let endpoint;
      if (account?.type === 'Organization') endpoint = `/orgs/${encodeURIComponent(owner)}/repos`;
      else if (account?.type === 'User') {
        const current = await request('GET', '/user', repository);
        if (current?.login?.toLowerCase() !== owner.toLowerCase()) throw new GitHubError('owner-mismatch', '当前 GitHub 身份不能为另一个个人账号创建仓库。');
        endpoint = '/user/repos';
      } else throw new GitHubError('invalid-owner', '无法核实仓库所有者是个人账号还是组织。');
      const created = await request('POST', endpoint, repository, { name, private: isPrivate, auto_init: false }, authorized);
      return { status: 'created', repository: created };
    },
  };
}
