import test from 'node:test';
import assert from 'node:assert/strict';
import {Configuration} from '../assets/runtime/skill_ui/static/configuration.js';

globalThis.document={addEventListener(){},getElementById(){return null;}};
globalThis.window={addEventListener(){}};
globalThis.location={search:''};
function snapshot(){
  return {configuration_protocol:'field-scopes-v1',revision:'r1',default_targets:{global:'global',project:'project'},readiness:{missing:[]},validation_errors:[],
    layers:[{id:'project',scope:'project',name:'Project',path:'/project/.env',readonly:false},{id:'global',scope:'global',name:'Global',path:'/global/.env',readonly:false}],
    fields:[['a','saved-a','project'],['b','saved-b','global'],['empty','',null],['off',false,'project'],['zero',0,'project'],['list',[],'global']].map(([id,value,source])=>({id,config_key:'REAL_'+id,label:'Setting '+id,type:typeof value==='boolean'?'boolean':typeof value==='number'?'number':Array.isArray(value)?'list':'text',targets:['project','global'],edit_revision:id+'-1',effective:{value,source,empty:value==='',status:source?'set':'optional'},rows:[{layer:'project',present:source==='project',status:source==='project'?'effective':'missing',value:source==='project'?value:null},{layer:'global',present:source==='global',status:source==='global'?'effective':'missing',value:source==='global'?value:null}]}))};
}
function fixture(api=async()=>({config:snapshot()})){
  const ui=new Configuration({root:{addEventListener(){},querySelectorAll(){return[];}},api,render(){},reload(){},toast(){}});
  ui.paint=()=>{};ui.rerender=()=>{};ui.queuePreview=function(){this.invalidate();};ui.set(snapshot());return ui;
}
function ready(ui,ids=['a','b']){ui.preview={...snapshot(),field_results:Object.fromEntries(ids.map(id=>[id,{status:'ready'}]))};}
const click=(ui,config,id,layer)=>ui.onClick({target:{closest:()=>({dataset:{config,id,layer}})}});

test('empty defaults and typed false/zero/list retain their meaning',()=>{
  const ui=fixture();assert.equal(ui.fieldTarget('empty'),'global');assert.match(ui.fieldHTML(ui.field('empty')),/is-default/);
  for(const id of ['off','zero','list'])assert.equal(ui.field(id).effective.empty,false);
  const state=snapshot();state.presentation={initial_target:'project'};delete ui.snapshot;ui.set(state);
  assert.equal(ui.fieldTarget('empty'),'project');assert.equal(ui.fieldTarget('b'),'global');
});

test('scope cancellation preserves draft and target without writing',async()=>{
  const requests=[],state=snapshot();state.scope_plans={a:{target:'global',state:'choice',conflict:true,cleanup:['project'],choice_token:'c',cleanup_token:'d'}};
  const ui=fixture(async(path,request)=>{requests.push([path,request]);return{config:state};});
  ui.stage('a',{op:'set',value:'edited'});ui.confirmScope=async()=>null;
  const before=structuredClone(ui.payload());assert.equal(await ui.requestSwitch('a','global'),false);
  assert.deepEqual(ui.payload(),before);assert.equal(ui.fieldTarget('a'),'project');
  assert.deepEqual(requests.map(([path])=>path),['/api/preview']);
});

test('scope confirmation stages choice and cleanup; only bottom save writes',async()=>{
  const requests=[],state=snapshot();state.scope_plans={a:{target:'global',state:'choice',conflict:true,cleanup:['project'],choice_token:'c',cleanup_token:'d'}};
  const saved=snapshot();saved.fields[0].effective.source='global';saved.fields[0].edit_revision='a-2';saved.field_results={a:{status:'saved'}};
  const ui=fixture(async(path,request)=>{requests.push([path,request]);return{status:'saved',config:path==='/api/save'?saved:state};});
  ui.confirmScope=async()=>({content:'target'});
  assert.equal(await ui.requestSwitch('a','global'),true);assert.equal(ui.fieldTarget('a'),'global');
  assert.deepEqual(ui.switchIntents.a,{target:'global',mode:'input',choose_content:true,choice:'target',choice_token:'c',cleanup_token:'d'});
  assert.match(ui.selectionHTML('a',ui.switchIntents.a,state.scope_plans.a),/保存后将清除/);
  for(const text of ['REAL_a','/project/.env','Setting a'])assert.ok(ui.selectionHTML('a',ui.switchIntents.a,state.scope_plans.a).includes(text));
  await assert.rejects(ui.save('a'),/单项保存入口已移除/);assert.equal(requests.filter(([path])=>path==='/api/save').length,0);
  ready(ui,['a']);await ui.save();assert.equal(requests.filter(([path])=>path==='/api/save').length,1);assert.equal(ui.count(),0);
});

