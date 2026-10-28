import test from 'node:test';
import assert from 'node:assert/strict';
import {realpath} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {validateSource} from '../scripts/git-setup/source-record.mjs';
import {safePath,publicText} from '../scripts/git-setup/validation.mjs';
import {authTarget} from '../scripts/git-setup/auth-roots.mjs';
const owner={main:await realpath(tmpdir())};
test('source selection is nonsecret, platform-specific and scoped to its owner',()=>{
 const entry={url:'https://cnb.cool/team/repo',method:'cnb-url',file:'.credentials/repo'};assert.equal(validateSource(entry,owner).method,'cnb-url');
 for(const bad of [{...entry,file:'../outside'}, {...entry,url:'https://cnb:synthetic-value@cnb.cool/team/repo'},{...entry,method:'gh',account:'user'},{...entry,password:'value'}])assert.throws(()=>validateSource(bad,owner));
 assert.equal(validateSource({url:'https://github.com/team/repo',method:'gh',account:'user'},owner).account,'user');
});
test('Windows same-directory casing is accepted but cross-drive paths cannot escape',async()=>{
 if(process.platform!=='win32')return;
 await safePath(owner.main,join(owner.main.toLowerCase(),'file'));
 const other=(owner.main[0].toUpperCase()==='Z'?'Y':'Z')+':/outside';
 await assert.rejects(safePath(owner.main,other),{code:'project_path_invalid'});
 assert.throws(()=>validateSource({url:'https://cnb.cool/team/repo',method:'cnb-url',file:other},owner),{code:'auth_source_invalid'});
});
test('different complete repository paths cannot share a default filename',()=>{
 assert.notEqual(authTarget('https://cnb.cool/a--b/c').filename,authTarget('https://cnb.cool/a/b--c').filename);
});
test('public instructions accept only a literal Token placeholder',()=>{
 publicText('https://cnb:<TOKEN>@cnb.cool/team/repo');assert.throws(()=>publicText('https://cnb:synthetic-token@cnb.cool/team/repo'),{code:'input_secret'});
});
