import test from 'node:test';
import assert from 'node:assert/strict';
import { parseCnbCredential } from '../scripts/git-setup/cnb-credential.mjs';
import { authTarget } from '../scripts/git-setup/auth-roots.mjs';
const target = authTarget('https://cnb.cool/team/sub/project.git');
const token = 'synthetic:token@with/slash+and%encoding';
const raw = 'https://cnb:'+encodeURIComponent(token)+'@cnb.cool/team/sub/project';
test('single authenticated URL decodes the Token and matches the full target',()=>{
  for(const value of [raw,raw+'\n','\uFEFF'+raw+'\r\n',raw+'.git/']) {
    const parsed=parseCnbCredential(value,target);assert.equal(parsed.username,'cnb');assert.equal(parsed.password,token);
  }
});
test('multiline, malformed and other-target credentials are rejected without values',()=>{
  for(const value of [target.url+'\nToken:'+token,raw+'\n\n',raw.replace('team/sub','team/other'),raw.replace('cnb:', 'user:'),raw+'?token='+token,raw.replace('team/sub','team/old/../sub'),'http'+raw.slice(5)]) {
    assert.throws(()=>parseCnbCredential(value,target),error=>{assert.equal(error.message.includes(token),false);assert.match(error.code,/^auth_file_/);return true;});
  }
});
test('rotation changes only the current operation digest',()=>{
  assert.notEqual(parseCnbCredential(raw,target).revision,parseCnbCredential(raw.replace(encodeURIComponent(token),'rotated'),target).revision);
});
