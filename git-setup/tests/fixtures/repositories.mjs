import { mkdir, mkdtemp, writeFile, appendFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { devNull } from 'node:os';

const exec = promisify(execFile);
export const skill = fileURLToPath(new URL('../../', import.meta.url));
export const cli = join(skill, 'scripts/git-setup/cli.mjs');

export async function registerRepositories(project, paths) {
  await appendFile(join(project, '.gitignore'), '\n/data/git-setup/\n');
  await mkdir(join(project, 'data/git-setup'), { recursive: true });
  const input = join(project, 'data/git-setup/registration-input.json');
  await writeFile(input, JSON.stringify({ choice: 'standard', directory: 'fixture-git', ruleFiles: ['AGENTS.md'] }));
  const run = async args => JSON.parse((await exec(process.execPath, [cli, ...args], { timeout: 60000 })).stdout);
  const args = ['--project', project, '--input', input];
  const plan = await run(['harness-plan', ...args]);
  await run(['harness-apply', ...args, '--expected-fingerprint', plan.fingerprint, '--plan-file', join(project, 'data/git-setup/registration-plan.json'), '--apply']);
  await writeFile(join(project, 'fixture-git/repos.conf'), paths.join('\n')+'\n');
}
export async function repositoryFixture() {
  const base = resolve(process.env.GIT_SETUP_TEST_ROOT || '../../tmp/git-setup/relationships');
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'repositories-'));
  const env = { ...process.env, GIT_CEILING_DIRECTORIES: root, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_SETUP_|^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG)/.test(key)) delete env[key];
  const git = async (cwd, ...args) => (await exec('git', ['-C', cwd, '-c', 'commit.gpgsign=false', ...args], { env, timeout: 30000 })).stdout.trim();
  const repo = async name => {
    const directory = join(root, name); await mkdir(directory, { recursive: true });
    await git(directory, 'init', '-q', '-b', 'main');
    await git(directory, 'config', 'user.name', 'Repository Fixture');
    await git(directory, 'config', 'user.email', 'repository@example.invalid');
    await git(directory, 'config', 'core.hooksPath', devNull);
    await writeFile(join(directory, '.gitignore'), '.credentials/\ndata/git-setup/\n');
    await writeFile(join(directory, 'README.md'), name+'\n');
    await git(directory, 'add', '.'); await git(directory, 'commit', '-qm', 'fixture');
    return directory;
  };
  const run = async (entry, args, cwd = root) => {
    try { const result = await exec(process.execPath, [entry, ...args], { cwd, env, timeout: 90000 }); return { exit: 0, ...JSON.parse(result.stdout) }; }
    catch (error) { return { exit: error.code, ...JSON.parse(error.stdout || error.stderr) }; }
  };
  const install = async (project, input = {}, entry = cli) => {
    const inputFile = join(root, `input-${Date.now()}-${Math.random()}.json`);
    await writeFile(inputFile, JSON.stringify({ choice: 'standard', directory: 'vcs', ruleFiles: ['AGENTS.md'], ...input }));
    const args = ['--project', project, '--input', inputFile];
    const plan = await run(entry, ['harness-plan', ...args]);
    if (plan.status !== 'planned') throw new Error(JSON.stringify(plan));
    const applied = await run(entry, ['harness-apply', ...args, '--plan-file', join(project, 'data/git-setup/install.json'), '--expected-fingerprint', plan.fingerprint, '--apply']);
    if (!applied.complete) throw new Error(JSON.stringify(applied));
    return applied;
  };
  return { root, env, git, repo, run, install };
}
