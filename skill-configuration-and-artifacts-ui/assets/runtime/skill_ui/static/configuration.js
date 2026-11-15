const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const scopeNames = {global:'全局', project:'此项目'};
const sourceStatuses = {effective:'当前使用', shadowed:'此项目未使用这份设置', empty:'未填写，使用其他位置的设置', missing:'这里还没有设置这一项', skipped:'本次未使用', error:'暂时无法读取'};
const includesField = (only,id) => !only || (Array.isArray(only)?only.includes(id):only===id);

export class Configuration {
  constructor({root,api,render,reload,toast}) {
    Object.assign(this,{root,api,render,reload,toast});
    this.drafts={};this.inputs={};this.errors={};this.revealed={};this.listeners=new Set();
    this.visible=new Set();this.details=new Set();this.generation=0;this.busy=false;
    this.preview=null;this.message='';this.error='';
    root.addEventListener('click',event=>this.onClick(event));
    root.addEventListener('input',event=>this.onInput(event));
    root.addEventListener('compositionstart',()=>{this.composing=true;clearTimeout(this.timer);});
    root.addEventListener('compositionend',event=>{this.composing=false;this.onInput(event);});
    root.addEventListener('submit',event=>{if(event.target.id==='configuration-form'){event.preventDefault();this.save();}});
    root.addEventListener('toggle',event=>{const id=event.target.dataset.configDetail;if(id&&event.target.isConnected)event.target.open?this.details.add(id):this.details.delete(id);},true);
    document.addEventListener('pointerdown',event=>{if(this.fileRemoval&&!event.target.closest('.file-delete-control'))this.dismissFileRemoval();});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&this.fileRemoval){event.preventDefault();this.dismissFileRemoval(true);}});
    window.addEventListener('resize',()=>positionFileRemoval.call(this));
    document.addEventListener('scroll',()=>positionFileRemoval.call(this),true);
  }
  fields(){return this.snapshot.fields;}
  field(id){return this.fields().find(f=>f.id===id);}
  layer(id){return this.snapshot.layers.find(l=>l.id===id);}
  row(f,target){return f.rows.find(r=>r.layer===target)||{};}
  after(f){return this.preview?.fields.find(n=>n.id===f.id)||f;}
  missing(){return (this.preview||this.snapshot).readiness?.missing||[];}
  targets(f){return this.snapshot.layers.filter(l=>!l.readonly&&l.enabled!==false&&!f.readonly&&f.targets?.includes(l.id));}
  source(row){return row.source==='default'?'默认设置':this.layer(row.source)?.name||'尚未设置';}
  set(snapshot){
    if(snapshot.configuration_protocol!=='field-scopes-v1')throw Error('配置适配器需要与共用核心一起迁移到逐项范围协议。');
    const first=!this.snapshot;this.snapshot=snapshot;
    this.invalidate();this.revealed={};this.error='';this.message='';
    if(first){
      const params=new URLSearchParams(location.search),presentation=snapshot.presentation||{};
      const requested=params.has('fields')?params.get('fields').split(',').map(id=>id.trim()):presentation.focus||[];
      this.focus=requested.filter(id=>this.field(id));
      if(!this.focus.length&&!this.missing().length&&!params.has('fields'))this.focus=this.fields().filter(f=>!f.advanced&&f.active!==false).slice(0,3).map(f=>f.id);
      this.focus.forEach(id=>this.visible.add(id));
      this.initialTarget=params.get('target')||presentation.initial_target;
    }
    this.rememberMissing();
  }
  rememberMissing(){this.missing().forEach(id=>this.visible.add(id));}
  invalidate(){clearTimeout(this.timer);this.generation++;this.preview=null;}
  detail(id,title,body,className=''){return `<details class="${className}" data-config-detail="${esc(id)}" ${this.details.has(id)?'open':''}><summary>${title}</summary>${body}</details>`;}
  afterRender(){this.paint();}
  rerender(){
    const active=document.activeElement,id=active?.id,start=active?.selectionStart,end=active?.selectionEnd;
    this.render();const replacement=id&&document.getElementById(id);
    if(replacement){replacement.focus({preventScroll:true});if(start!=null&&replacement.setSelectionRange)replacement.setSelectionRange(start,end);}
  }
  async onClick(event){
    const action=event.target.closest('[data-config]')?.dataset.config;
    try{
      if(action==='reload'){await this.reload();this.toast('已重新载入；草稿保留，请核对新结果。');this.queuePreview();}
      if(action==='copy'){await navigator.clipboard.writeText('配置已保存，请继续。');this.toast('已复制，请回到 Agent 对话窗口发送。');}
    }catch(error){this.error=error.message;this.paint();this.toast(error.message);}
  }
  subscribe(listener){this.listeners.add(listener);return()=>this.listeners.delete(listener);}
  notify(){for(const listener of this.listeners)listener();}
  expire(message='页面访问已结束，不能继续保存。未保存内容保留在当前页，可先复制；请让 Agent 重新打开页面。'){
    this.expired=true;this.invalidate();this.fileRemoval=null;this.revealed={};this.savedRevealed={};this.error=message;this.paint();this.notify();
  }
  selectionHTML(id,intent=this.switchIntents?.[id],plan=this.preview?.scope_plans?.[id]){return intent?switchInfoHTML.call(this,this.field(id),intent,plan):'';}
  draftValue(id){const edit=this.changes()[id];return edit?.op==='set'?edit.value:edit?.op==='unset'?null:this.field(id).effective.value;}
  requestSwitch(id,target,mode='input'){
    if(this.busy||this.expired||this.uncertain||!this.targets(this.field(id)).some(layer=>layer.id===target))return false;
    delete this.pendingSwitchRevisions?.[id];
    if(this.field(id).effective.source===target)delete this.switchIntents[id];
    else if(this.switchIntents[id]?.target!==target||this.switchIntents[id]?.mode!==mode)this.switchIntents[id]={target,mode};
    this.queuePreview();return true;
  }
}
const baseSet=Configuration.prototype.set;
const baseOnClick=Configuration.prototype.onClick;
function sourceDescription(row) {
  if(row.source==='default')return '默认设置';
  const layer=this.layer(row.source);
  return layer?.scope==='project'?'此项目的设置':layer?.scope==='global'?'通用设置':this.source(row);
}
function sourceValue(f, row) {
  if(f.sensitive)return row.mask || '未设置';
  if(row.value == null)return '未设置';
  if(row.value === '')return '未填写';
  if(f.type==='boolean')return row.value?'已开启':'已关闭';
  if(f.choice_labels?.[row.value])return f.choice_labels[row.value];
  if(Array.isArray(row.value))return row.value.length?row.value.join('\n'):'没有已选项';
  return typeof row.value === 'object' ? JSON.stringify(row.value) : String(row.value);
}
function configurationFiles(f) {
  const defaults=new Set(Object.values(this.snapshot.default_targets||{}));
  if(f.targets?.length===1)defaults.add(f.targets[0]);
  return this.snapshot.layers.filter(l=>l.id!=='process' && this.row(f,l.id).status!=='skipped' &&
    (f.rows.some(row=>row.layer===l.id)) &&
    (l.exists!==false || defaults.has(l.id) || this.row(f,l.id).status==='error'));
}
function fileName(layer) {
  return layer.path.split(/[\\/]/).filter(Boolean).at(-1);
}
function sourceStrip(f) {
  const files=configurationFiles.call(this,f),scopes=[...new Set(['project','global',...files.map(l=>l.scope)])].filter(scope=>files.some(l=>l.scope===scope));
  return `<div class="field-files scope-blocks ${scopes.length===1?'scope-blocks--single':''}" role="group" aria-label="${esc(f.label)}的设置位置">${scopes.map(scope=>{
    const selected=this.inspectedScopes?.[f.id]===scope,target=this.scopeTarget(f,scope),row=this.row(f,target),effective=this.layer(f.effective.source)?.scope===scope;
    const writable=this.targets(f).some(l=>l.id===target),plan=this.preview?.scope_plans?.[f.id],intent=this.switchIntents?.[f.id];
    const unresolved=intent?.target===target&&plan?.state==='choice';
    const defaultSelected=!this.layer(f.effective.source)&&f.effective.empty===true&&this.layer(this.fieldTarget(f.id))?.scope===scope;
    const showAction=selected&&writable&&target!==f.effective.source;
    const label=intent?.target===target&&plan?.state==='ready'&&plan.uses_target||!this.changes()[f.id]&&['effective','shadowed'].includes(row.status)?'使用已有内容':'保存并使用';
    return `<div class="scope-block ${effective?'is-effective':''} ${defaultSelected?'is-default':''} ${selected?'is-open':''}" data-scope="${scope}"><button type="button" id="scope-${esc(f.id)}-${scope}" class="scope-inspect" data-config="inspect-scope" data-id="${esc(f.id)}" data-scope="${scope}" aria-expanded="${selected}" aria-controls="file-info-${esc(f.id)}" ${this.busy?'disabled':''}><span class="scope-title">${esc(scope==='project'?'项目':scope==='global'?'全局':scope)}${effective?'<span class="scope-effective" role="img" aria-label="当前生效" title="当前生效">✓</span>':''}</span><small>${defaultSelected?'默认保存位置 · ':''}${esc(this.layer(target)?.scope_label||(scope==='project'?'仅当前项目':'多个项目共用'))}</small></button>${showAction?`<button type="button" class="scope-apply" data-config="apply-field" data-id="${esc(f.id)}" data-layer="${esc(target)}" ${this.busy||this.uncertain||unresolved||this.needsReview?.[f.id]?'disabled':''}>${label}</button>`:''}</div>`;
  }).join('')}</div>`;
}
Configuration.prototype.set = function(snapshot) {
  const previous=this.snapshot;
  this.fileRemoval=null;
  this.savedRevealed={};
  baseSet.call(this,snapshot);
  this.needsReview??={};this.switchIntents??={};this.inspectedScopes??={};this.scopeFiles??={};
  // A lost response may already have applied a pure switch. Compare the whole
  // checked field projection using its opaque server fingerprint, including secrets.
  for(const f of snapshot.fields)if(this.pendingSwitchRevisions?.[f.id]&&this.pendingSwitchRevisions[f.id]===f.edit_revision)this.clearDraft(f.id);
  if(previous)for(const f of snapshot.fields) {
    const old=previous.fields.find(n=>n.id===f.id);
    if(this.hasFieldDraft(f.id)&&old?.edit_revision!==f.edit_revision)this.needsReview[f.id]=true;
  }
  // A new loader revision invalidates every previous overwrite/discard choice.
  for(const intent of Object.values(this.switchIntents)){delete intent.choice;delete intent.choice_token;}
  this.uncertain=false;
  this.fieldTargets??={};
  for(const f of this.fields()) {
    const targets=this.targets(f),old=this.fieldTargets[f.id];
    this.fieldTargets[f.id]=(this.hasFieldDraft(f.id)?targets.find(l=>l.id===old)?.id:null) ||
      targets.find(l=>this.drafts[l.id]?.[f.id])?.id ||
      this.defaultFieldTarget(f);
  }
};
Configuration.prototype.fieldTarget = function(id) {return this.fieldTargets?.[id] || '';};
Configuration.prototype.defaultFieldTarget = function(f) {
  const targets=this.targets(f);
  return targets.find(l=>l.id===f.effective.source)?.id || targets.find(l=>l.id===this.initialTarget)?.id ||
    targets.find(l=>l.id===this.snapshot.default_targets?.global)?.id || targets[0]?.id || '';
};
Configuration.prototype.scopeTarget = function(f,scope) {
  const files=configurationFiles.call(this,f).filter(l=>l.scope===scope),selected=this.scopeFiles?.[f.id]?.[scope];
  return files.find(l=>l.id===selected)?.id || files.find(l=>l.id===f.effective.source)?.id ||
    files.find(l=>['effective','shadowed'].includes(this.row(f,l.id).status))?.id ||
    files.find(l=>l.id===this.snapshot.default_targets?.[scope])?.id || files[0]?.id || '';
};
Configuration.prototype.changes = function() {
  return Object.fromEntries((this.snapshot?.fields||[]).map(f=>[f.id,this.drafts[this.fieldTarget(f.id)]?.[f.id]]).filter(([,change])=>change));
};
Configuration.prototype.pendingFiles = function() {
  return Object.fromEntries(Object.entries(this.drafts).filter(([,changes])=>Object.keys(changes).length));
};
Configuration.prototype.hasFieldDraft = function(id) {
  return !!this.switchIntents?.[id]||Object.values(this.drafts).some(changes=>changes[id]);
};
Configuration.prototype.count = function() {
  return new Set([...Object.values(this.drafts).flatMap(changes=>Object.keys(changes)),...Object.keys(this.switchIntents||{})]).size;
};
Configuration.prototype.payload = function(only) {
  const files=Object.fromEntries(Object.entries(this.pendingFiles()).map(([target,changes])=>[target,Object.fromEntries(Object.entries(changes).filter(([id])=>includesField(only,id)))]).filter(([,changes])=>Object.keys(changes).length));
  return {files,switches:Object.fromEntries(Object.entries(this.switchIntents||{}).filter(([id])=>includesField(only,id))),
    file_edits:Object.fromEntries(Object.entries(this.fileEdits||{}).map(([target,ids])=>[target,[...ids].filter(id=>(includesField(only,id))&&files[target]?.[id])]))};
};
Configuration.prototype.writable = function(f) {
  return this.targets(f).some(l=>l.id===this.fieldTarget(f.id)) && this.after(f).active!==false;
};
Configuration.prototype.selectFieldTarget = function(id,target) {
  // File inspection is deliberately independent of the ordinary draft target.
  const l=this.layer(target);if(!l)return;
  this.fileRemoval=null;
  (this.scopeFiles[id]??={})[l.scope]=target;
  this.inspectedScopes[id]=l.scope;this.inspectedFiles??={};this.inspectedFiles[id]=target;
  this.savedRevealed={};this.inspectionGeneration=(this.inspectionGeneration||0)+1;
};
Configuration.prototype.stage = function(id,change,raw) {
  if(this.expired||!this.writable(this.field(id)))return;
  delete this.pendingSwitchRevisions?.[id];
  this.fileRemoval=null;
  const target=this.fieldTarget(id),f=this.field(id),explicit=this.fileEdits?.[target]?.has(id);
  const unchanged=!explicit&&change.op==='set'&&(!f.sensitive&&JSON.stringify(change.value)===JSON.stringify(f.effective.value)||f.sensitive&&change.value==='');
  if(unchanged)delete (this.drafts[target]||{})[id];else (this.drafts[target]??={})[id]=change;
  if(raw!==undefined)(this.inputs[target]??={})[id]=raw;
  else delete (this.inputs[target]||{})[id];
  if(this.switchIntents?.[id]){delete this.switchIntents[id].choice;delete this.switchIntents[id].choice_token;}
  delete (this.singleErrors||{})[id];
  this.queuePreview();
  // Selector edits must be visible even when the following preview fails/expires.
  if(raw===undefined)this.rerender();
};
Configuration.prototype.onInput = function(event) {
  const input=event.target,id=input.dataset.configField;
  if(!id||!this.snapshot||this.busy||this.expired)return;
  const f=this.field(id),target=this.fieldTarget(id);
  if(!f||!this.writable(f))return;
  this.fileEdits?.[target]?.delete(id);
  let value=input.value;
  try {
    if(f.type==='number'){if(value===''||!Number.isFinite(Number(value)))throw Error();value=Number(value);}
    if(f.type==='boolean')value=input.checked;
    if(f.type==='list')value=input.value.split('\n').filter(line=>line!=='');
    if(f.type==='json')value=JSON.parse(input.value);
    delete (this.errors[target]||{})[id];
  } catch {(this.errors[target]??={})[id]='请输入有效的 '+(f.type==='json'?'JSON':'数值')+'。';}
  this.stage(id,{op:'set',value},input.value);
};
Configuration.prototype.canSave = function() {
  return !this.busy&&!this.expired&&!this.uncertain&&!Object.keys(this.needsReview||{}).length&&this.count()>0&&!!this.preview&&!this.error&&
    !Object.values(this.errors).some(errors=>Object.keys(errors).length)&&!this.preview.validation_errors?.length;
};
Configuration.prototype.queuePreview = function() {
  if(this.expired)return;
  this.fileRemoval=null;
  if(this.uncertain){this.invalidate();this.paint();return;}
  this.invalidate();this.error='';this.message='';this.retrySave=false;
  if(Object.values(this.errors).some(errors=>Object.keys(errors).length))this.error='请先修正标出的格式错误。';
  this.paint();
  if(!this.count()||this.error||this.composing)return;
  const generation=this.generation,revision=this.snapshot.revision,changes=structuredClone(this.payload());
  this.timer=setTimeout(async()=>{
    try {
      const result=await this.api('/api/preview',{revision,changes});
      if(generation!==this.generation)return;
      for(const id of result.config.unchanged_fields||[]) {
        if(!this.switchIntents?.[id])this.clearDraft(id);
        else for(const draft of Object.values(this.drafts))delete draft[id];
      }
      this.preview=result.config;this.rememberMissing();this.rerender();
    } catch(error) {if(generation!==this.generation)return;this.error=error.message;this.paint();}
  },160);
};
Configuration.prototype.dismissFileRemoval = function(restoreFocus=false) {
  const removal=this.fileRemoval;if(!removal)return;
  this.fileRemoval=null;paintSourceInfo.call(this,this.field(removal.id));
  if(restoreFocus)document.getElementById(`source-clear-${removal.id}`)?.focus({preventScroll:true});
};
Configuration.prototype.requestFileRemoval = async function(id,target) {
  const f=this.field(id);
  if(this.uncertain||this.needsReview?.[id]||!this.row(f,target).present||!this.targets(f).some(l=>l.id===target))return;
  this.dismissFileRemoval();
  const removal=this.fileRemoval={id,target,revision:this.snapshot.revision,loading:true,
    changes:{files:{[target]:{[id]:{op:'unset'}}},switches:{},file_edits:{[target]:[id]}}};
  paintSourceInfo.call(this,f);
  document.getElementById(`file-remove-cancel-${id}`)?.focus({preventScroll:true});
  try {
    const result=await this.api('/api/preview',{revision:removal.revision,changes:removal.changes});
    if(this.fileRemoval!==removal||this.snapshot.revision!==removal.revision)return;
    removal.errors=result.config.validation_errors||[];
    removal.effective=result.config.fields.find(field=>field.id===id)?.effective;
    removal.ready=!removal.errors.length&&!!removal.effective;
  } catch(error) {
    if(this.fileRemoval!==removal)return;
    removal.errors=[error.message];
  } finally {
    if(this.fileRemoval===removal){removal.loading=false;paintSourceInfo.call(this,f);}
  }
};
Configuration.prototype.save = async function(only,removal) {
  if(this.busy||this.expired||this.uncertain||(!only&&!this.canSave())||only&&Object.keys(this.needsReview||{}).some(id=>includesField(only,id)))return;
  if(removal&&(this.fileRemoval!==removal||!removal.ready||removal.revision!==this.snapshot.revision))return;
  if(!removal&&Object.entries(this.errors).some(([,errors])=>Object.keys(errors).some(id=>includesField(only,id))))return;
  const changes=structuredClone(removal?removal.changes:this.payload(only)),revision=this.snapshot.revision;
  this.fileRemoval=null;
  this.invalidate();this.busy=true;this.error='';this.render();
  let writing=false;
  try {
    // A single-item preview uses only that item's payload and saved dependencies.
    const checked=await this.api('/api/preview',{revision,changes});
    if(checked.config.validation_errors?.length){
      if(removal){this.error=checked.config.validation_errors.join(' ');return;}
      this.preview=checked.config;
      if(only&&!Object.values(checked.config.scope_plans||{}).some(plan=>plan.state!=='ready'))
        for(const id of (Array.isArray(only)?only:[only]))(this.singleErrors??={})[id]=checked.config.validation_errors;
      return;
    }
    for(const id of Object.keys(changes.switches)){
      const expected=checked.config.fields.find(field=>field.id===id)?.edit_revision;
      if(expected)(this.pendingSwitchRevisions??={})[id]=expected;
    }
    writing=true;
    const result=await this.api('/api/save',{revision,changes});
    const consumed=new Set([...Object.values(changes.files).flatMap(c=>Object.keys(c)),...Object.keys(changes.switches)]);
    if(!removal)for(const id of consumed){this.clearDraft(id);delete this.fieldTargets[id];}
    else if(!this.hasFieldDraft(only))delete this.fieldTargets[only];
    this.set(result.config);
    if(removal)this.toast('已删除这项设置，已核对当前来源。');
    else this.message=only?'本项已保存并核对生效位置。':'修改已保存并核对生效位置。';
    return {saved:true,config:result.config};
  } catch(error) {
    this.uncertain=writing&&(!error.status||error.status>=500);
    if(!this.uncertain)for(const id of Object.keys(changes.switches))delete this.pendingSwitchRevisions?.[id];
    this.error=this.expired?this.error:this.uncertain?'保存结果尚未确认。请重新载入核对，草稿已保留；不会自动重试。':error.message;
    this.toast(this.error);
  } finally {
    this.busy=false;this.render();
    if(this.count()&&!this.error&&(only||!this.preview?.validation_errors?.length)){const message=this.message;this.queuePreview();this.message=message;this.paint();}
  }
};
Configuration.prototype.clearDraft = function(id) {
  this.fileRemoval=null;
  for(const target of Object.keys(this.drafts)) {
    delete this.drafts[target][id];delete (this.inputs[target]||{})[id];delete (this.errors[target]||{})[id];
    this.fileEdits?.[target]?.delete(id);
  }
  delete (this.switchIntents||{})[id];delete (this.needsReview||{})[id];
  delete this.pendingSwitchRevisions?.[id];
  delete (this.singleErrors||{})[id];
  this.fieldTargets[id]=this.defaultFieldTarget(this.field(id));
};
Configuration.prototype.controlHTML = function(f) {
  const edit=this.changes()[f.id], raw=this.inputs[this.fieldTarget(f.id)]?.[f.id];
  const saved=edit?.op==='unset'?null:edit?.op==='set'?edit.value:f.effective.value;
  const base=`id="field-${esc(f.id)}" data-config-field="${esc(f.id)}" aria-describedby="error-${esc(f.id)} result-${esc(f.id)}" ${this.busy?'disabled':''}`;
  if(f.sensitive) {
    const revealed=this.revealed[`${f.id}:input`];
    const placeholder=edit?.op==='unset'?'将移除此文件中的设置':f.effective.mask||'填写新值';
    const label=`${revealed!==undefined?'隐藏':'显示'}${f.label}`;
    return `<div class="context-secret"><input ${base} type="${revealed!==undefined?'text':'password'}" value="${esc(edit?.op==='set'?edit.value:revealed??'')}" placeholder="${esc(placeholder)}" autocomplete="new-password"><button type="button" data-config="reveal-input" data-id="${esc(f.id)}" aria-label="${esc(label)}" title="${esc(label)}" aria-pressed="${revealed!==undefined}" ${edit?.op==='unset'||(!edit&&!f.effective.configured)?'disabled':''}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>${revealed!==undefined?'<path d="m3 3 18 18"/>':''}</svg></button></div>`;
  }
  if(f.type==='boolean')return `<label class="context-boolean"><span class="boolean-on">已开启</span><span class="boolean-off">已关闭</span><input ${base} type="checkbox" role="switch" ${saved?'checked':''}></label>`;
  if(f.type==='select')return `<select ${base}><option value="">不单独指定</option>${(f.choices||[]).map(c=>`<option value="${esc(c)}" ${c===saved?'selected':''}>${esc(f.choice_labels?.[c]||c)}</option>`).join('')}</select>`;
  if(['list','json'].includes(f.type))return `<textarea ${base}>${esc(raw??(f.type==='list'?(saved||[]).join('\n'):JSON.stringify(saved??null,null,2)))}</textarea>${f.type==='list'?'<small>每行一项，按填写顺序使用。</small>':''}`;
  return `<input ${base} type="${f.type==='number'?'number':'text'}" ${f.type==='number'?'step="any"':''} value="${esc(raw??saved??'')}" placeholder="${esc(edit?.op==='unset'?'将移除此文件中的设置':'')}">`;
};
Configuration.prototype.onClick = async function(event) {
  const button=event.target.closest('[data-config]');
  if(!button||button.disabled||this.busy||this.expired)return;
  const action=button.dataset.config,id=button.dataset.id,layer=button.dataset.layer;
  if(action==='inspect-scope'||action==='close-source') {
    this.fileRemoval=null;
    const scope=button.dataset.scope;
    if(action==='close-source'||this.inspectedScopes[id]===scope) {
      delete this.inspectedScopes[id];delete (this.inspectedFiles||{})[id];
      this.inspectionGeneration=(this.inspectionGeneration||0)+1;
    }
    else this.selectFieldTarget(id,this.scopeTarget(this.field(id),scope));
    this.savedRevealed={};this.rerender();return;
  }
  if(action==='inspect-file') {
    if(!configurationFiles.call(this,this.field(id)).some(l=>l.id===layer))return;
    const previous=this.inspectedFiles?.[id];
    if(previous!==layer) {
      delete (this.savedRevealed||{})[`${id}:${previous}`];
      this.inspectionGeneration=(this.inspectionGeneration||0)+1;
      if(this.switchIntents?.[id]){delete this.switchIntents[id].choice;delete this.switchIntents[id].choice_token;this.queuePreview();}
    }
    (this.inspectedFiles??={})[id]=layer;
    this.selectFieldTarget(id,layer);this.rerender();return;
  }
  if(action==='apply-field') {
    const f=this.field(id);
    if(!this.targets(f).some(l=>l.id===layer))return;
    if(layer!==f.effective.source) {
      const previous=this.switchIntents[id],mode=button.dataset.mode||'existing';
      if(previous?.target!==layer||previous.mode!==mode)this.switchIntents[id]={target:layer,mode};
    } else delete this.switchIntents[id];
    await this.save(id);return;
  }
  if(action==='choose-content') {
    const plan=this.preview?.scope_plans?.[id],intent=this.switchIntents[id];
    if(!plan||!intent||plan.target!==layer)return;
    intent.choice=button.dataset.choice;intent.choice_token=plan.choice_token;
    this.queuePreview();this.rerender();return;
  }
  if(action==='cancel-switch') {delete this.switchIntents[id];this.queuePreview();this.rerender();return;}
  if(action==='review-draft') {delete this.needsReview[id];this.queuePreview();this.rerender();return;}
  if(action==='clear-file'){await this.requestFileRemoval(id,layer);return;}
  if(action==='cancel-file-removal'){this.dismissFileRemoval(true);return;}
  if(action==='confirm-file-removal') {
    const removal=this.fileRemoval;
    if(removal?.id===id&&removal.target===layer)await this.save(id,removal);
    return;
  }
  if(action==='empty-file') {
    if(!this.targets(this.field(id)).some(l=>l.id===layer))return;
    this.savedRevealed={};this.revealed={};
    this.fieldTargets[id]=layer;
    ((this.fileEdits??={})[layer]??=new Set()).add(id);
    delete (this.errors[layer]||{})[id];
    this.stage(id,{op:'set',value:''});
    this.rerender();return;
  }
  if(!['reveal-saved','reveal-input'].includes(action)) {
    if(action==='undo') {
      this.clearDraft(id);
      this.revealed={};this.savedRevealed={};this.queuePreview();this.rerender();return;
    }
    if(['undo','reload'].includes(action))this.savedRevealed={};
    return baseOnClick.call(this,event);
  }
  if(!this.field(id)?.sensitive)return;
  const saved=action==='reveal-saved',key=`${id}:${saved?layer:'input'}`,generation=this.generation,viewGeneration=this.inspectionGeneration||0;
  if(saved&&this.inspectedFiles?.[id]!==layer)return;
  const cache=saved?(this.savedRevealed??={}):this.revealed;
  try {
    if(cache[key]!==undefined)delete cache[key];
    else if(!saved&&this.changes()[id]?.op==='set')cache[key]=this.changes()[id].value;
    else {
      const result=await this.api('/api/reveal',{id,target:saved?layer:null,revision:this.snapshot.revision});
      if(generation!==this.generation||(saved&&(viewGeneration!==(this.inspectionGeneration||0)||this.inspectedFiles?.[id]!==layer)))return;
      cache[key]=result.value;
    }
    if(saved) {
      paintSourceInfo.call(this,this.field(id));
      document.getElementById(`source-reveal-${id}`)?.focus({preventScroll:true});
    } else this.rerender();
  } catch(error) {this.error=error.message;this.paint();this.toast(error.message);}
};
Configuration.prototype.paint = function() {
  if(!this.snapshot)return;
  const feedback=document.getElementById('config-feedback');
  if(!feedback)return;
  const count=this.count(),missing=this.missing(),files=this.pendingFiles();
  document.getElementById('config-readiness').innerHTML=missing.length?`<p class="readiness-warning">还需填写：${missing.map(id=>esc(this.field(id)?.label||id)).join('、')}。可以先保存其他有效修改。</p>`:'';
  feedback.innerHTML=(this.error?`<p class="error">${esc(this.error)}</p>`:'')+
    (this.preview?.validation_errors||[]).map(message=>`<p class="error">${esc(message)}</p>`).join('')+
    (this.message?`<div class="save-success"><strong>${esc(this.message)}</strong><p>请回到 Agent 对话窗口，发送：</p><div class="continue-message"><span>配置已保存，请继续。</span><button type="button" data-config="copy">复制消息</button></div><small>${esc(this.snapshot.validation||'保存校验通过')}</small></div>`:'');
  document.getElementById('config-save-count').textContent=count?`有 ${count} 项修改待保存`:'修改内容后即可保存';
  document.getElementById('save-effect').textContent=count&&!this.preview&&!this.error?'正在检查本次修改…':Object.keys(this.switchIntents||{}).length?'一并保存待处理修改与已提出的切换。':'普通修改沿用各项原位置，下次任务使用。';
  const save=document.getElementById('config-save');save.disabled=!this.canSave();save.textContent=this.busy?'正在保存…':'保存修改';
  for(const f of this.fields()) {
    const target=this.fieldTarget(f.id),current=this.layer(target),next=this.after(f),edit=this.changes()[f.id];
    const pending=Object.entries(files).filter(([,changes])=>changes[f.id]);
    const node=document.getElementById(`result-${f.id}`);
    if(!node)continue;
    const field=node.closest('[data-field]');
    field.querySelector('.field-value')?.classList.toggle('has-edits',pending.length>0);
    const undo=field.querySelector('[data-config=undo]');if(undo)undo.hidden=!pending.length;
    const messages=Object.values(this.errors).map(errors=>errors[f.id]).filter(Boolean);
    document.getElementById(`error-${f.id}`).textContent=[...new Set(messages)].join(' ');
    document.getElementById(`field-${f.id}`)?.setAttribute('aria-invalid',String(messages.length>0));
    const destination=document.getElementById(`destination-${f.id}`);
    if(destination) {
      const intent=this.switchIntents?.[f.id],place=intent?this.layer(intent.target):current;
      destination.textContent=intent?`待切换到：${scopeNames[place.scope]} · ${place.name.replace(/^(项目|全局) · /,'')}；应用前保留输入与当前来源。`:pending.length&&current?`待保存：${scopeNames[current.scope]} · ${current.name.replace(/^(项目|全局) · /,'')}；查看其他位置不会更改它。`:'';
      destination.hidden=!pending.length&&!intent;
    }
    paintSourceInfo.call(this,f);
    node.innerHTML='';
    if(pending.length&&this.preview&&(!this.switchIntents?.[f.id]||this.preview.scope_plans?.[f.id]?.state==='ready')) {
      node.innerHTML=`<p class="config-note edit-preview">保存后，此项目使用：${esc(sourceValue(f,next.effective))} · ${esc(sourceDescription.call(this,next.effective))}</p>`;
      if(edit?.op==='set'&&edit.value!==''&&next.effective.source!==target)
        node.innerHTML+='<p class="config-note">这里的修改会保存；此项目已有优先使用的设置。</p>';
    } else if(!pending.length&&(!this.layer(f.effective.source)||f.effective.source==='process')) {
      node.innerHTML=`<p class="config-note">${f.effective.source?'当前使用'+esc(sourceDescription.call(this,f.effective))+'。':'这一项还没有填写。'}</p>`;
    }
    const elsewhere=pending.filter(([layer])=>layer!==target);
    if(elsewhere.length)node.innerHTML+=`<p class="config-note">本项另有 ${elsewhere.length} 处文件修改待保存：${elsewhere.map(([layer])=>esc(scopeNames[this.layer(layer)?.scope]+' · '+fileName(this.layer(layer)))).join('、')}。</p>`;
    if(next.effective.message)node.innerHTML+=`<p class="error">${esc(next.effective.message)}</p>`;
    if(this.needsReview?.[f.id])node.innerHTML+=`<p class="error">已保存的本项内容发生变化。输入和原保存位置已保留，请先核对新来源。</p><button type="button" class="text-button" data-config="review-draft" data-id="${esc(f.id)}">已核对，保留这份草稿</button>`;
    if(this.singleErrors?.[f.id])node.innerHTML+=`<p class="error single-save-error">本项尚未单独保存：${this.singleErrors[f.id].map(esc).join(' ')} 如需同时修改关联项，请使用底部“保存修改”。</p>`;
    const intent=this.switchIntents?.[f.id],plan=this.preview?.scope_plans?.[f.id];
    if(intent)node.innerHTML+=switchInfoHTML.call(this,f,intent,plan);
    const undoNow=field.querySelector('[data-config=undo]');if(undoNow)undoNow.hidden=!this.hasFieldDraft(f.id);
    const strip=field.querySelector('.field-files');if(strip)strip.outerHTML=sourceStrip.call(this,f);
  }
  this.saveBarObserver?.disconnect();
  const bar=document.querySelector('.context-save'),shell=document.querySelector('.config-shell');
  if(bar&&shell) {
    const reserve=()=>shell.style.setProperty('--save-bar-height',Math.ceil(bar.getBoundingClientRect().height)+'px');
    reserve();this.saveBarObserver=new ResizeObserver(reserve);this.saveBarObserver.observe(bar);
  }
  this.notify();
  if(this.expired) {
    this.root.querySelectorAll('[data-config], [data-action], [data-voice-open]').forEach(button=>button.disabled=true);
    this.root.querySelectorAll('[data-config-field]').forEach(input=>{
      if(input.type==='checkbox'||input.tagName==='SELECT')input.disabled=true;
      else input.readOnly=true;
      if(this.field(input.dataset.configField)?.sensitive)input.type='password';
    });
    const countdown=document.getElementById('countdown');if(countdown)countdown.textContent='页面已结束';
  }
};
Configuration.prototype.html = function() {
  const pending=new Set(Object.values(this.drafts).flatMap(changes=>Object.keys(changes)));
  const eligible=this.fields().filter(f=>this.after(f).active!==false||pending.has(f.id));
  const primary=eligible.filter(f=>this.visible.has(f.id)),other=eligible.filter(f=>!this.visible.has(f.id));
  return `<section class="content context-content"><form id="configuration-form" novalidate>
    <div class="heading"><div><h1>${esc(this.snapshot.presentation?.title||'配置设置')}</h1><p>${esc(this.snapshot.presentation?.description||'展开设置位置，查看详情或切换生效位置。')}</p></div><button type="button" data-config="reload" ${this.busy?'disabled':''}>重新载入</button></div>
    <div id="config-readiness" role="status"></div>
    ${this.snapshot.note?`<p class="config-note prototype-note">${esc(this.snapshot.note)}</p>`:''}
    <div class="context-fields">${primary.map(f=>this.fieldHTML(f)).join('')}</div>
    ${other.length?this.detail('other',`其他设置 <span class="detail-count">${other.length}</span>`,[...new Set(other.map(f=>f.group||'其他'))].map(group=>`<section class="context-group"><h2>${esc(group)}</h2>${other.filter(f=>(f.group||'其他')===group).map(f=>this.fieldHTML(f)).join('')}</section>`).join(''),'other-settings'):''}
    <footer class="context-save" aria-label="保存配置"><div class="save-controls"><div class="save-status-line"><span id="config-save-count"></span><span id="save-effect"></span></div><button type="submit" class="primary" id="config-save">保存修改</button></div><div id="config-feedback" role="status" aria-live="polite"></div></footer>
  </form></section>`;
};

