import { createHash } from 'node:crypto';
import { resolve, relative, isAbsolute, sep } from 'node:path';

const credentialFail = (code, message) => { throw Object.assign(new Error(message), { code }); };

// Shared by Agent/API authentication and the standalone project helper.
export function authTarget(url) {
  if (/^(?:[^@\s]+@)?cnb\.cool:/i.test(url) || /^ssh:\/\/(?:[^@/]+@)?cnb\.cool(?:[:/]|$)/i.test(url)) credentialFail('cnb_https_required', 'CNB 不支持 SSH；请使用不含凭证的 HTTPS 仓库地址。');
  let parsed;
  try { parsed = new URL(url); } catch { return null; }
  if (!['github.com', 'cnb.cool'].includes(parsed.hostname.toLowerCase())) return null;
  if (parsed.protocol === 'ssh:' && parsed.hostname === 'github.com') return null;
  if (parsed.protocol !== 'https:' || parsed.port || parsed.username || parsed.password || parsed.search || parsed.hash) credentialFail('invalid_auth_target', 'GitHub/CNB HTTPS 仓库地址不能含凭证、端口或查询参数。');
  const repository = parsed.pathname.replace(/^\//, '').replace(/\/$/, '').replace(/\.git$/, '');
  const parts = repository.split('/');
  if (parts.length < 2 || (parsed.hostname === 'github.com' && parts.length !== 2) || parts.some(part => !/^[A-Za-z0-9_.-]+$/.test(part) || part === '.' || part === '..')) credentialFail('invalid_auth_target', '凭证目标必须是完整组织及仓库路径。');
  const host = parsed.hostname;
  return { host, repository, key: `${host}/${host === 'github.com' ? repository.toLowerCase() : repository}`, url: `https://${host}/${repository}`, filename: `${parts.map(part => part.replaceAll('-', '%2D')).join('--')}.${host === 'github.com' ? 'github-token' : 'cnb-url'}` };
}

export const cnbCredentialFormat = target => '单行 https://cnb:<访问令牌>@cnb.cool/' + target.repository;

export function parseCnbCredential(raw, expected) {
  const value = raw.replace(/^\uFEFF/, '').replace(/\r?\n$/, '');
  let url, password, target;
  try {
    if (!value || /[\s\0"']/u.test(value)) throw new Error();
    const matched = /^https:\/\/cnb:([^@\s/?#]+)@cnb\.cool(\/[^?#]*)$/.exec(value);
    if (!matched) throw new Error();
    url = new URL(value);
    if (matched[2] !== url.pathname) throw new Error();
    password = decodeURIComponent(url.password);
    target = authTarget('https://' + url.host + url.pathname);
  } catch { credentialFail('auth_file_format', 'CNB 凭证须为单行完整 HTTPS 鉴权 URL，令牌中的 URL 特殊字符须编码。'); }
  if (url.protocol !== 'https:' || url.username !== 'cnb' || !password || /[\r\n\0]/.test(password)
      || url.search || url.hash || target?.key !== expected.key) credentialFail('auth_file_target_mismatch', 'CNB 凭证须使用 cnb 用户名并匹配本次完整仓库目标。');
  return { username: 'cnb', password, revision: createHash('sha256').update(JSON.stringify([target.key, 'cnb', password])).digest('hex') };
}

export function parseCredentialFile(raw, method, target) {
  if (method === 'cnb-url') return parseCnbCredential(raw, target);
  if (method !== 'github-token') credentialFail('auth_method_invalid', '未知文件鉴权方式。');
  const value = raw.replace(/^\uFEFF/, '').replace(/\r?\n$/, '');
  if (!value || /[\s\0"'=:]/u.test(value)) credentialFail('auth_file_format', 'GitHub 凭证应为单行 Token 原文，不加引号、变量名或空格。');
  return { username: 'x-access-token', password: value, revision: createHash('sha256').update(raw).digest('hex') };
}

export function validateCredentialSource(entry, owner) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry) || Object.keys(entry).some(key => !['url', 'method', 'file', 'account'].includes(key))) credentialFail('input_config_invalid', '本机凭证来源的字段不符合标准；不输出原内容。');
  const target = authTarget(entry.url);
  if (!target || !['gh', 'github-token', 'cnb-url', 'environment'].includes(entry.method)
      || (target.host === 'cnb.cool' ? entry.method !== 'cnb-url' : entry.method === 'cnb-url')) credentialFail('auth_source_invalid', '来源的目标与鉴权方式不匹配。');
  if (entry.method === 'gh') {
    if (!/^[A-Za-z0-9-]+$/.test(entry.account || '') || entry.file !== undefined) credentialFail('auth_source_invalid', 'gh 来源必须明确账号且不能包含文件。');
  } else if (['github-token', 'cnb-url'].includes(entry.method)) {
    if (typeof entry.file !== 'string' || /[\r\n\0]/.test(entry.file) || entry.account !== undefined) credentialFail('auth_source_invalid', '文件来源须指定文件路径。');
    const rel = relative(owner.main, resolve(owner.main, entry.file));
    if (!rel || isAbsolute(rel) || rel === '..' || rel.startsWith('..' + sep)) credentialFail('auth_source_invalid', '来源文件须位于已确认凭证主检出。');
  } else if (entry.file !== undefined || entry.account !== undefined) credentialFail('auth_source_invalid', '环境来源不得混入其他文件或账号。');
  return { ...entry, url: target.url };
}

export function parseCredentialSources(text, owner) {
  const blocks = [...text.matchAll(/^```json\s*\r?\n([\s\S]*?)^```\s*$/gm)];
  let entries;
  try { if (blocks.length !== 1) throw new Error(); entries = JSON.parse(blocks[0][1]); }
  catch { credentialFail('auth_source_invalid', '本机来源记录须包含唯一的 JSON 来源数组；不输出原文。'); }
  if (!Array.isArray(entries) || entries.length > 200) credentialFail('auth_source_invalid', '本机来源记录必须是来源数组。');
  const result = entries.map(entry => validateCredentialSource(entry, owner));
  if (new Set(result.map(entry => authTarget(entry.url).key)).size !== result.length) credentialFail('auth_source_duplicate', '同一完整目标有重复来源，请明确保留哪个选择。');
  return result;
}

export function parseCredentialRequest(text) {
  const request = {};
  for (const line of text.split(/\r?\n/)) {
    if (!line) break;
    const index = line.indexOf('=');
    if (index < 1) credentialFail('auth_request_invalid', 'Git 凭证请求格式无效。');
    const key = line.slice(0, index);
    if (!['protocol', 'host', 'path', 'username'].includes(key)) continue;
    if (Object.hasOwn(request, key)) credentialFail('auth_request_invalid', 'Git 凭证请求包含重复身份字段。');
    request[key] = line.slice(index + 1);
  }
  if (request.protocol !== 'https' || !request.host || !request.path || /[\r\n\0]/.test(Object.values(request).join(''))) credentialFail('auth_request_invalid', 'Git 凭证请求缺少完整 HTTPS 目标。');
  const target = authTarget(`https://${request.host}/${request.path}`);
  if (!target) credentialFail('auth_request_invalid', 'Git 凭证请求目标不受支持。');
  return { ...request, target };
}
