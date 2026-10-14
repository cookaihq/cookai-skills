import { execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const skill = fileURLToPath(new URL('../../', import.meta.url));
let root = skill;
try {
  root = execFileSync('git', ['-C', skill, 'rev-parse', '--show-superproject-working-tree'], { encoding: 'utf8', timeout: 10000 }).trim() || skill;
} catch { /* A copied installation uses its own ignored tmp directory. */ }
const testRoot = process.env.GIT_SETUP_TEST_ROOT || resolve(root, 'tmp/git-setup/tests');
const ignore = spawnSync('git', ['-C', root, 'check-ignore', '-q', testRoot], { timeout: 10000 });
if (ignore.status !== 0) throw new Error(`测试临时目录未被 Git 忽略：${testRoot}`);
mkdirSync(testRoot, { recursive: true });
const tests = readdirSync(resolve(skill, 'tests')).filter(name => name.endsWith('.test.mjs')).map(name => resolve(skill, 'tests', name));
const result = spawnSync(process.execPath, ['--test', ...tests], {
  // Relationship and multi-repository scenarios launch real Git/CLI processes.
  cwd: skill, stdio: 'inherit', timeout: 1200000,
  env: { ...process.env, GIT_SETUP_TEST_ROOT: testRoot },
});
if (result.error) process.stderr.write(`完整测试进程未完成：${result.error.code || 'process_error'}（总预算 1200 秒）。\n`);
process.exitCode = result.status ?? 1;
