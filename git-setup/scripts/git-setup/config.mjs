import { canonical } from './repository-facts.mjs';
export { inspectProject } from './repository-facts.mjs';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const AUTH_KEYS = ['GIT_SETUP_GITHUB_AUTH', 'GIT_SETUP_GITHUB_TOKEN', 'GIT_SETUP_GITHUB_HOST', 'GIT_SETUP_GITHUB_REPOSITORY'];
const ENV_KEYS = AUTH_KEYS;
const nonempty = value => typeof value === 'string' && value.trim() !== '';
const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };

async function readOptional(file) {
  try { return await fs.readFile(file, 'utf8'); }
  catch (error) { if (error.code === 'ENOENT') return null; fail('CONFIG_UNREADABLE', `Cannot read configuration file ${file}.`); }
}

function parseEnv(text) {
  const values = {};
  for (const line of text.split(/\r?\n/)) {
    const match = /^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(line);
    if (!match || !ENV_KEYS.includes(match[1])) continue;
    let value = match[2];
    if (value.length >= 2 && ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'")))) value = value.slice(1, -1);
    values[match[1]] = value;
  }
  return values;
}

/** Internal values may contain secrets; expose sources/presence only in user-facing output. */
export async function loadEnvironment(project, options = {}) {
  const root = await canonical(project);
  const layers = [{ path: 'process', values: options.env ?? process.env }];
  const files = ['.env.git-setup', '.env.local', '.env'].map(name => path.join(root, name));
  if (options.useGlobalConfig) files.push(options.globalConfigPath ?? path.join(os.homedir(), '.config/git-setup/.env'));
  for (const file of files) {
    const raw = await readOptional(file);
    if (raw !== null) layers.push({ path: file, values: parseEnv(raw) });
  }
  const values = {}, sources = {};
  const auth = layers.find(layer => AUTH_KEYS.some(key => nonempty(layer.values[key])));
  if (auth) {
    for (const key of AUTH_KEYS) if (nonempty(auth.values[key])) { values[key] = auth.values[key]; sources[key] = auth.path; }
    const mode = values.GIT_SETUP_GITHUB_AUTH ?? (values.GIT_SETUP_GITHUB_TOKEN ? 'token' : null);
    if (!['token', 'gh'].includes(mode)) fail('CREDENTIAL_GROUP_INCOMPLETE', 'GitHub auth group must explicitly select gh or provide a token with its host and repository in the same source.');
    if (mode === 'token' && (!values.GIT_SETUP_GITHUB_TOKEN || !values.GIT_SETUP_GITHUB_HOST || !values.GIT_SETUP_GITHUB_REPOSITORY)) fail('CREDENTIAL_GROUP_INCOMPLETE', 'Token, GitHub host and repository must be nonempty in the same configuration source.');
    if (mode === 'gh' && values.GIT_SETUP_GITHUB_TOKEN) fail('CREDENTIAL_GROUP_CONFLICT', 'gh authentication and an explicit token cannot share one auth group.');
    values.GIT_SETUP_GITHUB_AUTH = mode;
    sources.GIT_SETUP_GITHUB_AUTH = auth.path;
    if (values.GIT_SETUP_GITHUB_HOST && !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(values.GIT_SETUP_GITHUB_HOST)) fail('CREDENTIAL_GROUP_INVALID', 'Configured GitHub host is not a hostname.');
    if (values.GIT_SETUP_GITHUB_REPOSITORY && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(values.GIT_SETUP_GITHUB_REPOSITORY)) fail('CREDENTIAL_GROUP_INVALID', 'Configured GitHub repository must be owner/name.');
  }
  return { values, sources };
}
