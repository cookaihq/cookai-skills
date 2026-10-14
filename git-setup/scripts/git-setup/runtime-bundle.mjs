import { readFile, readdir, lstat, mkdir, writeFile, rename, rm, realpath } from 'node:fs/promises';
import { resolve, join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';

const source = fileURLToPath(new URL('../../', import.meta.url));
export const digest = value => createHash('sha256').update(typeof value === 'string' || Buffer.isBuffer(value) ? value : JSON.stringify(value)).digest('hex');
export const runtimeFail = (code, message) => { throw Object.assign(new Error(message), { code }); };

/** Copy the complete shipped Node runtime; no source-machine paths or credentials. */
export async function runtimeBundle() {
  const files = {};
  for (const name of ['package.json', 'LICENSE']) files[name] = await readFile(join(source, name), 'utf8');
  const directory = join(source, 'scripts/git-setup');
  for (const name of (await readdir(directory)).sort()) {
    if (!name.endsWith('.mjs') || name === 'test.mjs') continue;
    const info = await lstat(join(directory, name));
    if (!info.isFile() || info.isSymbolicLink()) runtimeFail('runtime_source_invalid', '运行包源文件必须是普通文件。');
    files[`scripts/git-setup/${name}`] = await readFile(join(directory, name), 'utf8');
  }
  const hashes = Object.fromEntries(Object.entries(files).map(([name, value]) => [name, digest(value)]));
  return { files, hashes, fingerprint: digest(hashes), version: JSON.parse(files['package.json']).version };
}

/** Reject symlink ancestors before either reading generated code or writing it. */
export async function safePath(root, path) {
  root = resolve(root); path = resolve(path);
  const rel = relative(root, path);
  if (rel.startsWith('..') || resolve(root, rel) !== path) runtimeFail('runtime_path_invalid', '运行文件必须位于已解析的目标目录内。');
  if (await realpath(root) !== root) runtimeFail('runtime_path_symlink', '运行目录根不得经过符号链接。');
  let cursor = root;
  for (const part of rel.split(/[\\/]/).filter(Boolean)) {
    cursor = join(cursor, part);
    try { if ((await lstat(cursor)).isSymbolicLink()) runtimeFail('runtime_path_symlink', '运行文件路径不得经过符号链接。'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}

export async function packageFiles(directory) {
  const result = {};
  async function visit(relativePath) {
    const path = join(directory, relativePath);
    const info = await lstat(path);
    if (info.isSymbolicLink()) runtimeFail('runtime_path_symlink', '运行包不能包含符号链接。');
    if (info.isDirectory()) {
      for (const name of (await readdir(path)).sort()) await visit(join(relativePath, name));
    } else {
      if (!info.isFile() || info.size > 2 * 1024 * 1024 || Object.keys(result).length >= 300) runtimeFail('runtime_package_invalid', '运行包包含非普通文件或超出检查预算。');
      result[relativePath.replaceAll('\\', '/')] = digest(await readFile(path));
    }
  }
  await visit('');
  return result;
}

export async function writePackage(directory, files) {
  for (const [name, content] of Object.entries(files)) {
    const path = join(directory, name);
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    await writeFile(path, content, { flag: 'wx', mode: 0o600 });
  }
}

/** Immutable, content-addressed runtime in this repository's actual common directory. */
export async function installHelperRuntime(commonDir) {
  const bundle = await runtimeBundle();
  const parent = join(commonDir, 'git-setup');
  const directory = join(parent, bundle.fingerprint);
  await safePath(commonDir, directory);
  await mkdir(parent, { recursive: true, mode: 0o700 });
  const verify = async () => {
    const actual = await packageFiles(directory);
    if (Object.keys(actual).length !== Object.keys(bundle.hashes).length || Object.entries(bundle.hashes).some(([name, value]) => actual[name] !== value)) runtimeFail('runtime_modified', '已安装的 Git helper 运行包发生变化；保留现场并修复，不覆盖或回退。');
  };
  try { await lstat(directory); await verify(); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    const temporary = join(parent, `.install-${randomUUID()}`);
    try {
      await mkdir(temporary, { mode: 0o700 });
      await writePackage(temporary, bundle.files);
      try { await rename(temporary, directory); }
      catch (renameError) { if (!['EEXIST', 'ENOTEMPTY'].includes(renameError.code)) throw renameError; }
      await verify();
    } finally { await rm(temporary, { recursive: true, force: true }); }
  }
  return { file: join(directory, 'scripts/git-setup/credential-helper.mjs'), fingerprint: bundle.fingerprint };
}
