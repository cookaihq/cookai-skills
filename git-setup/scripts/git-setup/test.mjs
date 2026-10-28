import { execFileSync, spawnSync } from 'node:child_process';
import { mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { testEnvironment } from '../../tests/fixtures/test-environment.mjs';

const skill = fileURLToPath(new URL('../../', import.meta.url));
let root = skill;
try {
  root = execFileSync('git', ['-C', skill, 'rev-parse', '--show-superproject-working-tree'], { encoding: 'utf8', timeout: 10000 }).trim() || skill;
} catch { /* A copied installation uses its own ignored tmp directory. */ }
const testRoot = process.env.GIT_SETUP_TEST_ROOT || resolve(root, 'tmp/git-setup/tests');
const ignore = spawnSync('git', ['-C', root, 'check-ignore', '-q', testRoot], { timeout: 10000 });
if (ignore.status !== 0) throw new Error(`测试临时目录未被 Git 忽略：${testRoot}`);
mkdirSync(testRoot, { recursive: true });
const tests = process.argv.length > 2 ? process.argv.slice(2).map(file => resolve(file))
  : readdirSync(resolve(skill, 'tests')).filter(name => name.endsWith('.test.mjs')).map(name => resolve(skill, 'tests', name));
const options = {
  // Relationship, PR and Release scenarios launch real Git/CLI processes.
  // Keep per-operation limits while allowing a selected Windows suite to finish.
  cwd: skill, stdio: 'inherit', timeout: 3600000,
  env: testEnvironment(testRoot),
};
const result = spawnSync(process.execPath, ['--test', '--test-concurrency=2', '--test-reporter=tap', ...tests], options);
if (result.error) process.stderr.write(`完整测试进程未完成：${result.error.code || 'process_error'}（总预算 3600 秒）。\n`);
process.exitCode = result.status ?? 1;
