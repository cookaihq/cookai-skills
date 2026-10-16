import { readFile, lstat, mkdir, writeFile, chmod } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { digest, packageFiles, safePath, runtimeFail as fail } from './runtime-bundle.mjs';

export const projectFile = name => ['config.json', 'repos.conf'].includes(name) || name.startsWith('extensions/');
const ordered = value => Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)));
export const installationFingerprint = (files, projectFiles) => digest({ files: ordered(files), projectFiles: ordered(projectFiles) });

export async function installationFiles(directory) {
  const files = await packageFiles(directory), projectFiles = {};
  for (const [name, sha256] of Object.entries(files)) {
    if (projectFile(name)) projectFiles[name] = { sha256, mode: (await lstat(join(directory, name))).mode & 0o777 };
  }
  return { files, projectFiles, fingerprint: installationFingerprint(files, projectFiles) };
}

export function plannedInstallation(plan) {
  const files = Object.fromEntries(Object.entries(plan.files).map(([name, content]) => [name, digest(content)]));
  const projectFiles = Object.fromEntries(Object.entries(files).filter(([name]) => projectFile(name)).map(([name, sha256]) => [name, { sha256, mode: 0o600 }]));
  for (const [name, record] of Object.entries(plan.preserved || {})) {
    files[name] = record.sha256;
    projectFiles[name] = record;
  }
  return { files, projectFiles, fingerprint: installationFingerprint(files, projectFiles) };
}

export async function copyProjectFiles(source, destination, files) {
  for (const [name, expected] of Object.entries(files)) {
    if (!projectFile(name)) fail('harness_project_file_invalid', '只能保留项目配置、登记和 extensions 中的文件。');
    const from = join(source, name), to = join(destination, name);
    await safePath(source, from); await safePath(destination, to);
    const info = await lstat(from), content = await readFile(from);
    if (!info.isFile() || digest(content) !== expected.sha256 || (info.mode & 0o777) !== expected.mode) fail('harness_plan_changed', '项目文件内容或权限已变化；保留原文件，不继续覆盖。');
    await mkdir(dirname(to), { recursive: true, mode: 0o700 });
    await writeFile(to, content, { flag: 'wx', mode: expected.mode });
    await chmod(to, expected.mode);
    if (digest(await readFile(to)) !== expected.sha256 || ((await lstat(to)).mode & 0o777) !== expected.mode) fail('harness_readback', '保留的项目文件内容或权限回读不一致。');
  }
}
