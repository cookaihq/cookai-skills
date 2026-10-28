import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile, lstat, symlink, rm, mkdtemp, mkdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { cli } from './fixtures/repositories.mjs';
import { planDocument,writeDocument } from '../scripts/git-setup/project-document.mjs';
import { tmpdir } from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const exec=promisify(execFile),roots=[];
const base=resolve(process.env.GIT_SETUP_TEST_ROOT || tmpdir());
async function repositoryFixture(){
 await mkdir(base,{recursive:true});
 const root=await mkdtemp(join(base,'git-setup-doc-test-'));roots.push(root);
 // Keep the non-Git fixture from discovering the enclosing development repository.
 await writeFile(join(root,'.git'),`gitdir: ${join(root,'uninitialized.git').replaceAll('\\','/')}\n`);
 const repo=async name=>{const path=join(root,name);await mkdir(path);return path;};
 const run=async(entry,args)=>{try{return JSON.parse((await exec(process.execPath,[entry,...args],{timeout:30000})).stdout);}catch(error){return JSON.parse(error.stderr);}};
 return {root,repo,run};
}
after(async()=>{for(const root of roots){if(!resolve(root).startsWith(join(base,'git-setup-doc-test-')))throw new Error('Unsafe cleanup');await rm(root,{recursive:true});}});
const input={content:'# Project Git\n\nUse the current credential file; no persistent helper.\n',ruleFiles:['AGENTS.md']};
test('project document links preserve existing rules and create no runtime or binding',async()=>{
 const f=await repositoryFixture(),p=await f.repo('project');await writeFile(join(p,'AGENTS.md'),'# Existing policy\nDo not discard this.\n');
 const plan=await planDocument(p,input);
 const done=await writeDocument(p,input,{apply:true,expectedFingerprint:plan.fingerprint});assert.equal(done.complete,true);
 const rules=await readFile(join(p,'AGENTS.md'),'utf8');assert.ok(rules.includes('Do not discard this.'));assert.ok(rules.includes('docs/agent/git.md'));assert.equal(await readFile(join(p,'docs/agent/git.md'),'utf8'),input.content);

 for(const name of ['run.mjs','harness','runtime','.git/git-setup'])await assert.rejects(lstat(join(p,name)),{code:'ENOENT'});
 const next=await planDocument(p,input);await writeDocument(p,input,{apply:true,expectedFingerprint:next.fingerprint});assert.equal(await readFile(join(p,'AGENTS.md'),'utf8'),rules);
});
test('multiple host rule links edit one entity and retain links',async()=>{
 const f=await repositoryFixture(),p=await f.repo('project');await writeFile(join(p,'AGENTS.md'),'# Shared rule\n');await symlink('AGENTS.md',join(p,'CLAUDE.md'),'file');
 const request={...input,ruleFiles:['AGENTS.md','CLAUDE.md']};const plan=await planDocument(p,request);assert.equal(plan.edits.length,1);
 assert.equal((await writeDocument(p,request,{apply:true,expectedFingerprint:plan.fingerprint})).complete,true);assert.equal((await lstat(join(p,'CLAUDE.md'))).isSymbolicLink(),true);
});
test('stale document plans preserve subsequent user changes and link retargets',async()=>{
 const f=await repositoryFixture(),p=await f.repo('project');await writeFile(join(p,'AGENTS.md'),'# Original\n');const plan=await planDocument(p,input);
 await writeFile(join(p,'AGENTS.md'),'# User change\n');await assert.rejects(writeDocument(p,input,{apply:true,expectedFingerprint:plan.fingerprint}),{code:'document_changed'});assert.equal(await readFile(join(p,'AGENTS.md'),'utf8'),'# User change\n');
});
test('document input rejects secrets and traversal before any write',async()=>{
 const f=await repositoryFixture(),p=await f.repo('project');
 await assert.rejects(planDocument(p,{...input,content:'https://cnb:synthetic-secret@cnb.cool/team/repo'}),{code:'input_secret'});
 await assert.rejects(planDocument(p,{...input,path:'../outside.md'}),{code:'input_path_invalid'});
});
test('old installation commands and flags have no forwarding aliases',async()=>{
 const f=await repositoryFixture(),p=await f.repo('project');
 for(const command of ['harness-plan','harness-apply','harness-recover','auth-prepare','auth-verify','auth-cleanup','runtime-info'])assert.equal((await f.run(cli,[command,'--project',p])).code,'unknown_command');
 assert.equal((await f.run(cli,['check','--project',p,'--harness-directory','vcs'])).code,'invalid_argument');
});
