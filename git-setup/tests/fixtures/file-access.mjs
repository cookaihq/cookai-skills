import { chmod, stat } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';

const exec = promisify(execFile);

// Exercise a real denied read under the current test identity, then restore it.
export async function withUnreadableFile(file, check) {
  if (process.platform === 'win32') {
    const native = name => join(process.env.SystemRoot, 'System32', name + '.exe');
    const identity = (await exec(native('whoami'), ['/user', '/fo', 'csv', '/nh'], { timeout: 10000 })).stdout.match(/S-1-[0-9-]+/)?.[0];
    if (!identity) throw new Error('Cannot determine the test identity for the file access fixture');
    await exec(native('icacls'), [file, '/deny', `*${identity}:(R)`], { timeout: 10000 });
    try { return await check(); }
    finally { await exec(native('icacls'), [file, '/remove:d', `*${identity}`], { timeout: 10000 }); }
  }
  const mode = (await stat(file)).mode & 0o777;
  await chmod(file, 0);
  try { return await check(); }
  finally { await chmod(file, mode); }
}