test('low to high stages without confirmation and cancel restores original target',async()=>{
  const ui=fixture();ui.stage('b',{op:'set',value:'edited'});ui.confirmScope=async()=>assert.fail('no cleanup requires confirmation');
  await ui.requestSwitch('b','project');assert.equal(ui.payload().files.project.b.value,'edited');
  await click(ui,'cancel-switch','b');assert.equal(ui.fieldTarget('b'),'global');assert.equal(ui.draftValue('b'),'edited');
});

test('partial save consumes success and preserves failed input and target',async()=>{
  const next=snapshot();next.revision='r2';next.fields[0].edit_revision='a-2';next.fields[0].effective.value='new-a';next.field_results={a:{status:'saved'},b:{status:'failed',message:'bad value'}};
  const checked=snapshot();checked.validation_errors=['bad value'];checked.field_results={a:{status:'ready'},b:{status:'failed'}};
  const ui=fixture(async(path)=>({status:'partial',config:path==='/api/save'?next:checked}));
  ui.stage('a',{op:'set',value:'new-a'});ui.stage('b',{op:'set',value:'bad-b'});ready(ui);
  assert.equal((await ui.save()).saved,true);assert.equal(ui.hasFieldDraft('a'),false);
  assert.equal(ui.draftValue('b'),'bad-b');assert.equal(ui.fieldTarget('b'),'global');assert.equal(ui.saveResults.b.status,'failed');
  ui.stage('b',{op:'set',value:'fixed'});assert.equal(ui.saveResults.b,undefined);
});

test('lost response uses original receipt without a second write',async()=>{
  const completed=snapshot();completed.field_results={a:{status:'saved'}};let saves=0,lookup;
  const ui=fixture(async(path,request)=>{if(path==='/api/save'){saves++;throw Error('lost response');}if(path==='/api/save-result'){lookup=request;return{status:'saved',config:completed};}return{config:snapshot()};});
  ui.stage('a',{op:'set',value:'keep'});ready(ui,['a']);await ui.save();
  assert.equal(saves,1);assert.equal(lookup.request_id,ui.saveRequestId);assert.equal(ui.count(),0);assert.equal(ui.uncertain,false);
});

test('unknown result blocks retries; expiry preserves failed drafts',async()=>{
  const projected=snapshot();projected.fields[0].edit_revision='a-2';
  let saves=0;const ui=fixture(async path=>{if(path==='/api/save'){saves++;throw Error('lost');}return{status:'unknown',config:path==='/api/preview'?projected:snapshot()};});
  ui.stage('a',{op:'set',value:'keep'});ready(ui,['a']);await ui.save();assert.equal(ui.uncertain,true);
  await ui.save();assert.equal(saves,1);assert.equal(ui.draftValue('a'),'keep');
  ui.set(snapshot());ui.expire();assert.equal(ui.draftValue('a'),'keep');assert.equal(ui.canSave(),false);
});

test('new input invalidates confirmations; unrelated reload preserves them',async()=>{
  const ui=fixture();await ui.requestSwitch('a','global');Object.assign(ui.switchIntents.a,{choice:'target',choice_token:'old',cleanup_token:'clean'});
  const next=snapshot();next.revision='r2';next.fields[1].edit_revision='b-2';ui.set(next);
  assert.equal(ui.switchIntents.a.choice,'target');ui.stage('a',{op:'set',value:'new'});
  assert.equal(ui.switchIntents.a.choice,undefined);assert.equal(ui.switchIntents.a.cleanup_token,undefined);
});

test('file removal cancellation preserves input and confirmation stages unset',async()=>{
  const paths=[],ui=fixture(async path=>{paths.push(path);return{config:snapshot()};});
  ui.stage('a',{op:'set',value:'old draft'});await ui.requestFileRemoval('a','project');ui.dismissFileRemoval();assert.equal(ui.draftValue('a'),'old draft');
  await ui.requestFileRemoval('a','project');await click(ui,'confirm-file-removal','a','project');
  assert.deepEqual(ui.payload().files.project.a,{op:'unset'});assert.ok(paths.every(path=>path==='/api/preview'));
});

test('directory previews bind full drafts and expire on edits',()=>{
  const ui=fixture();ui.stage('b',{op:'set',value:'edit'});ui.preview={...snapshot(),preview_tokens:{b:'candidate-1'}};
  assert.deepEqual(ui.payload().preview_tokens,{b:'candidate-1'});assert.equal(ui.payload('b').preview_tokens,undefined);
  ui.stage('b',{op:'set',value:'edit again'});assert.equal(ui.payload().preview_tokens,undefined);
});
