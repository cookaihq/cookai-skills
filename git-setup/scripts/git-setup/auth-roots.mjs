import { join } from 'node:path';
import { resolveRepository } from './repository-relationship.mjs';
export { authFail, mainCheckout, localEnvironment, localGit } from './repository-facts.mjs';
export { authTarget } from './credential-data.mjs';

export async function credentialOwner(project, options = {}) {
  const relation = await resolveRepository(project, options);
  return { repository: relation.project, owner: { main: relation.management.main, commonDir: relation.management.commonDir }, directory: join(relation.management.main, '.credentials', 'repositories'), relationship: relation };
}
