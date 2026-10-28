import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile,readFile,mkdir,lstat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { repositoryFixture } from './fixtures/repositories.mjs';
async function fixture(event='explicit'){
 const f=await repositoryFixture(),project=await f.repo('project');
 for(const name of ['origin','mirror']){const path=join(f.root,name+'.git');await f.git(f.root,'init','--bare','-q',path);await f.git(project,'remote','add',name,path);}
 const path='vcs/extensions/command.mjs',content="import{appendFileSync}from'node:fs';appendFileSync('data/git-setup/extension-ran','once\\n');\n";
 await mkdir(join(project,'vcs/extensions'),{recursive:true});await mkdir(join(project,'data/git-setup'),{recursive:true});await writeFile(join(project,path),content);
 const extension={id:'fixture',path,sha256:createHash('sha256').update(content).digest('hex'),event,description:'fixture extension',...(event==='explicit'?{}:{timeoutMs:5000})};
 await f.configure(project,{sync:{source:'origin',targets:['mirror'],refs:['refs/heads/main']},extensions:[extension]});return{...f,project,path,content};
}
test('explicit commands stay separate from ordinary publish and sync',async()=>{
 const f=await fixture();
 for(const args of [['publish','--remote','origin','--ref','refs/heads/main'],['sync']]){
  const plan=await f.command(f.project,args);assert.equal(plan.status,'planned',JSON.stringify(plan));const result=await f.command(f.project,[...args,'--apply','--expected-fingerprint',plan.fingerprint]);assert.equal(result.complete,true,JSON.stringify(result));assert.deepEqual(result.extensions,[]);
 }
 await assert.rejects(lstat(join(f.project,'data/git-setup/extension-ran')),{code:'ENOENT'});
});
test('automatic extension runs once after verified push and a changed extension stops before push',async()=>{
 const f=await fixture('after-publish'),args=['publish','--remote','origin','--ref','refs/heads/main','--operation-id','extension-case-001'];
 const plan=await f.command(f.project,args);await writeFile(join(f.project,f.path),f.content+'// changed\n');
 assert.equal((await f.command(f.project,[...args,'--apply','--expected-fingerprint',plan.fingerprint])).code,'project_extension_changed');
 await writeFile(join(f.project,f.path),f.content);const current=await f.command(f.project,args);assert.equal((await f.command(f.project,[...args,'--apply','--expected-fingerprint',current.fingerprint])).complete,true);
 assert.equal((await f.command(f.project,[...args,'--apply','--expected-fingerprint',current.fingerprint])).status,'previous_completed');assert.equal(await readFile(join(f.project,'data/git-setup/extension-ran'),'utf8'),'once\n');
});
