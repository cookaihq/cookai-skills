import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const skill = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const worktree = path.resolve(skill, '../..');
const cliFile = path.join(skill, 'scripts/git-setup/cli.mjs');

test('documented CLI adds a mirror, preserves local Issue conventions, recovers and installs future-session rules', async t => {
  const base = path.join(process.env.GIT_SETUP_TEST_ROOT ?? path.join(worktree, 'tmp/git-setup'), 'forward-test');
  await fs.mkdir(base, { recursive: true });
  const root = await fs.mkdtemp(path.join(base, 'run-'));
  const outer = path.join(root, 'management');
  const app = path.join(outer, 'application');
  const origin = path.join(root, 'primary.git');
  const mirror = path.join(root, 'secondary.git');
  const log = [];
  const anomalies = [];
  const env = { ...process.env, GIT_TERMINAL_PROMPT: '0' };
  for (const key of Object.keys(env)) if (/^GIT_SETUP_/.test(key)) delete env[key];
  t.after(async () => {
    await fs.writeFile(path.join(root, 'observations.json'), `${JSON.stringify(log, null, 2)}\n`);
    t.diagnostic(`Forward-test evidence: ${root}`);
  });

  async function git(cwd, ...args) {
    return (await exec('git', ['-C', cwd, ...args], { encoding: 'utf8', timeout: 30000, env })).stdout.trim();
  }
  async function cli(project, command, ...args) {
    try {
      const result = await exec(process.execPath, [cliFile, command, '--project', project, ...args], { encoding: 'utf8', timeout: 90000, maxBuffer: 4 * 1024 * 1024, env });
      log.push({ command, project, args, exit: 0, stdout: result.stdout, stderr: result.stderr });
      return result.stdout;
    } catch (error) {
      log.push({ command, project, args, exit: error.code, stdout: error.stdout, stderr: error.stderr });
      if (command === 'sync' && !args.includes('--apply') && JSON.parse(error.stdout || '{}').status === 'planned') {
        anomalies.push('A successfully generated sync preview returned a nonzero exit code.');
        return error.stdout;
      }
      throw new Error(`CLI ${command} failed: ${error.stdout || ''}${error.stderr || ''}`);
    }
  }
  async function initExisting(directory) {
    await fs.mkdir(directory, { recursive: true });
    await git(directory, 'init', '--quiet', '--initial-branch=main');
    await git(directory, 'config', 'user.name', 'CLI fixture');
    await git(directory, 'config', 'user.email', 'cli-fixture@example.invalid');
    await git(directory, 'config', 'core.hooksPath', path.join(directory, '.fixture-no-hooks'));
  }
  await initExisting(outer);
  await fs.writeFile(path.join(outer, '.gitignore'), 'application/\ndata/git-setup/\n');
  await fs.mkdir(path.join(outer, 'issues'));
  await fs.writeFile(path.join(outer, 'issues/08-existing-item.md'), '# Existing item\n\nStatus: open\n');
  await fs.writeFile(path.join(outer, 'AGENTS.md'), '# Existing project rules\n\nAll Issues belong to this repository under issues/. Use NN-slug.md and include Status: open.\n');
  await git(outer, 'add', '.gitignore', 'AGENTS.md', 'issues/08-existing-item.md');
  await git(outer, 'commit', '--quiet', '-m', 'existing local Issue convention');
  await initExisting(app);
  await fs.writeFile(path.join(app, 'README.md'), '# Existing application\n');
  await fs.writeFile(path.join(app, 'AGENTS.md'), '# Existing child rules\n\nRetain this project-specific instruction.\n');
  await git(app, 'add', 'README.md', 'AGENTS.md');
  await git(app, 'commit', '--quiet', '-m', 'existing application');
  await git(root, 'init', '--bare', '--quiet', origin);
  await git(root, 'init', '--bare', '--quiet', mirror);
  await git(app, 'remote', 'add', 'origin', origin);
  await git(app, 'push', '--quiet', '--set-upstream', 'origin', 'main');
  await git(app, 'branch', 'local-only');
  await fs.appendFile(path.join(app, 'README.md'), '\nSelected main update.\n');
  await git(app, 'add', 'README.md');
  await git(app, 'commit', '--quiet', '-m', 'approved main update');
  await fs.writeFile(path.join(app, 'staged-but-not-published.txt'), 'Preserve the index.\n');
  await git(app, 'add', 'staged-but-not-published.txt');
  await fs.appendFile(path.join(app, 'README.md'), '\nPreserve this uncommitted edit.\n');
  const head = await git(app, 'rev-parse', 'HEAD');
  const index = await git(app, 'ls-files', '--stage');
  const readme = await fs.readFile(path.join(app, 'README.md'), 'utf8');
  const upstream = await git(app, 'rev-parse', '--abbrev-ref', '@{upstream}');

  await cli(app, 'inspect');
  await cli(app, 'remote-add', '--name', 'backup', '--url', mirror, '--apply');
  const appConfig = path.join(root, 'application-config.json');
  await fs.writeFile(appConfig, JSON.stringify({ schemaVersion: 1, remotes: { origin, backup: mirror }, sync: { source: 'origin', targets: ['backup'], refs: ['refs/heads/main'] } }));
  await cli(app, 'config-write', '--input', appConfig, '--apply');
  await cli(app, 'config-check');
  await cli(app, 'publish', '--remote', 'origin', '--ref', 'refs/heads/main', '--apply');
  await cli(app, 'sync');
  const beforeMirror = await git(mirror, 'show-ref', '--heads').catch(() => '');
  assert.equal(beforeMirror, '', 'Preview must not write remote refs');
  await cli(app, 'sync', '--apply');
  assert.equal(await git(origin, 'rev-parse', 'refs/heads/main'), head);
  assert.equal(await git(mirror, 'rev-parse', 'refs/heads/main'), head);
  assert.equal(await git(mirror, 'show-ref', '--verify', 'refs/heads/local-only').catch(() => ''), '');
  assert.equal(await git(app, 'rev-parse', '--abbrev-ref', '@{upstream}'), upstream);
  assert.equal(await git(app, 'ls-files', '--stage'), index);
  assert.equal(await fs.readFile(path.join(app, 'README.md'), 'utf8'), readme);

  const outerConfig = path.join(root, 'management-config.json');
  await fs.writeFile(outerConfig, JSON.stringify({ schemaVersion: 1, issues: { backend: 'local', target: 'outer', local: { directory: 'issues', filename: '{number}-{slug}.md', numbering: { start: 1, width: 2 } } }, workspace: { repositories: [{ path: 'application', issues: 'inherit' }], issuePolicy: { backend: 'local' } } }));
  await cli(outer, 'config-write', '--input', outerConfig, '--apply');
  const resolved = await cli(app, 'issue-resolve', '--workspace', outer);
  assert.match(resolved, /"backend":\s*"local"/);
  const body = path.join(root, 'issue-body.md');
  await fs.writeFile(body, 'Status: open\n\nKeep Issues in the existing management repository while mirroring application code.\n');
  const created = await cli(app, 'issue-create', '--workspace', outer, '--title', 'Preserve local Issue conventions', '--body-file', body, '--operation-id', 'forward-local-issue-001', '--apply');
  assert.match(created, /保存到本地/);
  const issueFiles = (await fs.readdir(path.join(outer, 'issues'))).filter(name => name.endsWith('.md'));
  assert.equal(issueFiles.length, 2);
  const newIssueName = issueFiles.find(name => name !== '08-existing-item.md');
  assert.match(newIssueName, /^09-.*\.md$/);
  const newIssue = path.join(outer, 'issues', newIssueName);
  assert.match(await fs.readFile(newIssue, 'utf8'), /Status: open/);
  const recovered = await cli(app, 'issue-recover', '--workspace', outer, '--operation-id', 'forward-local-issue-001');
  assert.match(recovered, /保存到本地/);
  assert.deepEqual((await fs.readdir(path.join(outer, 'issues'))).sort(), issueFiles.sort());
  assert.equal(await fs.stat(path.join(app, 'issues')).then(() => true, () => false), false);

  await fs.symlink('AGENTS.md', path.join(app, 'CLAUDE.md'));
  await cli(app, 'install-rules', '--workspace', outer, '--rule-file', 'AGENTS.md', '--rule-file', 'CLAUDE.md');
  assert.equal(await fs.readFile(path.join(app, 'AGENTS.md'), 'utf8'), '# Existing child rules\n\nRetain this project-specific instruction.\n');
  await cli(app, 'install-rules', '--workspace', outer, '--rule-file', 'AGENTS.md', '--rule-file', 'CLAUDE.md', '--apply');
  const rules = await fs.readFile(path.join(app, 'AGENTS.md'), 'utf8');
  assert.match(rules, /Retain this project-specific instruction/);
  assert.match(rules, /\.git-setup\/issue-routing\.md/);
  const routing = await fs.readFile(path.join(app, '.git-setup/issue-routing.md'), 'utf8');
  assert.match(routing, /issue-resolve/);
  assert.match(routing, /--workspace/);
  assert.equal(await fs.readFile(path.join(app, 'CLAUDE.md'), 'utf8'), rules);
  await cli(app, 'install-rules', '--workspace', outer, '--rule-file', 'AGENTS.md', '--apply');
  assert.equal(await fs.readFile(path.join(app, 'AGENTS.md'), 'utf8'), rules);
  // Each call is a separate Node process: persisted rules/config survive process boundaries.
  const workspaceReference = /路径基准：`([^`]+)`/.exec(routing)?.[1];
  assert.ok(workspaceReference, 'Installed routing instructions identify the workspace relative to the project');
  const fresh = await cli(app, 'issue-resolve', '--workspace', path.resolve(app, workspaceReference));
  assert.equal(JSON.parse(fresh).backend, 'local');
  assert.equal(JSON.parse(fresh).configOwner, outer);
  assert.equal(await git(app, 'rev-parse', 'HEAD'), head);
  log.push({ observed: 'selected main exists at both remotes; private branch absent; index/upstream/uncommitted README retained; exactly one new numbered outer local Issue; recovery returns same Issue; rules retained and idempotent across separate CLI processes', issueFile: newIssue });
  assert.deepEqual(anomalies, []);
});
