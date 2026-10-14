/** The generated entry verifies local files before importing the operation modules. */
export const projectLauncher = directory => String.raw`#!/usr/bin/env node
import { readFile, lstat, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join, resolve, relative } from 'node:path';
import { createHash } from 'node:crypto';
try {
  const directory = fileURLToPath(new URL('./', import.meta.url));
  if (!(await lstat(join(directory, 'manifest.json'))).isFile()) throw new Error('项目清单必须是普通文件。');
  const manifest = JSON.parse(await readFile(join(directory, 'manifest.json'), 'utf8'));
  if (manifest.standard !== 'git-setup' || manifest.schemaVersion !== 1 || !manifest.files || Array.isArray(manifest.files)) throw new Error('项目 Harness 清单无效。');
  const actual = [];
  async function inventory(name = '', depth = 0) {
    if (depth > 10 || actual.length >= 300) throw new Error('项目运行包超出检查预算。');
    const path = join(directory, name), info = await lstat(path);
    if (info.isDirectory()) {
      for (const child of await readdir(path)) await inventory(name ? name+'/'+child : child, depth + 1);
    } else {
      if (!info.isFile() || info.size > 2 * 1024 * 1024) throw new Error('项目运行文件必须是普通小文件。');
      actual.push(name);
    }
  }
  for (const name of ['run.mjs', 'config.json', 'runtime']) await inventory(name);
  const required = Object.entries(manifest.files).filter(([name]) => ['run.mjs', 'config.json'].includes(name) || name.startsWith('runtime/'));
  if (!manifest.files['run.mjs'] || !manifest.files['runtime/scripts/git-setup/project-entry.mjs'] || actual.length !== required.length || actual.some(name => !Object.hasOwn(manifest.files, name))) throw new Error('项目运行包与清单不一致。');
  for (const [name, expected] of required) {
    if (!name || /[\\\0\r\n]/.test(name) || name.startsWith('/') || name.split('/').some(p => !p || p === '..' || p === '.') || !/^[a-f0-9]{64}$/.test(expected)) throw new Error('项目 Harness 清单路径无效。');
    let cursor = directory;
    for (const part of name.split('/')) { cursor = join(cursor, part); if ((await lstat(cursor)).isSymbolicLink()) throw new Error('项目 Harness 路径不能经过符号链接。'); }
    if (name === 'config.json') continue;
    if (createHash('sha256').update(await readFile(cursor)).digest('hex') !== expected) throw new Error('项目 Harness 文件变化，先检查本项目产物；不回退 Skill。');
  }
  const minimum = Number(JSON.parse(await readFile(join(directory, 'runtime/package.json'), 'utf8')).engines.node.replace('>=', ''));
  if (!Number.isSafeInteger(minimum) || Number(process.versions.node.split('.')[0]) < minimum) throw new Error('Node.js 不满足运行包声明的版本。');
  const { runProject } = await import('./runtime/scripts/git-setup/project-entry.mjs');
  const projectDirectory = ${JSON.stringify(directory)};
  const root = resolve(directory, ${JSON.stringify('../'.repeat(directory.split('/').length))});
  if (relative(root, directory).replaceAll('\\', '/') !== projectDirectory || manifest.directory !== projectDirectory) throw new Error('项目入口与根目录关系不符。');
  await runProject(root, projectDirectory);
} catch (error) {
  process.stderr.write(JSON.stringify({status:'error', code:'harness_runtime_invalid', message:'项目运行文件无法验证或执行；检查 Node >=20、清单及项目文件，不回退 Skill 安装目录。'})+'\n');
  process.exitCode = 1;
}
`;
