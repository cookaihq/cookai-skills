import { mkdir, mkdtemp, writeFile, appendFile, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { devNull } from 'node:os';

import { inspect } from '../../scripts/git-setup/git.mjs';
import { confirmedTargets } from '../../scripts/git-setup/project-policy.mjs';
import { registeredRepositories } from '../../scripts/git-setup/repository-relationship.mjs';
import { planDocument, writeDocument } from '../../scripts/git-setup/project-document.mjs';
const exec = promisify(execFile);
export const skill = fileURLToPath(new URL('../../', import.meta.url));
export const cli = join(skill, 'scripts/git-setup/cli.mjs');

export async function registerRepositories(project, paths) {
  await appendFile(join(project, '.gitignore'), '\n/data/git-setup/\n');
  await mkdir(join(project, 'data/git-setup'), { recursive: true });
  await mkdir(join(project, 'fixture-git'), { recursive: true });
  await writeFile(join(project, 'fixture-git/repos.conf'), paths.join('\n')+'\n');
  await writeFile(join(project, 'fixture-git/config.json'), JSON.stringify({schemaVersion:1, targets:confirmedTargets(await inspect(project)), sync:null, extensions:[],repositories:{}}));
}
export async function repositoryFixture() {
  const base = resolve(process.env.GIT_SETUP_TEST_ROOT || '../../tmp/git-setup/relationships');
  await mkdir(base, { recursive: true });
  const root = await mkdtemp(join(base, 'repositories-'));
  const env = { ...process.env, GIT_CEILING_DIRECTORIES: root, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_SETUP_|^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG)/.test(key)) delete env[key];
  env.GIT_CONFIG_NOSYSTEM = '1';
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
    try { const result = await exec(process.execPath, [entry, ...args], { cwd, env, timeout: 300000 }); return { exit: 0, ...JSON.parse(result.stdout) }; }
    catch (error) { return { exit: error.code, ...JSON.parse(error.stdout || error.stderr) }; }
  };
  const configure = async (project, input = {}) => {
    const directory = input.directory || 'vcs';
    await mkdir(join(project, directory), { recursive: true });
    const path = join(project, directory, 'config.json');
    let previous;
    try { previous = JSON.parse(await readFile(path, 'utf8')); } catch (error) { if(error.code !== 'ENOENT') throw error; }
    const config = previous || { schemaVersion: 1, targets: confirmedTargets(await inspect(project)), sync: null, extensions: [], repositories: {} };
    if(input.refreshTargets) config.targets = confirmedTargets(await inspect(project));
    for(const key of ['sync','extensions']) if(Object.hasOwn(input,key)) config[key]=input[key];
    const registration = await registeredRepositories(project, { policyDirectory: directory });
    for(const item of registration.entries) if(item.repository) {
      if(!config.repositories[item.path] || input.repositories?.[item.path]) config.repositories[item.path] = { targets: confirmedTargets(await inspect(item.repository.root)), sync: input.repositories?.[item.path]?.sync ?? config.repositories[item.path]?.sync ?? null };
    }
    await writeFile(path, JSON.stringify(config,null,2)+'\n');
    const doc = { path:'docs/agent/git.md', content:'# Project Git\n\nRead project rules; choose the repository and remote explicitly.\n', ruleFiles:['AGENTS.md'] };
    const plan = await planDocument(project, doc);
    return writeDocument(project, doc, {apply:true,expectedFingerprint:plan.fingerprint});
  };
  const command = (project, args, cwd = root) => run(cli, [...args, '--project', project, '--policy-directory', 'vcs'], cwd);
  const selectSource = async (project, source, options = {}) => {
    const input = join(root, 'source-'+Math.random()+'.json'); await writeFile(input,JSON.stringify(source));
    return run(cli, ['source-save','--project',project,'--input',input,'--apply',...(options.workspace?['--workspace',options.workspace,'--policy-directory','vcs']:[])]);
  };
  const credential = async (project, url, method, value, { repository, file } = {}) => {
    const selection = repository ? ['--repository', repository] : [];
    const args = [...selection, '--url', url, '--auth-method', method, ...(file ? ['--credential-file', file] : [])];
    const prepared = await command(project, ['credential-prepare', ...args, '--apply']);
    if (!prepared.credentialFile) throw new Error(JSON.stringify(prepared));
    await writeFile(prepared.credentialFile, value, {mode:0o600});
    const input = join(root,'source-'+Math.random()+'.json');
    await writeFile(input,JSON.stringify({url,method,file:prepared.credentialFile}));
    const saved = await command(project, ['source-save', ...selection, '--input',input,'--apply']);
    if(saved.status !== 'source_saved') throw new Error(JSON.stringify(saved));
    const verified = await command(project, ['credential-check', ...selection,'--url',url]);
    return {prepared,verified};
  };
  return { root, env, git, repo, run, configure, command, selectSource, credential };
}
