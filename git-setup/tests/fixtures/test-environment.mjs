import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname, delimiter } from 'node:path';
import { execFileSync } from 'node:child_process';

// All changes are confined to the test root and the child process environment.
// A closed proxy makes a missing local fixture fail without contacting a real host.
export function testEnvironment(root) {
  const home = join(root, 'home');
  mkdirSync(home, { recursive: true });
  writeFileSync(join(home, '.gitconfig'), '[core]\n\tautocrlf = false\n[credential]\n\thelper =\n');
  const env = { ...process.env, HOME: home, USERPROFILE: home, XDG_CONFIG_HOME: join(home, '.config'),
    GIT_SETUP_TEST_ROOT: root, GIT_TERMINAL_PROMPT: '0', GCM_INTERACTIVE: 'never',
    HTTPS_PROXY: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1',
    HTTP_PROXY: 'http://127.0.0.1:1', http_proxy: 'http://127.0.0.1:1',
    ALL_PROXY: '', all_proxy: '', NO_PROXY: '', no_proxy: '' };
  for (const key of Object.keys(env)) {
    if (/^(?:GH_TOKEN|GITHUB_TOKEN|GIT_CONFIG.*|GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_INDEX_FILE|GIT_CEILING_DIRECTORIES|GIT_ASKPASS|SSH_ASKPASS|NODE_OPTIONS)$/.test(key)
        || (key.startsWith('GIT_SETUP_') && key !== 'GIT_SETUP_TEST_ROOT')) delete env[key];
  }
  env.GIT_CONFIG_NOSYSTEM = '1';
  if (process.platform === 'win32') {
    const executable = execFileSync('where.exe', ['git'], { encoding: 'utf8', timeout: 10000 }).trim().split(/\r?\n/)[0];
    const installation = dirname(dirname(executable));
    const bins = ['cmd', 'bin', 'usr/bin'].map(part => join(installation, part)).filter(existsSync);
    const pathKey = Object.keys(env).find(key => key.toLowerCase() === 'path') || 'PATH';
    env[pathKey] = [...bins, env[pathKey]].join(delimiter);
  }
  return env;
}
