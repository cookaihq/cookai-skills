import test from 'node:test';
import assert from 'node:assert/strict';
import {writeFile,readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {repositoryFixture} from './fixtures/repositories.mjs';
async function fixture(){
 const f=await repositoryFixture(),manager=await f.repo('manager'),child=await f.repo('manager/api'),sibling=await f.repo('manager/web');
 const origin=join(f.root,'origin.git'),mirror=join(f.root,'mirror.git');for(const p of [origin,mirror])await f.git(f.root,'init','--bare','-q',p);
 await f.git(child,'remote','add','origin',origin);await f.git(child,'remote','add','mirror',mirror);await f.configure(manager);await writeFile(join(manager,'vcs/repos.conf'),'api\nweb\n');await f.configure(manager,{repositories:{api:{sync:{source:'origin',targets:['mirror'],refs:['refs/heads/main']}}}});return{...f,manager,child,sibling,origin,mirror};
}
test('explicit selection publishes and syncs only the chosen repository',async()=>{
 const f=await fixture(),before=await f.git(f.sibling,'status','--porcelain');
 for(const args of [['publish','--remote','origin','--ref','refs/heads/main'],['sync']]){
  const selected=[...args,'--repository','api'];const plan=await f.command(f.manager,selected);assert.equal(plan.status,'planned',JSON.stringify(plan));
  assert.equal((await f.command(f.manager,[...selected,'--apply','--expected-fingerprint',plan.fingerprint])).complete,true);
 }
 assert.equal(await f.git(f.origin,'rev-parse','main'),await f.git(f.child,'rev-parse','main'));assert.equal(await f.git(f.mirror,'rev-parse','main'),await f.git(f.child,'rev-parse','main'));assert.equal(await f.git(f.sibling,'status','--porcelain'),before);
});
test('a selected Worktree must belong to the registered repository',async()=>{
 const f=await fixture(),checkout=join(f.root,'linked');await f.git(f.child,'worktree','add','-b','feature',checkout);
 const check=await f.command(f.manager,['check','--repository','api','--checkout',checkout]);assert.equal(check.project,checkout,JSON.stringify(check));assert.equal(check.relationship.management.root,f.manager);
 assert.equal((await f.command(f.manager,['check','--repository','api','--checkout',f.sibling])).code,'WORKSPACE_CHECKOUT_MISMATCH');
});
test('changed registration, policy, remote and refs invalidate a publication plan',async()=>{
 const f=await fixture(),args=['publish','--repository','api','--remote','origin','--ref','refs/heads/main'];const plan=await f.command(f.manager,args);
 await writeFile(join(f.manager,'vcs/repos.conf'),'web\n');assert.equal((await f.command(f.manager,[...args,'--apply','--expected-fingerprint',plan.fingerprint])).code,'WORKSPACE_UNREGISTERED');
 await writeFile(join(f.manager,'vcs/repos.conf'),'api\nweb\n');await f.git(f.child,'remote','set-url','origin',f.mirror);assert.equal((await f.command(f.manager,args)).code,'project_target_changed');
 assert.equal(await f.git(f.origin,'for-each-ref'),'');assert.equal(await f.git(f.mirror,'for-each-ref'),'');
});
