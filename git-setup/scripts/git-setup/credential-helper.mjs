#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
const pkg = JSON.parse(await readFile(new URL('../../package.json', import.meta.url), 'utf8'));
try {
  if (Number(process.versions.node.split('.')[0]) < Number(pkg.engines.node.replace('>=', ''))) throw new Error('Node 版本不符合 Skill 要求。');
  const [flag, operation, last] = process.argv.slice(2);
  if (flag !== '--transport' || last !== undefined || !['get', 'store', 'erase'].includes(operation)) throw new Error('Git helper 只接受本次传输调用。');
  if (operation === 'get') {
    let input = '';
    for await (const chunk of process.stdin) { input += chunk; if (input.length > 32768) throw new Error('Git 凭证请求过大。'); }
    const request = {};
    for (const line of input.split('\n')) {
      if (!line) break;
      const index = line.indexOf('=');
      if (index < 1) throw new Error('Git 凭证请求格式无效。');
      const key = line.slice(0, index), value = line.slice(index + 1);
      if (key.endsWith('[]')) continue;
      if (Object.hasOwn(request, key)) throw new Error('Git 凭证请求包含重复字段。');
      request[key] = value;
    }
    let credential;
    {
      const { authTarget } = await import('./auth-roots.mjs');
      const expected = authTarget(process.env.GIT_SETUP_TRANSPORT_TARGET || '');
      const actual = request.protocol === 'https' && request.path && authTarget(`https://${request.host}/${request.path}`);
      if (!expected || !actual || actual.key !== expected.key) throw new Error('Git 请求目标与本次已选凭证不一致。');
      credential = { username: process.env.GIT_SETUP_TRANSPORT_USERNAME, password: process.env.GIT_SETUP_TRANSPORT_TOKEN };
      if (!credential.username || !credential.password || /[\r\n\0]/.test(credential.password) || (request.username && request.username !== credential.username)) throw new Error('本次 Git 凭证或用户名无效。');
    }
    process.stdout.write(`username=${credential.username}\npassword=${credential.password}\n\n`);
  }
} catch (error) {
  process.stderr.write(`git-setup credential helper: ${error.code || 'unavailable'}；${/^(auth_|gh_)/.test(error.code || '') ? error.message : '检查已选来源，不回退其他账号。'}\n`);
  process.stdout.write('quit=true\n\n');
  process.exitCode = 1;
}
