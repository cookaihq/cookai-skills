import { readFile, lstat } from 'node:fs/promises';
import { join } from 'node:path';
import { inspect, validateUrl } from './git.mjs';
import { authTarget } from './auth-roots.mjs';
import { digest, runtimeFail as fail, safePath } from './runtime-bundle.mjs';

export { choiceKey, readChoice, resolveHarnessDirectory, fields, portablePath, publicText } from './harness-location.mjs';
import { resolveHarnessDirectory, fields, portablePath, publicText } from './harness-location.mjs';
export const endpointId = url => { validateUrl(url); return authTarget(url)?.key || url; };
const object = value => value && typeof value === 'object' && !Array.isArray(value);
function validatePolicy(config, directory) {
  if (!object(config.targets) || !Array.isArray(config.extensions)) fail('harness_config_invalid', 'Harness 目标或扩展无效。');
  for (const [name, target] of Object.entries(config.targets)) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) fail('harness_config_invalid', '远端名称无效。');
    fields(target, ['fetch', 'push'], '已确认目标');
    if (![target.fetch, target.push].every(value => typeof value === 'string' && value && !/[\r\n\0]/.test(value))) fail('harness_config_invalid', '目标身份必须包含已确认的 fetch/push 值。');
  }
  if (config.sync !== null) {
    fields(config.sync, ['source', 'targets', 'refs'], '同步策略');
    const { source, targets, refs } = config.sync;
    if (!config.targets[source] || !Array.isArray(targets) || !targets.length || targets.includes(source) || new Set(targets).size !== targets.length || targets.some(name => !config.targets[name])
      || !Array.isArray(refs) || !refs.length || new Set(refs).size !== refs.length || refs.some(ref => typeof ref !== 'string' || !/^refs\/(heads|tags)\/.+/.test(ref) || /[\s:*?\[\\~^\0]/.test(ref))) fail('harness_config_invalid', '同步必须指定不同的已确认远端和明确的完整引用，禁止通配符、强推和删除。');
  }
  const ids = new Set();
  for (const extension of config.extensions) {
    fields(extension, ['id', 'path', 'sha256', 'event', 'description', 'timeoutMs'], '项目扩展');
    portablePath(extension.path);
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(extension.id) || ids.has(extension.id) || !extension.path.endsWith('.mjs') || (directory && (extension.path === directory || (extension.path.startsWith(`${directory}/`) && !extension.path.startsWith(`${directory}/extensions/`))))
      || !/^[a-f0-9]{64}$/.test(extension.sha256) || !['after-publish', 'after-sync', 'explicit'].includes(extension.event) || typeof extension.description !== 'string' || !extension.description.trim()
      || (extension.event === 'explicit' ? extension.timeoutMs !== undefined : !Number.isSafeInteger(extension.timeoutMs) || extension.timeoutMs < 1000 || extension.timeoutMs > 300000)) fail('harness_config_invalid', '扩展必须有唯一名称、项目内 Node 文件、内容指纹及触发方式；自动扩展有有限超时，explicit 入口由调用方管理执行，不能设置 timeoutMs。');
    ids.add(extension.id);
  }
  return config;
}

export function validateHarnessConfig(config, directory) {
  fields(config, ['schemaVersion', 'targets', 'sync', 'extensions', 'repositories'], 'Harness 配置');
  publicText(config);
  if (config.schemaVersion !== 1) fail('harness_config_invalid', 'Harness 配置版本无效。');
  validatePolicy(config, directory);
  if (config.repositories !== undefined) {
    if (!object(config.repositories)) fail('harness_config_invalid', '各仓策略必须按登记路径指定。');
    for (const [name, policy] of Object.entries(config.repositories)) {
      portablePath(name); fields(policy, ['targets', 'sync'], '所选仓库策略');
      validatePolicy({ ...policy, extensions: [] }, directory);
    }
  }
  return config;
}

export function repositoryPolicy(config, key) {
  if (key === '.') return { targets: config.targets, sync: config.sync, extensions: config.extensions };
  const policy = config.repositories?.[key];
  if (!policy) fail('harness_repository_policy_missing', '所选仓库没有已确认策略；先按该仓实际远端生成策略，不套用外仓设置。');
  return { ...policy, extensions: [] };
}

export function confirmedTargets(state) {
  const targets = {};
  for (const remote of state.remotes) {
    if (remote.mirror || remote.fetch.length !== 1 || remote.push.length !== 1) fail('harness_complex_remote', '采用标准前先明确多地址或镜像远端的处理方式。');
    targets[remote.name] = { fetch: endpointId(remote.fetch[0]), push: endpointId(remote.push[0]) };
  }
  return targets;
}

export async function readHarnessConfig(root, directory) {
  directory = await resolveHarnessDirectory(root, directory);
  if (!directory) return { exists: false, directory: null, fingerprint: digest('missing') };
  const path = join(root, directory, 'config.json');
  await safePath(root, path);
  let raw;
  try {
    const info = await lstat(path);
    if (!info.isFile() || info.size > 262144) fail('harness_config_invalid', 'Harness 配置不是普通小文件。');
    raw = await readFile(path, 'utf8');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    fail('harness_config_missing', '项目已有标准 Harness，但配置缺失；修复原配置，不回退旧同步策略。');
  }
  let config;
  try { config = JSON.parse(raw); } catch { fail('harness_config_invalid', 'Harness 配置 JSON 无效；不回退旧同步配置。'); }
  return { exists: true, path, directory, config: validateHarnessConfig(config, directory), fingerprint: digest(raw) };
}

export async function assertHarnessTargets(root, config, names = Object.keys(config.targets)) {
  const state = await inspect(root);
  for (const name of names) {
    const actual = state.remotes?.find(remote => remote.name === name), expected = config.targets[name];
    if (!expected || !actual || actual.mirror || actual.fetch.length !== 1 || actual.push.length !== 1 || endpointId(actual.fetch[0]) !== expected.fetch || endpointId(actual.push[0]) !== expected.push) fail('harness_target_changed', `远端 ${name} 的目标与已确认 Harness 配置不同；重新检查目标和鉴权，不跟随变更写入。`);
  }
  return state;
}

/** Sync only consumes the explicitly installed project policy. */
export async function syncConfiguration(root, directory) {
  const installed = await readHarnessConfig(root, directory);
  if (!installed.exists) fail('harness_config_missing', '同步需要已确认的项目 Harness 位置和策略；不读取旧项目 JSON。');
  await assertHarnessTargets(root, installed.config, [installed.config.sync?.source, ...(installed.config.sync?.targets || [])].filter(Boolean));
  return { ...installed, config: { sync: installed.config.sync }, source: 'harness' };
}
