import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, symlink, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { repositoryFixture } from './fixtures/repositories.mjs';
import { prepareAuth, authSelection, verifySelection, publicSelection, rememberSource, gitAuthentication } from '../scripts/git-setup/auth.mjs';
import { sourceRecordPath } from '../scripts/git-setup/source-record.mjs';
const exec=promisify(execFile);
const url='https://github.com/team/project.git';
const identity=async()=>({status:200,data:{login:'fixture-user',id:17},headers:{}});
async function fixture(){const f=await repositoryFixture();return {...f,project:await f.repo('project')};}
async function token(f){const p=await prepareAuth(f.project,url,{authMethod:'github-token',apply:true});await writeFile(p.credentialFile,'synthetic-only-token\n');return p;}
test('GitHub requires a source choice and a missing file is not created by reading',async()=>{
 const f=await fixture();const before=await f.git(f.project,'config','--local','--list');
 assert.deepEqual((await authSelection(f.project,url)).required,['auth-method']);
 assert.deepEqual((await authSelection(f.project,url,{authMethod:'github-token'})).required,['credential-prepare']);
 assert.equal(await f.git(f.project,'config','--local','--list'),before);
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
