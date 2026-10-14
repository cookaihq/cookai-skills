import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export const sha256 = text => createHash('sha256').update(text).digest('hex');
export async function issueInput(project) {
  const rule = 'Use local tickets, numbered NN-slug.md. Start at 01. Keep Status: open and append replies under Comments.\n';
  const template = '# {number} {title}\n\nStatus: open\n\n{body}\n\n## Comments\n';
  await mkdir(join(project, 'docs/agents'), { recursive: true });
  await writeFile(join(project, 'docs/agents/issue-tracker.md'), rule);
  await writeFile(join(project, 'docs/agents/issue-template.md'), template);
  return {
    schemaVersion: 1, action: 'create', operationId: randomUUID(), initiatingProject: project,
    relationship: { workspace: project, kind: 'self', path: '.' },
    rules: { project, path: 'docs/agents/issue-tracker.md', sha256: sha256(rule), references: [], initialization: { mode: 'initialize-if-missing' } },
    target: { scope: 'current', project, backend: 'local', local: { directory: 'tickets', filename: '{number}-{slug}.md', numbering: { start: 1, width: 2 }, template: { path: 'docs/agents/issue-template.md', sha256: sha256(template) }, commentsHeading: '## Comments' } },
    decision: { basis: 'Follow the existing tracker and template.', override: null },
    content: { title: 'Preserve the project rules', body: 'The created ticket must retain its status and location.' },
  };
}
export async function inputFile(f, input) {
  const file = join(f.root, `issue-input-${randomUUID()}.json`);
  await writeFile(file, JSON.stringify(input));
  return file;
}
export const evidence = async (root, path) => ({ path, sha256: sha256(await readFile(join(root, path))) });
