import { execFileSync } from 'node:child_process';
import { devNull } from 'node:os';
import { performance } from 'node:perf_hooks';

const MAX_OBJECTS = 100000;
const MAX_SCAN_BYTES = 256 * 1024 * 1024;
const LARGE_BLOB_BYTES = 50 * 1024 * 1024;
const TOTAL_TIMEOUT_MS = 120000;
const patterns = [
  ['github_token', /\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b/],
  ['private_key', /-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----/],
  ['credential_url', /https?:\/\/[^\s/@:]+:[^\s/@]+@/i],
  ['aws_access_key', /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/],
];

function auditGit(project) {
  const env = { ...process.env, LC_ALL: 'C', GIT_TERMINAL_PROMPT: '0', GIT_NO_REPLACE_OBJECTS: '1', GIT_GRAFT_FILE: devNull };
  for (const key of Object.keys(env)) if (/^GIT_(DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|CONFIG|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|SHALLOW_FILE)/.test(key)) delete env[key];
  const deadline = performance.now() + TOTAL_TIMEOUT_MS;
  return (args, input) => {
    const remaining = Math.floor(deadline - performance.now());
    if (remaining <= 0) throw Object.assign(new Error('历史检查达到总时间预算，未完成检查，不能发布。'), { code: 'history_audit_timeout' });
    try { return execFileSync('git', ['-C', project, ...args], { input, encoding: 'utf8', timeout: Math.min(30000, remaining), maxBuffer: 64 * 1024 * 1024, env, stdio: ['pipe', 'pipe', 'pipe'] }); }
    catch { throw Object.assign(new Error('历史检查未完成；请核对引用、完整克隆及检查预算。'), { code: 'history_audit_failed' }); }
  };
}

function checkRef(git, ref) {
  if (typeof ref !== 'string' || !/^refs\/(heads|tags)\//.test(ref)) throw Object.assign(new Error('只接受完整 refs/heads/ 或 refs/tags/ 引用。'), { code: 'invalid_ref' });
  git(['check-ref-format', ref]);
}

/** Known-pattern audit across every reachable blob, including deleted historical files. */
export function auditHistory(project, refs) {
  if (!Array.isArray(refs) || !refs.length || new Set(refs).size !== refs.length) throw Object.assign(new Error('audit 需要不重复、明确的完整分支或标签引用。'), { code: 'refs_required' });
  const git = auditGit(project);
  const snapshot = {};
  for (const ref of refs) {
    checkRef(git, ref);
    snapshot[ref] = git(['rev-parse', '--verify', ref]).trim();
  }
  return scanSnapshot(git, snapshot);
}

/** Audit exact already-fetched source objects, never substitute local same-name refs. */
export function auditSnapshot(project, input) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || !Object.keys(input).length) throw Object.assign(new Error('快照检查需要完整引用到精确对象 ID 的非空映射。'), { code: 'invalid_snapshot' });
  const git = auditGit(project);
  const snapshot = {};
  for (const [ref, oid] of Object.entries(input)) {
    checkRef(git, ref);
    if (typeof oid !== 'string' || !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(oid)) throw Object.assign(new Error('快照只接受完整的十六进制 Git 对象 ID，不能使用引用或表达式。'), { code: 'invalid_snapshot' });
    const type = git(['cat-file', '-t', oid]).trim();
    if (!['commit', 'tree', 'blob', 'tag'].includes(type) || (ref.startsWith('refs/heads/') && type !== 'commit')) throw Object.assign(new Error('快照对象类型与 Git 引用不匹配。'), { code: 'invalid_snapshot' });
    snapshot[ref] = oid;
  }
  return scanSnapshot(git, snapshot);
}

function scanSnapshot(git, snapshot) {
  if (git(['rev-parse', '--is-shallow-repository']).trim() === 'true') return { status: 'incomplete', complete: false, reason: 'shallow_history', snapshot };
  const objects = git(['rev-list', '--objects', '--no-object-names', ...Object.values(snapshot), '--']).trim().split('\n').filter(Boolean);
  if (objects.length > MAX_OBJECTS) return { status: 'incomplete', complete: false, reason: 'object_budget', snapshot, objects: objects.length };
  const metadata = objects.length ? git(['cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'], `${objects.join('\n')}\n`).trim().split('\n') : [];
  const findings = []; let scannedBytes = 0; let scannedBlobs = 0; let skipped = 0;
  for (const line of metadata) {
    const [oid, type, sizeText] = line.split(' ');
    const size = Number(sizeText);
    if (type !== 'blob') continue;
    if (!Number.isSafeInteger(size)) { skipped++; continue; }
    if (size > LARGE_BLOB_BYTES) { findings.push({ oid, kind: 'large_blob', bytes: size }); skipped++; continue; }
    if (scannedBytes + size > MAX_SCAN_BYTES) { skipped++; continue; }
    const data = git(['cat-file', 'blob', oid]);
    for (const [kind, pattern] of patterns) if (pattern.test(data)) findings.push({ oid, kind });
    scannedBytes += size; scannedBlobs++;
  }
  return { status: skipped ? 'incomplete' : findings.length ? 'findings' : 'checked', complete: skipped === 0 && findings.length === 0, snapshot, scannedBlobs, scannedBytes, skippedBlobs: skipped, findings,
    limitation: '仅检查已知秘密模式与大文件；不证明不存在所有秘密。待提交文件须在首次提交前单独检查。' };
}
