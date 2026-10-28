import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, symlink, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { repositoryFixture } from './fixtures/repositories.mjs';
import { prepareAuth, authSelection, verifySelection, verifyAuth, publicSelection, rememberSource, gitAuthentication, selectedGitHubClient } from '../scripts/git-setup/auth.mjs';
import { sourceRecordPath } from '../scripts/git-setup/source-record.mjs';
const exec=promisify(execFile);
const url='https://github.com/team/project.git';
const identity=async()=>({status:200,data:{login:'fixture-user',id:17},headers:{}});
async function fixture(){const f=await repositoryFixture();return {...f,project:await f.repo('project')};}
async function token(f){const p=await prepareAuth(f.project,url,{authMethod:'github-token',apply:true});await writeFile(p.credentialFile,'synthetic-only-token\n');return p;}
test('GitHub defaults to its project Token and a missing file is not created by reading',async()=>{
 const f=await fixture();const before=await f.git(f.project,'config','--local','--list');
 const selected=await authSelection(f.project,url);
 assert.deepEqual(selected.required,['credential-prepare']);
 assert.equal(selected.source.method,'github-token');
 assert.equal(selected.credentialFile,join(f.project,'.credentials/repositories/team--project.github-token'));
 await assert.rejects(readFile(selected.credentialFile),{code:'ENOENT'});
 assert.deepEqual((await authSelection(f.project,url,{authMethod:'github-token'})).required,['credential-prepare']);
 assert.equal(await f.git(f.project,'config','--local','--list'),before);
});

function globalGh() {
 const calls=[];
 const run=async(file,args)=>{
  assert.equal(file,'gh');calls.push(args);
  if(args[0]==='--version')return {stdout:'gh version 2.80.0\n'};
  if(args[1]==='status')return {stdout:JSON.stringify({hosts:{'github.com':[
   {login:'active-user',state:'success',active:true},{login:'fixture-user',state:'success',active:false}
  ]}})};
  assert.deepEqual(args,['auth','token','--hostname','github.com','--user','fixture-user']);
  return {stdout:'synthetic-gh-token\n'};
 };
 return {calls,run};
}

test('a configured project Token is verified without probing or reading available global gh accounts',async()=>{
 const f=await fixture(),p=await token(f),gh=globalGh();let requests=0;
 const verified=await verifyAuth(f.project,url,{run:gh.run,apiOnly:true,apiTransport:async request=>{
  requests++;assert.equal(request.headers.Authorization,'Bearer synthetic-only-token');return identity();
 }});
 assert.equal(verified.status,'verified');assert.equal(verified.source.method,'github-token');
 assert.equal(verified.source.file,p.credentialFile);assert.equal(verified.identity.login,'fixture-user');
 assert.equal(requests,1);assert.deepEqual(gh.calls,[]);
 assert.ok(!JSON.stringify(verified).includes('synthetic-only-token'));
 await assert.rejects(readFile(sourceRecordPath({main:f.project})),{code:'ENOENT'});
});

test('missing, empty, malformed and rejected project Tokens never fall back to available gh',async()=>{
 const f=await fixture(),gh=globalGh(),options={run:gh.run,apiOnly:true};
 assert.deepEqual((await verifyAuth(f.project,url,options)).required,['credential-prepare']);
 await assert.rejects(selectedGitHubClient(f.project,{host:'github.com',repository:'team/project'},options),error=>{
  assert.equal(error.code,'auth_selection_required');assert.ok(error.message.includes('team--project.github-token'));return true;
 });
 const p=await prepareAuth(f.project,url,{apply:true});
 assert.deepEqual((await verifyAuth(f.project,url,options)).required,['credentials-filled']);
 await writeFile(p.credentialFile,'invalid\nmultiple-lines');
 await assert.rejects(verifyAuth(f.project,url,options),{code:'auth_file_format'});
 await writeFile(p.credentialFile,'synthetic-rejected-token');
 for(const status of [401,403]){
  let requests=0;
  await assert.rejects(verifyAuth(f.project,url,{...options,apiTransport:async request=>{
   requests++;assert.equal(request.headers.Authorization,'Bearer synthetic-rejected-token');return {status,data:{},headers:{}};
  }}),error=>{
   assert.equal(error.code,'http-error');assert.ok(error.message.includes(p.credentialFile));return true;
  });
  assert.equal(requests,1);
 }
 assert.equal(await readFile(p.credentialFile,'utf8'),'synthetic-rejected-token');
 assert.deepEqual(gh.calls,[]);
});

test('project-defined custom Token sources are reused by selection and preparation without falling back',async()=>{
 const f=await fixture(),standard=await token(f),gh=globalGh();
 const p=await prepareAuth(f.project,url,{credentialFile:'.credentials/repositories/custom.github-token',apply:true});
 await writeFile(p.credentialFile,'synthetic-custom-token');
 const source={url,method:'github-token',file:p.credentialFile};
 const explicit=await authSelection(f.project,url,{sources:[source],run:gh.run});
 assert.equal(explicit.credential.password,'synthetic-custom-token');
 await rememberSource(f.project,source,{apply:true});
 assert.equal((await authSelection(f.project,url,{run:gh.run})).source.file,p.credentialFile);
 const prepared=await prepareAuth(f.project,url,{apply:true});
 assert.equal(prepared.credentialFile,p.credentialFile);assert.equal(prepared.created,false);
 await rm(p.credentialFile);
 const missing=await authSelection(f.project,url,{run:gh.run});
 assert.deepEqual(missing.required,['credential-prepare']);assert.equal(missing.credentialFile,p.credentialFile);
 assert.equal(await readFile(standard.credentialFile,'utf8'),'synthetic-only-token\n');assert.deepEqual(gh.calls,[]);
});

