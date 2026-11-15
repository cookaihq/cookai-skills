import test from 'node:test';
import assert from 'node:assert/strict';
import {Configuration} from '../assets/runtime/skill_ui/static/configuration.js';

globalThis.document={addEventListener(){},getElementById(){return null;}};
globalThis.window={addEventListener(){}};
globalThis.location={search:''};
function snapshot(){
  return {configuration_protocol:'field-scopes-v1',revision:'r1',default_targets:{global:'global',project:'project'},readiness:{missing:[]},validation_errors:[],
    layers:[{id:'project',scope:'project',name:'Project',path:'/project/.env',readonly:false},{id:'global',scope:'global',name:'Global',path:'/global/.env',readonly:false}],
    fields:[['a','saved-a','project'],['b','saved-b','global'],['empty','',null],['off',false,'project'],['zero',0,'project'],['list',[],'global']].map(([id,value,source])=>({id,label:id,type:typeof value==='boolean'?'boolean':typeof value==='number'?'number':Array.isArray(value)?'list':'text',targets:['project','global'],edit_revision:id+'-1',effective:{value,source,empty:value==='',status:source?'set':'optional'},rows:[{layer:'project',present:source==='project',status:source==='project'?'effective':'missing',value:source==='project'?value:null},{layer:'global',present:source==='global',status:source==='global'?'effective':'missing',value:source==='global'?value:null}]}))};
}
function fixture(api=async()=>({})){const ui=new Configuration({root:{addEventListener(){},querySelectorAll(){return[];}},api,render(){},reload(){},toast(){}});ui.paint=()=>{};ui.rerender=()=>{};ui.queuePreview=function(){this.invalidate();};ui.set(snapshot());return ui;}

test('empty uses allowed global default without claiming file source; typed values are present',()=>{
  const ui=fixture();assert.equal(ui.fieldTarget('empty'),'global');
  const html=ui.fieldHTML(ui.field('empty'));assert.match(html,/scope-block [^\"]*is-default/);assert.doesNotMatch(html,/aria-label="当前生效"/);
  for(const id of ['off','zero','list'])assert.equal(ui.field(id).effective.empty,false);
  const sole=snapshot();sole.fields[2].targets=['project'];ui.set(sole);assert.equal(ui.fieldTarget('empty'),'project');
});
test('inspection and clearing a project input do not move its draft target',()=>{
  const ui=fixture();ui.stage('a',{op:'set',value:'edited'});ui.selectFieldTarget('a','global');
  assert.equal(ui.fieldTarget('a'),'project');assert.equal(ui.payload().files.project.a.value,'edited');assert.deepEqual(ui.payload().switches,{});
  ui.stage('a',{op:'set',value:''});assert.equal(ui.fieldTarget('a'),'project');
});

test('an entry default applies only when the field has no writable saved source',()=>{
  const ui=fixture(),state=snapshot();state.presentation={initial_target:'project'};
  delete ui.snapshot;ui.set(state);
  ui.stage('b',{op:'set',value:'edited-global'});
  assert.equal(ui.fieldTarget('b'),'global');
  assert.equal(ui.payload().files.global.b.value,'edited-global');
  assert.equal(ui.fieldTarget('empty'),'project');
});
test('choices expire on new input, destination or snapshot revision',()=>{
  const ui=fixture();ui.requestSwitch('a','global');Object.assign(ui.switchIntents.a,{choice:'target',choice_token:'old'});
  ui.stage('a',{op:'set',value:'new'});assert.equal(ui.switchIntents.a.choice,undefined);
  Object.assign(ui.switchIntents.a,{choice:'target',choice_token:'old'});ui.requestSwitch('a','project');assert.equal(ui.switchIntents.a,undefined);
  ui.requestSwitch('a','global');Object.assign(ui.switchIntents.a,{choice:'target',choice_token:'old'});const next=snapshot();next.revision='r2';ui.set(next);assert.equal(ui.switchIntents.a.choice,undefined);
});
test('single save consumes only its fields, preserving and rechecking other drafts',async()=>{
  const next=snapshot();next.revision='r2';next.fields[0].effective.value='new-a';next.fields[0].edit_revision='a-2';next.fields[1].effective.value='external-b';next.fields[1].edit_revision='b-2';
  const requests=[];const ui=fixture(async(path,request)=>{requests.push([path,request]);return{config:path==='/api/save'?next:snapshot()};});
  ui.stage('a',{op:'set',value:'new-a'});ui.stage('b',{op:'set',value:'my-b'});
  const result=await ui.save('a');assert.equal(result.saved,true);assert.equal(ui.hasFieldDraft('a'),false);assert.equal(ui.draftValue('b'),'my-b');assert.equal(ui.fieldTarget('b'),'global');assert.equal(ui.needsReview.b,true);
  for(const [,request]of requests)assert.deepEqual(Object.keys(request.changes.files),['project']);
});
test('unknown save result blocks retry until reread; expiry preserves typed input',async()=>{
  let saves=0;const ui=fixture(async(path)=>{if(path==='/api/save'){saves++;throw Error('lost response');}return{config:snapshot()};});
  ui.stage('a',{op:'set',value:'keep-this'});await ui.save('a');assert.equal(ui.uncertain,true);await ui.save('a');assert.equal(saves,1);assert.equal(ui.draftValue('a'),'keep-this');
  ui.set(snapshot());assert.equal(ui.uncertain,false);ui.expire();assert.equal(ui.draftValue('a'),'keep-this');assert.equal(ui.canSave(),false);await ui.save('a');assert.equal(saves,1);
});

test('rereading a completed pure switch consumes its lost-response intent',async()=>{
  const projected=snapshot();projected.fields[0].effective={value:'target-b',source:'global',empty:false};projected.fields[0].edit_revision='a-global-b';
  let saves=0;
  const ui=fixture(async path=>{if(path==='/api/save'){saves++;throw Error('lost response');}return{config:projected};});
  ui.requestSwitch('a','global','existing');await ui.save('a');
  assert.equal(ui.uncertain,true);assert.equal(saves,1);
  const reread=structuredClone(projected);reread.revision='r2';ui.set(reread);
  assert.equal(ui.count(),0);assert.equal(ui.hasFieldDraft('a'),false);
  assert.equal(ui.needsReview.a,undefined);assert.deepEqual(ui.payload().switches,{});
});

test('a switch plan names every cleaned file and its shared impact before applying',async()=>{
  const state=snapshot();state.layers[0].scope_label='Project shared impact';
  state.scope_plans={a:{target:'global',state:'ready',cleanup:['project'],message:'Switch is ready'}};
  let inspected;const response=new Promise(resolve=>{inspected=resolve;});
  const ui=fixture(async()=>{inspected();return{config:state};});
  ui.set(state);ui.queuePreview=Configuration.prototype.queuePreview;
  ui.requestSwitch('a','global','existing');await response;await Promise.resolve();
  const html=ui.selectionHTML('a');
  assert.match(html,/\/project\/\.env/);assert.match(html,/Project shared impact/);
  assert.match(html,/不会.*恢复/);
});
