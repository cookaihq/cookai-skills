import * as fs from 'node:fs/promises';
import { realpath } from 'node:fs/promises';
import path, { resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { gitConfiguration } from './git-trust.mjs';

const exec = promisify(execFile);
export const authFail = (code, message) => { throw Object.assign(new Error(message), { code }); };
export function localEnvironment(extra = {}) {
  const env = { ...process.env, ...extra, GIT_TERMINAL_PROMPT: '0', GH_PROMPT_DISABLED: '1', LC_ALL: 'C' };
  for (const key of Object.keys(env)) if (/^GIT_(?:DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG|TRACE|CURL_VERBOSE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES)/.test(key)) delete env[key];
  Object.assign(env, gitConfiguration());
  delete env.GH_TOKEN; delete env.GITHUB_TOKEN;
  return env;
}
export async function localGit(root, args, optional = false) {
  try { return (await exec('git', ['-C', root, ...args], { env: localEnvironment(), encoding: 'utf8', timeout: 15000, maxBuffer: 2 * 1024 * 1024 })).stdout; }
  catch (error) {
    if (optional && error.code === 1) return null;
    authFail('auth_git_configuration', `无法读取或写入 ${root} 的 Git 配置；未显示命令输出。`);
  }
}

export async function isGitDirectory(directory) {
  try {
    await exec('git', ['--git-dir', directory, 'rev-parse', '--is-bare-repository'], { env: localEnvironment(), encoding: 'utf8', timeout: 15000, maxBuffer: 1024 });
    return true;
  } catch (error) {
    if (error.code === 128) return false;
    authFail('GIT_INSPECTION_FAILED', '无法验证仓库元数据目录；不按目录名称推测关系。');
  }
}

/** Resolve the primary checkout from Git metadata, never by walking parent directories. */
export async function mainCheckout(project) {
  const state = await inspectProject(project);
  if (!state.isGit) return { ...state, main: state.root };
  const core = (await localGit(state.root, ['config', '--local', '--get', 'core.worktree'], true))?.trim();
  const records = (await localGit(state.root, ['-c', 'core.quotePath=false', 'worktree', 'list', '--porcelain'])).split('\n');
  let first = records.find(line => line.startsWith('worktree '))?.slice(9);
  if (first?.startsWith('"')) {
    try { first = JSON.parse(first); } catch { authFail('auth_main_checkout_path', '主检出路径包含无法解析的 Git 转义。'); }
  }
  let main;
  try { main = await realpath(core ? resolve(state.commonDir, core) : state.gitDir === state.commonDir ? state.root : first); }
  catch { authFail('auth_main_checkout_missing', 'Git 登记的主检出不可用；先修复路径，不在当前 Worktree 创建另一份凭证。'); }
  const primary = await inspectProject(main);
  if (!primary.isGit || primary.root !== main || primary.commonDir !== state.commonDir) authFail('auth_main_checkout_changed', '主检出与当前仓库的 Git common directory 不一致。');
  return { ...state, main };
}


const fail = authFail;
const gitEnv = () => { const env = localEnvironment(); delete env.GIT_CEILING_DIRECTORIES; return env; };
const git = async (root, args, optional = false) => (await localGit(root, args, optional))?.trim() ?? null;

export async function canonical(directory) {
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