test('only an explicit gh choice or a saved choice for this target uses the named global account',async()=>{
 const f=await fixture();await token(f);const gh=globalGh();
 const pending=await authSelection(f.project,url,{authMethod:'gh',run:gh.run});
 assert.deepEqual(pending.required,['gh-user']);assert.equal(pending.accounts[0].active,true);
 assert.equal(gh.calls.some(args=>args[1]==='token'),false);
 const source={url,method:'gh',account:'fixture-user'};
 const selected=await authSelection(f.project,url,{sources:[source],run:gh.run});
 assert.equal(selected.source.account,'fixture-user');assert.equal(selected.credential.password,'synthetic-gh-token');
 await rememberSource(f.project,source,{apply:true});
 const saved=await authSelection(f.project,url,{run:gh.run});assert.equal(saved.source.account,'fixture-user');
 assert.deepEqual(gh.calls.at(-1),['auth','token','--hostname','github.com','--user','fixture-user']);
 const unrelated=await authSelection(f.project,'https://github.com/team/other',{run:gh.run});
 assert.equal(unrelated.source.method,'github-token');assert.deepEqual(unrelated.required,['credential-prepare']);
 const project=await authSelection(f.project,url,{authMethod:'github-token',run:gh.run});
 assert.equal(project.credential.password,'synthetic-only-token');
});
test('optional source record contains no secret or ready state and preserves prose',async()=>{
 const f=await fixture(),p=await token(f),entry={url,method:'github-token',file:p.credentialFile};
 await rememberSource(f.project,entry,{apply:true});
 const path=join(f.project,'.credentials/repositories/sources.local.md');
 await writeFile(path,'Personal explanation\n'+await readFile(path,'utf8'));
 await rememberSource(f.project,entry,{apply:true});
 const raw=await readFile(path,'utf8');assert.ok(raw.startsWith('Personal explanation'));assert.ok(!raw.includes('synthetic-only-token'));assert.ok(!raw.includes('credentialRevision'));
 assert.equal((await authSelection(f.project,url)).source.file,p.credentialFile);
 await assert.rejects(rememberSource(f.project,{...entry,url:'https://cnb:synthetic-secret@cnb.cool/team/project'}),{code:'input_secret'});
});
test('same-file rotation is valid next operation, invalid within an operation',async()=>{
 const f=await fixture(),p=await token(f),options={authMethod:'github-token',apiTransport:identity};
 const selected=await verifySelection(await authSelection(f.project,url,options),options);
 const transport=gitAuthentication(f.project,options,selected);
 await writeFile(p.credentialFile,'next-synthetic-token');
 await assert.rejects(transport.authForUrl(url),{code:'auth_source_changed'});
 const next=await verifySelection(await authSelection(f.project,url,options),options);
 assert.equal(next.identity.id,17);assert.notEqual(next.fingerprint,selected.fingerprint);
 assert.ok(!JSON.stringify(publicSelection(next)).includes('next-synthetic-token'));
 const config=await f.git(f.project,'config','--local','--list');assert.ok(!config.includes('gitsetup.auth-'));assert.ok(!config.includes('credential.helper'));
});
test('credential preparation preserves existing content and refuses tracked or unignored files',async()=>{
 const f=await fixture(),p=await token(f);await prepareAuth(f.project,url,{authMethod:'github-token',apply:true});assert.equal(await readFile(p.credentialFile,'utf8'),'synthetic-only-token\n');
 await writeFile(join(f.project,'tracked'),'fixture');await f.git(f.project,'add','tracked');
 await assert.rejects(prepareAuth(f.project,url,{authMethod:'github-token',credentialFile:join(f.project,'tracked'),apply:true}),{code:'auth_file_tracked'});
 await assert.rejects(prepareAuth(f.project,url,{authMethod:'github-token',credentialFile:join(f.project,'unignored'),apply:true}),{code:'auth_file_not_ignored'});
});
test('get-only transport refuses a different target and leaves source/config untouched on store or erase',async()=>{
 const f=await fixture(),p=await token(f),options={authMethod:'github-token',apiTransport:identity};
 const transport=await gitAuthentication(f.project,options).authForUrl(url);
 const before=await readFile(p.credentialFile,'utf8'),config=await f.git(f.project,'config','--local','--list');
 const helper=new URL('../scripts/git-setup/credential-helper.mjs',import.meta.url);
 for(const operation of ['store','erase']) await exec(process.execPath,[helper.pathname.replace(/^\/(.:)/,'$1'),'--transport',operation],{env:{...f.env,...transport.env},timeout:10000});
 assert.equal(await readFile(p.credentialFile,'utf8'),before);assert.equal(await f.git(f.project,'config','--local','--list'),config);
 assert.equal(transport.env.GIT_CONFIG_COUNT,'8');
});
test('URL-specific extra auth headers cannot override the chosen source',async()=>{
 const f=await fixture();await token(f);await f.git(f.project,'config','http.https://github.com/.extraHeader','Authorization: fixture-conflict');
 await assert.rejects(gitAuthentication(f.project,{authMethod:'github-token',apiTransport:identity}).authForUrl(url),{code:'auth_http_header_conflict'});
});
test('gh uses the named account without adopting active account or environment Token',async()=>{
 const f=await fixture();const calls=[];
 const options={authMethod:'gh',ghUser:'fixture-user',apiTransport:identity,run:async(file,args)=>{calls.push(args);return {stdout:'synthetic-gh-token\n'};}};
 const selected=await verifySelection(await authSelection(f.project,url,options),options);
 assert.equal(selected.identity.login,'fixture-user');assert.deepEqual(calls[0],['auth','token','--hostname','github.com','--user','fixture-user']);
});
