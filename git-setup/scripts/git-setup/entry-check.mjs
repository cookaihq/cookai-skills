import { inspect, redact } from './git.mjs';
import { loadEnvironment } from './config.mjs';
import { localGit } from './auth-roots.mjs';
import { inspectAuth, boundRepositoryRelationship } from './auth.mjs';
import { harnessFacts } from './harness.mjs';
import { assertHarnessTargets } from './harness-config.mjs';
import { digest } from './runtime-bundle.mjs';

export async function entryCheck(project, options = {}) {
  const state = await inspect(project);
  const root = state.root || state.project;
  if (state.bare) return { status: 'checked', project: state.project, local: state, configuration: [], authentication: [], harness: { status: 'not_applicable', reason: 'bare_repository' }, checks: [{ field: 'repository', status: 'needs_input', message: 'bare 仓库没有项目工作区；先选择实际项目检出。' }], fingerprint: digest(state), changed: false, network: 'not_performed', actualPush: 'not_performed' };
  let raw = '';
  if (state.initialized && !state.bare) raw = await localGit(root, ['config', '--null', '--show-origin', '--show-scope', '--get-regexp', String.raw`^(user\.(name|email)|remote\..*\.(url|pushurl|fetch|push|mirror)|branch\..*\.(remote|merge|pushremote)|push\..*|remote\.pushdefault|core\.hookspath|credential\..*|http\..*(extraheader|followredirects)|url\..*\.(insteadof|pushinsteadof))$`], true) || '';
  const parts = raw.split('\0'), configuration = [];
  for (let index = 0; index + 2 < parts.length; index += 3) {
    const [scope, origin, field] = parts.slice(index, index + 3);
    const split = field.indexOf('\n'), key = split < 0 ? field : field.slice(0, split), value = split < 0 ? '' : field.slice(split + 1);
    // Helpers, headers and username config can embed arbitrary secrets: never echo them.
    const sensitive = /^credential\./i.test(key) || /extraheader$/i.test(key);
    configuration.push({ scope, origin: redact(origin), key: redact(key), value: sensitive ? (value ? '[configured; value hidden]' : '[empty; resets inherited helpers]') : redact(value),
      ...(sensitive ? { authenticationEvidence: 'configuration_presence_only; not_an_authentication_choice' } : {}) });
  }
  let environment;
  try {
    const loaded = await loadEnvironment(root, { useGlobalConfig: Boolean(options.useGlobalConfig) });
    environment = { sources: loaded.sources, present: Object.fromEntries(Object.entries(loaded.values).map(([key, value]) => [key, Boolean(value)])) };
  } catch (error) { environment = { status: 'invalid', code: error.code, message: error.message }; }
  const relationship = await boundRepositoryRelationship(root, options);
  const harness = await harnessFacts(root, { directory: options.workspace ? undefined : options.directory });
  const authentication = [];
  for (const url of new Set((state.remotes || []).flatMap(remote => [...remote.fetch, ...remote.push]))) authentication.push({ url, ...await inspectAuth(root, url, options) });
  const checks = [];
  checks.push({ field: 'repository', status: state.insideParent || state.bare ? 'needs_input' : state.initialized ? 'pass' : 'missing', message: state.insideParent ? '当前是父仓普通子目录；先明确实际项目，不自动修改父仓。' : state.type });
  checks.push({ field: 'author', status: state.author?.name && state.author?.email ? 'present' : 'missing', message: '值及来源见 configuration；是否符合项目指定身份由 Agent 读取实际项目规则后判断。' });
  checks.push({ field: 'remotes', status: state.remotes?.length ? 'present' : 'missing' });
  checks.push({ field: 'tracking', status: state.upstream ? 'present' : 'not_configured', message: '未配置 upstream 不自动认定错误；结合本次发布流程及项目规则判断。' });
  checks.push({ field: 'harness', status: harness.choiceChanged ? 'changed' : harness.installed?.status || 'needs_choice' });
  if (harness.installed?.config) {
    try { await assertHarnessTargets(root, harness.installed.config); checks.push({ field: 'harness.targets', status: 'pass' }); }
    catch (error) { checks.push({ field: 'harness.targets', status: 'invalid', code: error.code, message: error.message }); }
  }
  return { status: 'checked', project: state.project, local: state, configuration, environment, harness, authentication, relationship, checks,
    authenticationPolicy: { githubHttpsChoices: ['gh', 'github-token'], cnbChoices: ['cnb-url'],
      observedHelpersAreSelections: false, message: '检测到已有 helper 只证明配置存在；它不是第三种 GitHub 鉴权选项，也不证明账号已被选择。已有显式 legacy 绑定按兼容规则单独核对。' },
    fingerprint: digest({ project: root, relationship: relationship.fingerprint, configuration: digest(raw), harness: harness.fingerprint, remotes: state.remotes, authentication }),
    network: 'not_performed', pushPreflight: 'not_performed', actualPush: 'not_performed', changed: false,
    agentReview: ['读取实际项目规则核对作者及项目专属配置', '读取 candidates 对应实现、hooks/CI 及其调用方，按行为比较标准', '仅在事实和标准未变时复用已有明确选择'] };
}