Configuration.prototype.fieldHTML=function(f){
  const writable=this.writable(f),missing=this.missing().includes(f.id),edit=this.changes()[f.id];
  return `<section class="context-field" data-field="${esc(f.id)}"><div class="context-field-label"><div class="field-title"><div class="field-name"><label for="field-${esc(f.id)}">${esc(f.label)}</label>${missing?'<span class="field-tag missing">需要补填</span>':f.required?'<span class="field-tag">必要配置</span>':''}</div><code class="field-config-key">${esc(f.config_key||f.id)}</code></div></div>
  ${f.help&&f.type!=='boolean'?`<p class="field-help">${esc(f.help)}</p>`:''}
  <div class="field-editor${f.type==='boolean'?' field-editor--boolean':''}"><div class="field-value${edit?' has-edits':''}">${f.help&&f.type==='boolean'?`<p class="field-help">${esc(f.help)}</p>`:''}${writable?this.controlHTML(f):`<div class="readonly-value">${esc(sourceValue(f,f.effective))}</div>`}</div>
  ${sourceStrip.call(this,f)}<p class="field-destination" id="destination-${esc(f.id)}" hidden></p><section class="source-inline" id="file-info-${esc(f.id)}" role="region" aria-label="${esc(f.label)}的文件信息" hidden></section></div>
  ${!writable?`<p class="config-note">${this.after(f).active===false?'当前不适用，请核对关联设置。':'此项当前只读。'}</p>`:''}<p class="field-error error" id="error-${esc(f.id)}"></p><div class="field-result" id="result-${esc(f.id)}"></div><button type="button" class="text-button undo" data-config="undo" data-id="${esc(f.id)}" ${!this.hasFieldDraft(f.id)?'hidden':''}>撤销本项修改</button></section>`;
};
function switchInfoHTML(f,intent,plan) {
  const target=this.layer(intent.target),choice=intent.choice;
  const cleanup=(plan?.cleanup||[]).map(id=>this.layer(id));
  return `<section class="switch-plan" aria-label="${esc(f.label)}的切换处理">
    <div class="switch-plan-head"><strong>准备使用：${esc(scopeNames[target.scope])} · ${esc(target.name.replace(/^(项目|全局) · /,''))}</strong><button type="button" data-config="cancel-switch" data-id="${esc(f.id)}" ${this.busy?'disabled':''}>取消切换</button></div>
    <code class="switch-file-path">${esc(target.path)}</code><p>${esc(target.scope_label)}</p>
    <p class="${plan?.state==='blocked'?'error':''}">${esc(plan?.message||'正在核对本项内容与生效位置…')}</p>
    ${cleanup.length?`<p>将删除以下文件中的本项，其他设置保留：</p><ul class="switch-cleanup">${cleanup.map(layer=>`<li><strong>${esc(layer.name)}</strong><code class="switch-file-path">${esc(layer.path)}</code><span>${esc(layer.scope_label)}</span></li>`).join('')}</ul><p>已保存的清理不会因撤销草稿恢复。</p>`:''}
    ${plan?.conflict?`<div class="switch-choices" role="radiogroup" aria-label="${esc(f.label)}有不同内容，选择要使用的内容">${[['input','保存我填写的内容','替换目标文件里原有的这一项。'],['target','使用目标已有内容','应用成功后放弃本项未保存修改。']].map(([value,label,help])=>`<button type="button" role="radio" aria-checked="${choice===value}" data-config="choose-content" data-id="${esc(f.id)}" data-layer="${esc(intent.target)}" data-choice="${value}" ${this.busy?'disabled':''}><span class="choice-dot" aria-hidden="true"></span><span><strong>${label}</strong><small>${help}</small></span></button>`).join('')}</div>`:''}
    ${plan?.state==='ready'?`<button type="button" class="switch-commit" data-config="apply-field" data-id="${esc(f.id)}" data-layer="${esc(intent.target)}" data-mode="${esc(intent.mode||'existing')}" ${this.busy||this.uncertain||this.needsReview?.[f.id]?'disabled':''}>${choice==='target'||plan.uses_target?'使用已有内容':'保存并使用'}</button>`:''}</section>`;
}
function fileRemovalHTML(f,layer) {
  const removal=this.fileRemoval;
  if(removal?.id!==f.id||removal.target!==layer)return '';
  const l=this.layer(layer),after=removal.effective;
  return `<div class="file-removal" role="dialog" aria-label="删除${esc(f.label)}" aria-busy="${removal.loading}">
    <strong>删除这项设置？</strong>
    <p>仅删除 <code>${esc(l.name.replace(/^(项目|全局) · /,''))}</code> 中的「${esc(f.label)}」，其他设置保留。</p>
    <p>${esc(l.scope_label)} 已删除的原值不会自动恢复。</p>
    ${removal.loading?'<p role="status">正在核对删除后的设置…</p>':after?`<p>删除后，此项目使用：${esc(sourceValue(f,after))} · ${esc(sourceDescription.call(this,after))}</p>`:''}
    ${(removal.errors||[]).map(error=>`<p class="error" role="alert">${esc(error)}</p>`).join('')}
    ${this.hasFieldDraft(f.id)?'<p>本项未保存的输入会保留，删除后需重新核对。</p>':''}
    <div class="file-removal-buttons"><button type="button" id="file-remove-cancel-${esc(f.id)}" data-config="cancel-file-removal">取消</button><button type="button" id="file-remove-confirm-${esc(f.id)}" data-config="confirm-file-removal" data-id="${esc(f.id)}" data-layer="${esc(layer)}" ${!removal.ready||this.expired||this.uncertain?'disabled':''}>确认删除</button></div>
  </div>`;
}
function sourceInfoHTML(f) {
  const layer=this.inspectedFiles?.[f.id],l=configurationFiles.call(this,f).find(l=>l.id===layer);
  if(!l||this.inspectedScopes?.[f.id]!==l.scope)return '';
  const row=this.row(f,layer),key=`${f.id}:${layer}`,revealed=this.savedRevealed?.[key]!==undefined;
  const value=revealed?this.savedRevealed[key]:sourceValue(f,row);
  const status=l.exists===false?'未创建，保存时创建':sourceStatuses[row.status]||'未设置此项';
  const operations=row.present&&this.targets(f).some(target=>target.id===layer),disabled=this.busy?'disabled':'';
  const editDisabled=this.busy||this.uncertain||this.needsReview?.[f.id]?'disabled':'';
  const files=configurationFiles.call(this,f).filter(file=>file.scope===l.scope);
  const upper=this.snapshot.layers.slice(0,this.snapshot.layers.findIndex(file=>file.id===layer)).filter(file=>['effective','shadowed'].includes(this.row(f,file.id).status)&&this.row(f,file.id).present);
  const canCopy=!this.changes()[f.id]&&layer!==f.effective.source&&operations&&['effective','shadowed'].includes(row.status)&&(f.sensitive||JSON.stringify(row.value)!==JSON.stringify(f.effective.value));
  return `<div class="source-inline-head"><strong>${l.scope==='project'?'项目设置':'全局设置'}</strong><div class="source-inline-controls"><span>按优先顺序排列</span><button type="button" data-config="close-source" data-id="${esc(f.id)}" aria-label="收起${esc(f.label)}的文件详情" ${disabled}>收起详情</button></div></div><div class="inline-files">${files.map(file=>{
    const r=this.row(f,file.id);return `<button type="button" class="inline-file ${file.id===layer?'is-inspected':''}" data-config="inspect-file" data-id="${esc(f.id)}" data-layer="${esc(file.id)}" aria-pressed="${file.id===layer}" ${disabled}><span class="inline-file-label"><span>${esc(file.name.replace(/^(项目|全局) · /,''))}</span>${file.id===layer?`<code class="source-inline-path">${esc(file.path)}</code>`:''}</span><small>${file.id===f.effective.source?'✓ 当前生效':file.exists===false?'未创建':esc(sourceStatuses[r.status]||'未设置')}</small></button>`;
  }).join('')}</div><div class="source-inline-body"><div class="source-inline-value"><span>这里保存的内容</span><code>${esc(value)}</code></div><div class="source-inline-actions">${f.sensitive&&row.configured?`<button type="button" id="source-reveal-${esc(f.id)}" data-config="reveal-saved" data-id="${esc(f.id)}" data-layer="${esc(layer)}" ${disabled}>${revealed?'隐藏':'显示'}密钥</button>`:''}${operations?`<span class="file-delete-control"><button type="button" id="source-clear-${esc(f.id)}" data-config="clear-file" data-id="${esc(f.id)}" data-layer="${esc(layer)}" aria-haspopup="dialog" aria-expanded="${this.fileRemoval?.id===f.id&&this.fileRemoval.target===layer}" ${editDisabled}>删除这项设置</button>${fileRemovalHTML.call(this,f,layer)}</span><button type="button" id="source-empty-${esc(f.id)}" data-config="empty-file" data-id="${esc(f.id)}" data-layer="${esc(layer)}" ${editDisabled}>清空内容</button>`:''}</div></div><p class="source-inline-help">${esc(l.scope_label)}</p>${layer!==f.effective.source&&this.targets(f).some(t=>t.id===layer)?`<p class="source-inline-help">${upper.length?`使用此处将移除 ${upper.length} 处优先位置的这一项设置：${upper.map(file=>esc(file.name)).join('、')}。这些原值不会自动恢复；其他项与更低位置保留。`:'使用此处会保留其他位置已有的设置。'}</p>`:''}${canCopy?`<div class="copy-current-option"><span>也可以将输入框中的当前内容保存到这里：</span><button type="button" data-config="apply-field" data-id="${esc(f.id)}" data-layer="${esc(layer)}" data-mode="input" ${this.busy||this.uncertain?'disabled':''}>保存当前内容并使用</button></div>`:''}${row.message?`<p class="source-inline-message ${row.status==='error'?'error':''}">${esc(row.message)}</p>`:''}`;
}
function positionFileRemoval() {
  const removal=this.fileRemoval;if(!removal)return;
  const panel=document.querySelector('.file-removal'),anchor=document.getElementById(`source-clear-${removal.id}`);
  if(!panel||!anchor)return;
  const rect=anchor.getBoundingClientRect(),margin=16,gap=10;
  panel.style.maxHeight=`${Math.max(160,window.innerHeight-margin*2)}px`;
  const bounds=panel.getBoundingClientRect(),left=Math.max(margin,Math.min(rect.right-bounds.width,window.innerWidth-bounds.width-margin));
  const above=rect.top>=bounds.height+gap+margin;
  panel.style.left=`${left}px`;
  panel.style.top=`${Math.max(margin,Math.min(above?rect.top-bounds.height-gap:rect.bottom+gap,window.innerHeight-bounds.height-margin))}px`;
  panel.dataset.placement=above?'above':'below';
  panel.style.setProperty('--arrow-left',`${Math.max(16,Math.min(rect.left+rect.width/2-left,bounds.width-16))}px`);
}
function paintSourceInfo(f) {
  const node=document.getElementById(`file-info-${f.id}`);
  if(!node)return;
  const html=sourceInfoHTML.call(this,f);
  node.hidden=!html;
  if(node.sourceHTML!==html){
    const focused=node.contains(document.activeElement)?document.activeElement.id:null;
    node.innerHTML=html;node.sourceHTML=html;
    if(focused)document.getElementById(focused)?.focus({preventScroll:true});
  }
  positionFileRemoval.call(this);
}
