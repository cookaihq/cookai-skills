import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
import { readFile, writeFile } from 'node:fs/promises';
import { gitConfiguration } from '../scripts/git-setup/git-trust.mjs';
import { repositoryFixture, cli } from './fixtures/repositories.mjs';
import { gitService } from './fixtures/git-service.mjs';
import { issueInput, inputFile } from './fixtures/issue-input.mjs';

const exec = promisify(execFile);

test('Git retains concrete host trust while discarding inherited credentials, includes and wildcard exemptions', async () => {
  const f = await repositoryFixture(), project = await f.repo('trusted');
  const pairs = [['credential.helper','must-not-run'],['http.extraHeader','must-not-send'],['include.path',join(f.root,'must-not-read')],['safe.directory',''],['safe.directory','*'],['safe.directory',`${f.root}/*`],['safe.directory','relative'],['safe.directory',project]];
  const ambient = Object.assign({GIT_CONFIG_COUNT:String(pairs.length)},...pairs.map(([key,value],index)=>({[`GIT_CONFIG_KEY_${index}`]:key,[`GIT_CONFIG_VALUE_${index}`]:value})));
  const env = {...f.env,...gitConfiguration([['credential.helper',''],['credential.useHttpPath','true']],ambient)};
  const config = async key => (await exec('git',['-C',project,'config','--get-all',key],{env,encoding:'utf8',timeout:15000})).stdout;
  assert.equal(await config('safe.directory'),`\n${project}\n`);
  assert.equal((await config('credential.helper')).trim(),'');
  assert.equal((await config('credential.useHttpPath')).trim(),'true');
  for(const key of ['http.extraHeader','include.path']) await assert.rejects(config(key),error=>error.code===1);
  assert.deepEqual(gitConfiguration([],{...ambient,GIT_CONFIG_COUNT:'invalid'}),{});
});

test('public check, credential preparation and history audit work with narrowly scoped sandbox ownership trust', async t => {
  const f = await repositoryFixture(), project = await f.repo('sandbox-owned');
  const serviceFixture = {...f,env:{...f.env}};
  const service = await gitService(t,serviceFixture,{'acceptance/owned':'trust-fixture-token'});
  Object.assign(f.env,serviceFixture.env);
  f.env.GIT_TEST_ASSUME_DIFFERENT_OWNER='1';
  const check = () => f.run(cli,['check','--project',project]);
  assert.equal((await check()).status,'error');
  Object.assign(f.env,{GIT_CONFIG_COUNT:'1',GIT_CONFIG_KEY_0:'safe.directory',GIT_CONFIG_VALUE_0:project});
  const checked = await check();
  assert.equal(checked.status,'checked',JSON.stringify(checked));
  const prepared = await f.run(cli,['credential-prepare','--project',project,'--url','https://cnb.cool/acceptance/owned.git','--auth-method','cnb-url','--apply']);
  assert.ok(prepared.credentialFile,JSON.stringify(prepared));
  await writeFile(prepared.credentialFile,'https://cnb:trust-fixture-token@cnb.cool/acceptance/owned');
  const verified = await f.run(cli,['credential-check','--project',project,'--url','https://cnb.cool/acceptance/owned.git','--auth-method','cnb-url']);
  assert.equal(verified.status,'verified',JSON.stringify(verified));
  assert.ok(service.requests.some(request=>request.username==='cnb'));
  const audit = await f.run(cli,['audit','--project',project,'--ref','refs/heads/main']);
  assert.equal(audit.complete,true,JSON.stringify(audit));
  const planFile = join(project,'data/git-setup/sandbox-plan.json');
  const setup = await f.run(cli,['setup','--project',project,'--url','https://cnb.cool/acceptance/owned.git','--action','add','--name','origin','--auth-method','cnb-url','--plan-file',planFile]);
  assert.equal(setup.status,'planned',JSON.stringify(setup));
  assert.equal(JSON.parse(await readFile(planFile,'utf8')).result.fingerprint,setup.fingerprint);
  const input = await inputFile(f,await issueInput(project));
  const args = ['--project',project,'--input',input];
  const plan = await f.run(cli,['issue-resolve',...args]);
  assert.equal(plan.status,'planned',JSON.stringify(plan));
  const created = await f.run(cli,['issue-create',...args,'--apply','--expected-fingerprint',plan.fingerprint]);
  assert.equal(created.status,'verified',JSON.stringify(created));
  for(const key of ['safe.directory','credential.helper','http.extraHeader']) {
    assert.equal(await f.git(project,'config','--local','--get-all',key).catch(error=>error.code===1?'':Promise.reject(error)),'');
  }
});
