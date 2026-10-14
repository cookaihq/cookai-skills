import { join } from 'node:path';
import { authFail } from './repository-facts.mjs';
import { resolveRepository } from './repository-relationship.mjs';
export { authFail, mainCheckout, localEnvironment, localGit } from './repository-facts.mjs';

export async function credentialOwner(project, options = {}) {
  const relation = await resolveRepository(project, options);
  return { repository: relation.project, owner: { main: relation.management.main, commonDir: relation.management.commonDir }, directory: join(relation.management.main, '.credentials', 'repositories'), relationship: relation };
}

export function authTarget(url) {
  // CNB does not support SSH, including scp-style URLs.
  if (/^(?:[^@\s]+@)?cnb\.cool:/i.test(url) || /^ssh:\/\/(?:[^@/]+@)?cnb\.cool(?:[:/]|$)/i.test(url)) authFail('cnb_https_required', 'CNB 不支持 SSH；请使用不含凭证的 HTTPS 仓库地址。');
  let parsed;
  try { parsed = new URL(url); } catch { return null; }
  if (!['github.com', 'cnb.cool'].includes(parsed.hostname.toLowerCase())) return null;
  if (parsed.protocol === 'ssh:' && parsed.hostname === 'github.com') return null;
  if (parsed.protocol !== 'https:' || parsed.port || parsed.username || parsed.password || parsed.search || parsed.hash) authFail('invalid_auth_target', 'GitHub/CNB HTTPS 仓库地址不能含凭证、端口或查询参数。');
  const repository = parsed.pathname.replace(/^\//, '').replace(/\/$/, '').replace(/\.git$/, '');
  const parts = repository.split('/');
  if (parts.length < 2 || (parsed.hostname === 'github.com' && parts.length !== 2) || parts.some(part => !/^[A-Za-z0-9_.-]+$/.test(part) || part === '.' || part === '..')) authFail('invalid_auth_target', '凭证目标必须是完整组织及仓库路径。');
  const host = parsed.hostname;
  return { host, repository, key: `${host}/${host === 'github.com' ? repository.toLowerCase() : repository}`, url: `https://${host}/${repository}`, filename: `${parts.join('--')}.${host === 'github.com' ? 'github-token' : 'cnb-url'}` };
}
