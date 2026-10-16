import { resolve } from 'node:path';
import { authSelection, publicSelection, prepareAuth, verifyAuth } from './auth.mjs';
import { inspectProject, loadEnvironment } from './config.mjs';

const fail = message => { throw Object.assign(new Error(message), { code: 'invalid_argument' }); };
const flags = new Set(['apply', 'allow-ignore', 'replace-auth', 'credentials-filled', 'api-only', 'use-global-config', 'github-auth']);
const values = new Set(['url', 'auth-method', 'gh-user', 'credential-file', 'expected-fingerprint']);

export function parseAuthArguments(args) {
  const options = {};
  for (let index = 0; index < args.length; index++) {
    const key = args[index].slice(2);
    if (!args[index].startsWith('--') || (!flags.has(key) && !values.has(key)) || Object.hasOwn(options, key)) fail('鉴权参数未知或重复。');
    if (flags.has(key)) options[key] = true;
    else {
      const value = args[++index];
      if (!value || value.startsWith('--')) fail(`缺少 --${key} 的值。`);
      options[key] = value;
    }
  }
  return options;
}

// Both public and generated entries use the same authentication operation.
export async function authCommand(project, command, options) {
  const selected = {
    workspace: options.workspace && resolve(options.workspace), harnessDirectory: options['harness-directory'],
    authMethod: options['auth-method'], ghUser: options['gh-user'], credentialWorkspace: options['credential-workspace'], credentialFile: options['credential-file'],
    credentialsFilled: Boolean(options['credentials-filled']), githubAuth: Boolean(options['github-auth']), useGlobalConfig: Boolean(options['use-global-config']),
  };
  if (command === 'auth-info') {
    if (options.url) return publicSelection(await authSelection(project, options.url, selected));
    const { values, sources } = await loadEnvironment((await inspectProject(project)).root, selected);
    return { sources, present: Object.fromEntries(Object.entries(values).map(([key, value]) => [key, Boolean(value)])), hint: '用 --url 查询特定完整远端的文件或 gh 绑定。' };
  }
  if (!options.url) throw Object.assign(new Error('缺少 --url'), { code: 'missing_argument' });
  if (command === 'auth-prepare') return prepareAuth(project, options.url, { ...selected, apply: options.apply, allowIgnore: options['allow-ignore'], replaceAuth: options['replace-auth'] });
  if (command === 'auth-verify') return verifyAuth(project, options.url, { ...selected, apiOnly: Boolean(options['api-only']), apply: Boolean(options.apply), expectedFingerprint: options['expected-fingerprint'] });
  fail('未知鉴权操作。');
}
