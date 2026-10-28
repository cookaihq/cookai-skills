import { resolveIssueInput } from './issue-rules.mjs';
import { createLocalIssue, createRemoteIssue, recoverIssue, editLocalIssue, previewLocalEdit, updateRemoteIssue, commentRemoteIssue } from './issues.mjs';
import { selectedGitHubClient } from './auth.mjs';
import { digest } from './validation.mjs';

const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
export async function issueOperation(project, command, input, options = {}) {
  const prepare = async () => {
    const resolution = await resolveIssueInput(project, input);
    const client = resolution.backend === 'github' ? await selectedGitHubClient(resolution.targetProject, resolution.github, options) : undefined;
    if (client) {
      resolution.authentication = client.authentication;
      resolution.fingerprint = digest({ input: resolution.fingerprint, authentication: client.authentication.fingerprint });
    }
    return { resolution, client };
  };
  const { resolution, client } = await prepare();
  if (!['issue-resolve', 'issue-recover', `issue-${input.action}`].includes(command)) fail('issue_action_mismatch', '命令与本次输入的动作不同。');
  const validate = async () => {
    const { resolution: current } = await prepare();
    if (current.fingerprint !== resolution.fingerprint) fail('issue_plan_changed', '规则、关系或目标发生变化；未继续原操作。');
  };
  const checked = async operation => {
    const result = await operation;
    try { await validate(); }
    catch (error) {
      return {
        ...result, status: 'review_required', complete: false, retryAllowed: false,
        knownResult: result, finalCheck: { status: 'failed', code: error.code || 'unexpected_error' },
        message: '已取得操作结果，但返回前的依据复核失败；保留原操作编号和目标，核对实际状态，不重发写入。',
      };
    }
    return result;
  };
  if (command === 'issue-recover') return checked(recoverIssue({ resolution, operationId: input.operationId, client, validate }));
  if (command === 'issue-resolve' || !options.apply) {
    if (resolution.backend === 'local' && input.action !== 'create') await previewLocalEdit(resolution, input.content);
    return { ...resolution, status: 'planned', actualWrites: 'not_performed', agentInterpretation: 'supplied_by_caller; not_verified_by_script' };
  }
  if (options.expectedFingerprint !== resolution.fingerprint) fail('issue_plan_changed', '缺少本次计划指纹或输入已变化，未写入。');
  await validate();
  const operation = { resolution, content: input.content, body: input.content?.body, number: resolution.existing?.number, operationId: input.operationId, client, authorized: true, validate };
  if (input.action === 'create') return checked((resolution.backend === 'local' ? createLocalIssue : createRemoteIssue)(operation));
  if (resolution.backend === 'local') return checked(editLocalIssue(operation));
  return checked((input.action === 'update' ? updateRemoteIssue : commentRemoteIssue)(operation));
}
