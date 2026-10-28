import { join, resolve } from 'node:path';
import { inspect } from './git.mjs';
import { inspectAuth } from './auth.mjs';
import { readProjectPolicy, assertProjectTargets, repositoryPolicy, confirmedTargets } from './project-policy.mjs';
import { selectRepository } from './repository-relationship.mjs';
import { digest, fail } from './validation.mjs';

/** Explicit project policy is optional. Nothing is installed or written here. */
export async function projectCheck(root, options = {}) {
  const { directory, command = 'check', remote, repository = '.', checkout } = options;
  const policyFile = await readProjectPolicy(root, directory);
  const selection = await selectRepository(root, { repository, checkout, policyDirectory: directory });
  const local = await inspect(selection.project.root);
  if (!local.initialized || local.bare || local.insideParent || resolve(selection.project.root) !== local.root) fail('project_root_invalid', '操作检出必须是实际 Git 工作区根目录。');
  const config = policyFile.exists ? repositoryPolicy(policyFile.config, selection.key) : { targets: confirmedTargets(local), sync: options.sync || null, extensions: [] };
  const selected = { ...policyFile, config, fingerprint: digest(config) };
  const policy = { root, path: directory ? join(root, directory) : null, directory: directory || null };
  const names = command === 'check' ? Object.keys(config.targets) : command === 'sync' ? [config.sync?.source, ...(config.sync?.targets || [])].filter(Boolean) : [remote];
  if (names.some(name => !name)) fail('missing_remote', '请明确选择远端。');
  await assertProjectTargets(selection.project.root, config, names);
  const authentication = [];
  for (const name of names) {
    const target = local.remotes.find(item => item.name === name);
    const urls = command === 'read' ? target.fetch : command === 'publish' ? target.push : [...target.fetch, ...target.push];
    for (const url of new Set(urls)) authentication.push({ url, ...await inspectAuth(selection.project.root, url, { ...options, workspace: root, policyDirectory: directory }) });
  }
  return { status: 'checked', project: selection.project.root, policy, repository: selection.key, relationship: selection.relationship, local, selected, authentication,
    fingerprint: digest({ policy, relationship: selection.relationship.fingerprint, repository: selection.key, branch: local.branch, author: local.author, config: selected.fingerprint, targets: local.remotes.filter(item => names.includes(item.name)), authentication }),
    network: 'not_performed', actualPush: 'not_performed' };
}
