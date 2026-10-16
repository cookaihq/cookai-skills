import { inspectProject, localGit } from './repository-facts.mjs';
import { digest, runtimeFail as fail } from './runtime-bundle.mjs';

export const choiceKey = root => `gitsetup.harness-${digest(root)}.decision`;
export async function readChoice(root) {
  const state = await inspectProject(root);
  if (!state.isGit || state.root !== root) return null;
  const value = await localGit(root, ['config', '--local', '--get-all', choiceKey(root)], true);
  if (value === null) return null;
  let choice;
  try { choice = JSON.parse(value); } catch { fail('harness_choice_invalid', '已保存 Harness 选择损坏；先修复原记录。'); }
  fields(choice, ['choice', 'fingerprint', 'evidenceFiles', 'comparison', 'directory', 'ruleFiles'], '已保存 Harness 选择');
  publicText(choice);
  if (!['keep', 'standard'].includes(choice.choice) || !Array.isArray(choice.evidenceFiles) || !/^[a-f0-9]{64}$/.test(choice.fingerprint)) fail('harness_choice_invalid', '已保存 Harness 选择结构无效。');
  choice.evidenceFiles.forEach(portablePath);
  if (choice.directory) portablePath(choice.directory);
  if (choice.ruleFiles) choice.ruleFiles.forEach(portablePath);
  return choice;
}

export async function resolveHarnessDirectory(root, directory) {
  if (directory !== undefined) return portablePath(directory);
  const saved = await readChoice(root);
  return saved?.directory ? portablePath(saved.directory) : null;
}
const object = value => value && typeof value === 'object' && !Array.isArray(value);
export function fields(value, allowed, label) {
  if (!object(value) || Object.keys(value).some(key => !allowed.includes(key))) fail('harness_config_invalid', `${label} 的字段不符合标准；不输出原内容。`);
}
export function portablePath(value) {
  if (typeof value !== 'string' || !value || /[\\\0\r\n]/.test(value) || value.startsWith('/') || /^[A-Za-z]:/.test(value) || value.split('/').some(part => !part || ['.', '..', '.git', '.credentials'].includes(part))) fail('harness_path_invalid', '需要项目内的相对文件路径，不得指向 Git 元数据或凭证。');
  return value;
}
export function publicText(value) {
  const raw = JSON.stringify(value);
  if (/\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b|https?:\/\/[^\s/"\\]*@|-----BEGIN [^-]*PRIVATE KEY-----|[?&](?:access_token|token|password|api_key)=/i.test(raw)) fail('harness_secret', 'Harness 配置、比较依据和计划不得包含凭证。');
}
