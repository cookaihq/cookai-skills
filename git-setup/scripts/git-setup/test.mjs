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
const tests = readdirSync(resolve(skill, 'tests')).filter(name => name.endsWith('.test.mjs') && name !== 'runtime-cleanup.test.mjs').map(name => resolve(skill, 'tests', name));
const options = {
  // Relationship and multi-repository scenarios launch real Git/CLI processes.
  // A busy host exceeded 1200 seconds with 120 passing results; preserve assertions
  // and per-operation limits while allowing the complete suite a 30-minute budget.
  cwd: skill, stdio: 'inherit', timeout: 1800000,
  env: { ...process.env, GIT_SETUP_TEST_ROOT: testRoot },
};
const result = spawnSync(process.execPath, ['--test', ...tests], options);
if (result.error) process.stderr.write(`完整测试进程未完成：${result.error.code || 'process_error'}（总预算 1800 秒）。\n`);
process.exitCode = result.status ?? 1;
// Cleanup deliberately refuses while any Git process is running. Run its real
// process checks after the Git integration suites, not concurrently with them.
if (process.exitCode === 0) {
  const cleanup = spawnSync(process.execPath, ['--test', resolve(skill, 'tests/runtime-cleanup.test.mjs')], options);
  if (cleanup.error) process.stderr.write(`运行包清理测试未完成：${cleanup.error.code || 'process_error'}。\n`);
  process.exitCode = cleanup.status ?? 1;
}
