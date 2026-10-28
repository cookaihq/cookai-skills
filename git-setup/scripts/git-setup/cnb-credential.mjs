import { createHash } from 'node:crypto';
import { authFail, authTarget } from './auth-roots.mjs';

export const cnbCredentialFormat = target => '单行 https://cnb:<访问令牌>@cnb.cool/' + target.repository;

/** One authenticated URL. Diagnostics never include its text. */
export function parseCnbCredential(raw, expected) {
  const value = raw.replace(/^\uFEFF/, '').replace(/\r?\n$/, '');
  let url, password, target;
  try {
    if (!value || /[\s\0"']/u.test(value)) throw new Error();
    const raw = /^https:\/\/cnb:([^@\s/?#]+)@cnb\.cool(\/[^?#]*)$/.exec(value);
    if (!raw) throw new Error();
    url = new URL(value);
    if (raw[2] !== url.pathname) throw new Error();
    password = decodeURIComponent(url.password);
    target = authTarget('https://' + url.host + url.pathname);
  } catch { authFail('auth_file_format', 'CNB 凭证须为单行完整 HTTPS 鉴权 URL，令牌中的 URL 特殊字符须编码。'); }
  if (url.protocol !== 'https:' || url.username !== 'cnb' || !password || /[\r\n\0]/.test(password)
      || url.search || url.hash || target?.key !== expected.key) {
    authFail('auth_file_target_mismatch', 'CNB 凭证须使用 cnb 用户名并匹配本次完整仓库目标。');
  }
  return { username: 'cnb', password, revision: createHash('sha256').update(JSON.stringify([target.key, 'cnb', password])).digest('hex') };
}
