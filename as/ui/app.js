'use strict';
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const copy = value => JSON.parse(JSON.stringify(value));
const h = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const effortNames = {minimal:'最低',low:'低',medium:'中等',high:'高',xhigh:'超高',max:'最高',ultra:'极高'};
const effortLabel = value => value == null ? '不适用' : (effortNames[value] || value);
const agentLabel = agent => catalog[agent]?.label || agent;
let saved, draft, revision, catalog, csrf = '', currentTab = 'presets', editing = null;
let returnFocus = null, editorDrafts = {}, toastTimer, busy = false, expired = false, lastActivity = 0;
let initialization = {show:false}, initializing = false;
const dirty = () => JSON.stringify(draft) !== JSON.stringify(saved);
const model = (agent, id) => catalog[agent]?.models.find(item => item.id === id);
const modelName = (agent, id) => model(agent, id)?.label || id;
const agentBadge = agent => `<span class="agent-symbol ${agent === 'claude' ? 'claude' : ''}">${agent === 'claude' ? '✳' : '◎'}</span>`;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {const error = new Error(data.message || '请求未完成，请重试。');error.status = response.status;throw error;}
  return data;
}
function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 3600);
}
function reportError(error) {
  const box = $('#app-error'); box.hidden = false;box.textContent = error.message;
  if (error.status === 409) {
    const button = document.createElement('button');button.textContent='重新载入配置';
    button.addEventListener('click', async () => {
      if (!confirm('重新载入将替换当前草稿。是否继续？')) return;
      try {await load();box.hidden=true;} catch (next) {reportError(next);}
    });box.append(button);
  }
  if (error.status === 410) {expired = true;$('#lifetime').textContent='配置页已到期';$('#save').disabled=true;}
}
function applyReport(report) {
  saved = copy(report.settings);draft = copy(saved);revision=report.revision;catalog=report.catalog;
  initialization=report.initialization || {show:false};initializing=false;
  if(report.synthetic) {$('.preview-badge').textContent='合成数据预览';$('.catalog-note').textContent='模型目录为合成数据，仅用于审阅交互。保存不会影响个人配置或创建会话。';$('.demo-notice span:nth-child(2)').textContent='演示情景：仅 Codex 可用。所有操作只保存演示数据，不创建会话。';}
  $('#source-path').textContent=report.source;editorDrafts={};renderAgents();render();
}
function acceptSave(report) {
  // The request owns its submitted snapshot; newer edits still belong to the user.
  saved=copy(report.settings);revision=report.revision;catalog=report.catalog;
  initialization=report.initialization || {show:false};initializing=false;
  $('#source-path').textContent=report.source;renderAgents();render();
}
async function load() {
  const state = await api('/api/state');csrf=state.csrf;applyReport(state.settingsReport);updateLifetime(state.lifetime);
}
function updateLifetime(lifetime) {
  $('#lifetime').textContent=`空闲 ${Math.ceil(lifetime.remaining / 60)} 分钟到期`;
}
function updateSaveState() {
  const changed=dirty();$('#save').disabled=!changed || busy || expired;$('#discard').disabled=!changed || busy;
  $('#dirty-dot').classList.toggle('dirty',changed);
  $('#save-state').textContent=changed?'有未保存的变更':revision==='missing'?'尚未保存配置':'配置已保存';
  $('#save-detail').textContent=changed?'预览正在使用草稿 · 保存后生效':'修改只在点击保存后写入个人配置';
}
function render() {
  renderInitialization();
  const changes=[];
  for(const kind of ['defaults','presets'])for(const key of new Set([...Object.keys(saved[kind]),...Object.keys(draft[kind])])) {
    const before=saved[kind][key], after=draft[kind][key];
    if(JSON.stringify(before)===JSON.stringify(after))continue;
    changes.push(`<li><code>${h(kind+'.'+key)}</code> · ${!after?'移除':`${before?'修改':'新增'}：${h(agentLabel(after.agent || key))} · ${h(after.model)} · ${h(effortLabel(after.effort))}`}</li>`);
  }
  $('#change-summary').hidden=!changes.length;$('#change-list').innerHTML=changes.join('');
  const presets=Object.entries(draft.presets);
  $('#preset-count').textContent=presets.length;$('#nav-count').textContent=presets.length;
  $('#preset-rows').innerHTML=presets.map(([code,preset]) => `<tr>
    <td><div class="preset-identity"><span class="code-key">${h(code)}</span><div><div class="preset-name">${h(preset.name)}</div><div class="preset-command"><code>$as ${h(code)}</code></div></div></div></td>
    <td><div class="agent-label">${agentBadge(preset.agent)}${h(agentLabel(preset.agent))}</div><div class="model-label">${h(modelName(preset.agent,preset.model))}</div>${choiceProblem(preset.agent,preset)?`<small class="choice-warning">${h(choiceProblem(preset.agent,preset))}</small>`:''}</td>
    <td><span class="effort-pill ${['high','xhigh'].includes(preset.effort)?'high':''}">${h(effortLabel(preset.effort))}</span></td>
    <td><button class="edit-button" data-edit="${h(code)}" aria-label="编辑预设 ${h(code)}">↗</button></td></tr>`).join('');
  $('#empty-presets').hidden=presets.length>0;$('.preset-table').hidden=presets.length===0;
  $('#default-summary-chips').innerHTML=Object.entries(draft.defaults).map(([agent,value])=>`<span>${h(agentLabel(agent))} · ${h(effortLabel(value.effort))}</span>`).join('') || '<span>尚未保存默认值</span>';
  $('#default-cards').innerHTML=Object.entries(catalog).map(([agent,entry])=>{
    const value=draft.defaults[agent] || {}, selected=model(agent,value.model);
    const choices=entry.models.map(m=>`<option value="${h(m.id)}" ${value.model===m.id?'selected':''}>${h(m.label)}</option>`).join('');
    const stale=value.model&&!selected?`<option selected value="${h(value.model)}">${h(value.model)} · 当前不可用</option>`:'';
    const levels=selected?.efforts || [];
    return `<article class="default-card"><header>${agentBadge(agent)}<h3>${h(entry.label)}</h3><span>调用 $as 时使用</span></header>${entry.error?`<p role="status">${h(entry.error)}</p>`:''}<div class="default-inputs"><div><label for="default-${agent}-model">默认模型</label><small class="config-key">defaults.${agent}.model</small><select id="default-${agent}-model" data-default-agent="${agent}" data-field="model"><option value="">不设置默认配置</option>${stale}${choices}</select></div><div><label for="default-${agent}-effort">思考强度</label><small class="config-key">defaults.${agent}.effort</small><select id="default-${agent}-effort" data-default-agent="${agent}" data-field="effort" ${!selected?'disabled':''}><option value="">${selected&&!levels.length?'不适用':'请选择强度'}</option>${value.effort&&!levels.includes(value.effort)?`<option selected value="${h(value.effort)}">${h(effortLabel(value.effort))} · 当前不可用</option>`:''}${levels.map(level=>`<option value="${h(level)}" ${value.effort===level?'selected':''}>${h(effortLabel(level))}</option>`).join('')}</select></div></div>${value.model?`<small class="choice-warning">${h(choiceProblem(agent,value))}</small><button class="delete-button" data-remove-default="${agent}">移除默认配置</button>`:''}</article>`;
  }).join('');
  updateSaveState();renderPreview();
}
function choiceProblem(agent,value) {
  if(!catalog[agent]?.available)return catalog[agent]?.error || `${agentLabel(agent)} 当前不可用`;
  const selected=model(agent,value.model);
  if(!selected)return `模型 ${value.model} 不在当前目录中`;
  if(!(selected.efforts.length?selected.efforts:[null]).includes(value.effort))return `思考强度 ${value.effort ?? '未选择'} 与模型不匹配`;
  return '';
}
function renderInitialization() {
  $('#initialization').hidden=!initialization.show;
  $('#initialization-draft').hidden=!initializing;
  if(!initialization.show)return;
  const settings=initialization.settings;
  const rows=[...Object.entries(settings.defaults).map(([agent,value])=>({agent,...value,name:`${agentLabel(agent)} 默认配置`,code:null})),...Object.entries(settings.presets).map(([code,value])=>({code,...value}))];
  $('#recommendations').innerHTML=rows.map(value=>`<article class="recommendation"><strong>${h(value.name)}</strong><small>${value.code?`会话预设 · <code>${h(value.code)}</code>`:'Agent 默认配置'}</small><p>${h(agentLabel(value.agent))} · ${h(value.model)}</p><p>思考强度：${h(effortLabel(value.effort))} <code>${h(value.effort)}</code></p><small class="${choiceProblem(value.agent,value)?'choice-warning':'choice-ready'}">${h(choiceProblem(value.agent,value) || '当前目录支持')}</small></article>`).join('');
  $('#add-common').disabled=busy||dirty()||expired;$('#dismiss-initialization').disabled=busy||expired;
}
$('#add-common').addEventListener('click',async()=>{
  if(busy||dirty()||expired)return;
  busy=true;renderInitialization();
  try {
    const result=await api('/api/as/initialize',{revision});
    initialization.show=false;
    if(dirty()) {toast('提示已处理，保留你正在编辑的草稿');return;}
    draft=copy(result.settings);initializing=true;render();toast('已加入草稿，请检查后保存');
  } catch(error) {reportError(error);} finally {busy=false;render();}
});
$('#dismiss-initialization').addEventListener('click',async()=>{
  if(busy||expired)return;
  busy=true;renderInitialization();
  try {await api('/api/as/dismiss',{});initialization.show=false;toast('已关闭，以后不再显示此提示');}
  catch(error) {reportError(error);} finally {busy=false;render();}
});
function setTab(tab) {
  currentTab=tab;$('#presets-view').hidden=tab!=='presets';$('#defaults-view').hidden=tab!=='defaults';
  $('#default-summary').hidden=tab==='defaults';
  $$('.nav,.tab').forEach(button=>button.classList.toggle('active',button.dataset.tab===tab));
}
function parseCall(value) {
  const text=value.trimStart();const match=text.match(/^(\$as|\/as)(?=\s|$)/);
  if(!match) throw new Error('请以 $as 或 /as 开始输入。');
  const separator=text=>text.replace(/^(?:[^\S\r\n]*\r?\n|[^\S\r\n]+)/,'');
  let remainder=separator(text.slice(match[0].length)),code=null,message='',carryContext=false;
  while(true) {
    let tokens=remainder.trimStart();const first=tokens.match(/^\S+/)?.[0];
    if(first==='--') {message=separator(tokens.slice(2));break;}
    if(first==='-r') {
      if(carryContext) throw new Error('一次调用只需指定一个 -r。');
      carryContext=true;remainder=separator(tokens.slice(2));
    } else if(first==='--preset') {
      if(code!==null) throw new Error('一次调用只能选择一个预设。');
      tokens=tokens.slice(first.length).trimStart();code=tokens.match(/^\S+/)?.[0];
      if(!code) throw new Error('--preset 后须提供已保存的预设代码。');
      if(!Object.hasOwn(draft.presets,code)) throw new Error(`没有找到预设「${code}」。请先添加这个代码。`);
      remainder=separator(tokens.slice(code.length));
    } else if(code===null&&first!==undefined&&Object.hasOwn(draft.presets,first)) {
      code=first;remainder=separator(tokens.slice(code.length));
    } else {
      if(code===null&&tokens.match(/^\S+\s+(\S+)/)?.[1]==='-r') throw new Error(`没有找到预设「${first}」。请检查代码；字面正文请放在 -- 后。`);
      message=remainder;break;
    }
  }
  return {code,message:message.trim()?message:'',carryContext};
}
function renderPreview() {
  if(!draft) return;
  const output=$('#launch-preview');
  try {
    const call=parseCall($('#command').value), current=$('#current-agent').value;
    if(!call.code&&!draft.defaults[current]) throw new Error(`尚未保存 ${agentLabel(current)} 默认配置。`);
    const config=call.code?draft.presets[call.code]:{agent:current,...draft.defaults[current]};
    const problem=choiceProblem(config.agent,config);if(problem)throw new Error(problem);
    const switched=config.agent!==current;
    output.innerHTML=`<div class="result-heading"><span>将创建的新会话</span><span>${dirty()?'基于当前草稿':'基于已保存配置'}</span></div><div class="result-agent">${agentBadge(config.agent)}${h(agentLabel(config.agent))}${switched?'<span class="switch-badge">切换 Agent</span>':''}</div><div class="result-detail"><span>配置来源</span><b>${call.code?`预设 ${h(call.code)}`:`${h(agentLabel(current))} 默认值`}</b></div><div class="result-detail"><span>模型</span><b>${h(modelName(config.agent,config.model))}</b></div><div class="result-detail"><span>思考强度</span><b>${h(effortLabel(config.effort))}</b></div><div class="result-detail"><span>工作目录 / 分支</span><b>沿用当前所在位置</b></div><div class="result-detail"><span>权限 / 界面</span><b>沿用 Orca 启动配置</b></div><div class="result-detail"><span>上下文</span><b>${call.carryContext?'读取当前会话原始记录':'全新上下文'}</b></div>${call.message?`<div class="result-message"><span>首条消息 · ${call.carryContext?'先读取上下文，再处理正文':'自动发送并开始处理'}</span><p>${h(call.message)}</p></div>`:`<div class="message-empty">${call.carryContext?'读取上下文后确认接收，等待你输入。':'打开空白会话，等待你输入。<br>不携带当前聊天历史。'}</div>`}`;
  } catch(error) {output.innerHTML=`<div class="preview-error">${h(error.message)}</div>`;}
}
function currentEditorValue() {
  return {code:$('#preset-code').value,name:$('#preset-name').value,agent:$('#preset-agent').value,model:$('#preset-model').value,effort:$('#preset-effort').value || null};
}
function rememberEditor() {
  if(editing!==null) editorDrafts[editing]=currentEditorValue();
}
function renderAgents() {
  for(const id of ['current-agent','preset-agent']) {
    const node=$('#'+id),value=node.value;
    node.innerHTML=Object.entries(catalog).map(([agent,entry])=>`<option value="${agent}">${h(entry.label)}${entry.available?'':' · 不可用'}</option>`).join('');
    if(id==='current-agent')node.innerHTML+='<option value="workbuddy">WorkBuddy（调用宿主）</option>';
    if(catalog[value]||(id==='current-agent'&&value==='workbuddy'))node.value=value;
  }
}
function updateEditorModels(value) {
  const agent=$('#preset-agent').value,models=catalog[agent]?.models || [];
  $('#preset-model').innerHTML='<option value="">请选择模型</option>'+models.map(m=>`<option value="${h(m.id)}">${h(m.label)}</option>`).join('');
  if(value&&!models.some(m=>m.id===value))$('#preset-model').append(new Option(value+' · 当前不可用',value));
  $('#preset-model').value=value || '';
}
function updateEditorEfforts(value) {
  const selected=model($('#preset-agent').value,$('#preset-model').value),levels=selected?.efforts || [];
  $('#preset-effort').innerHTML=`<option value="">${selected&&!levels.length?'不适用':'请选择强度'}</option>`+levels.map(level=>`<option value="${h(level)}">${h(effortLabel(level))} · ${h(level)}</option>`).join('');
  $('#preset-effort').value=levels.includes(value)?value:'';
}
function updateEditorHints() {
  const code=$('#preset-code').value || '<code>', agent=$('#preset-agent').value;
  $$('[data-key]').forEach(el=>el.textContent=`presets.${code}.${el.dataset.key}`);
  $('#editor-example').textContent=`$as ${code}`;
  $('#agent-explanation').textContent=`使用这个代码时，始终启动 ${catalog[agent].label}，无论当前会话使用哪个 Agent。`;
}
function openEditor(code) {
  if(!draft) return;
  returnFocus=document.activeElement;editing=code ?? '__new__';
  const value=editorDrafts[editing] || (code?{code,...draft.presets[code]}:{code:'',name:'',agent:Object.keys(catalog)[0],model:'',effort:null});
  $('#editor-title').textContent=code?`编辑预设 ${code}`:'添加会话预设';$('#preset-code').value=value.code;$('#preset-name').value=value.name;$('#preset-agent').value=value.agent;
  updateEditorModels(value.model);updateEditorEfforts(value.effort);updateEditorHints();$('#form-error').textContent='';
  let deleteButton=$('#delete-preset');if(deleteButton) deleteButton.remove();
  if(code) {deleteButton=document.createElement('button');deleteButton.id='delete-preset';deleteButton.type='button';deleteButton.className='delete-button';deleteButton.textContent='删除这个预设';
    deleteButton.addEventListener('click',()=>{if(confirm(`从草稿删除预设 ${code}？保存后才会更新个人配置。`)){delete draft.presets[code];delete editorDrafts[editing];closeEditor(false);render();toast('已从草稿移除预设');}});$('.drawer-body').append(deleteButton);}
  $('#editor').hidden=false;$('#drawer-backdrop').hidden=false;document.body.style.overflow='hidden';
  $('.workspace').inert=true;$('.sidebar').inert=true;$('#preset-code').focus();
}
function closeEditor(keep=true) {
  if(keep) rememberEditor();else delete editorDrafts[editing];
  $('#editor').hidden=true;$('#drawer-backdrop').hidden=true;document.body.style.overflow='';editing=null;
  $('.workspace').inert=false;$('.sidebar').inert=false;if(returnFocus?.isConnected) returnFocus.focus();
}
$('#preset-form').addEventListener('submit',event=>{
  event.preventDefault();const value=currentEditorValue(),code=value.code.trim();
  if(!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}$/.test(code)) {$('#form-error').textContent='请输入 1–16 位有效代码。';return;}
  if(Object.hasOwn(draft.presets,code)&&code!==editing) {$('#form-error').textContent=`代码「${code}」已经存在，请换一个。`;return;}
  if(['constructor','prototype','__proto__'].includes(code)) {$('#form-error').textContent='这个代码不可用。';return;}
  const selected=model(value.agent,value.model);
  if(!selected||(selected.efforts.length&&!selected.efforts.includes(value.effort))) {$('#form-error').textContent='请选择可用模型和思考强度。';return;}
  if(!value.name.trim()) {$('#form-error').textContent='请填写预设名称。';return;}
  if(editing!=='__new__'&&editing!==code) delete draft.presets[editing];
  draft.presets[code]={name:value.name.trim(),agent:value.agent,model:value.model,effort:value.effort};
  closeEditor(false);render();setTab('presets');toast('已加入草稿，保存后生效');
});
$('#preset-agent').addEventListener('change',()=>{updateEditorModels();updateEditorEfforts();updateEditorHints();rememberEditor();});
$('#preset-model').addEventListener('change',()=>{updateEditorEfforts($('#preset-effort').value);rememberEditor();});
$('#preset-form').addEventListener('input',()=>{updateEditorHints();rememberEditor();});
$('#close-editor').addEventListener('click',()=>closeEditor());$('#drawer-backdrop').addEventListener('click',()=>closeEditor());$('#cancel-edit').addEventListener('click',()=>closeEditor(false));
document.addEventListener('keydown',event=>{
  if(editing===null)return;
  if(event.key==='Escape'){event.preventDefault();closeEditor();}
  if(event.key==='Tab'){
    const nodes=[...$('#editor').querySelectorAll('button,input,select')].filter(node=>!node.disabled && node.getClientRects().length);
    const first=nodes[0],last=nodes.at(-1);
    if(event.shiftKey && document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first.focus();}
  }
});
document.addEventListener('click',event=>{
  const tab=event.target.closest('[data-tab]');if(tab)setTab(tab.dataset.tab);
  const remove=event.target.closest('[data-remove-default]');if(remove){delete draft.defaults[remove.dataset.removeDefault];render();toast('已从草稿移除默认配置');}
  const edit=event.target.closest('[data-edit]');if(edit)openEditor(edit.dataset.edit);
  const example=event.target.closest('[data-example]');if(example){$('#command').value=example.dataset.example;renderPreview();}
});
$('#add-preset').addEventListener('click',()=>openEditor());$('#add-empty').addEventListener('click',()=>openEditor());
$('#default-cards').addEventListener('change',event=>{
  const {defaultAgent:agent,field}=event.target.dataset;if(!agent)return;
  if(field==='model') {
    if(!event.target.value)delete draft.defaults[agent];
    else draft.defaults[agent]={model:event.target.value,effort:null};
  } else if(draft.defaults[agent]) draft.defaults[agent].effort=event.target.value || null;
  render();$(`#default-${agent}-${field}`).focus();
});
$('#current-agent').addEventListener('change',renderPreview);$('#command').addEventListener('input',renderPreview);$('#preview-command').addEventListener('click',renderPreview);
$('#save').addEventListener('click',async()=>{
  busy=true;updateSaveState();$('#save').textContent='正在保存…';
  try{const result=await api('/api/as/save',{settings:draft,revision,initializing});acceptSave(result.settingsReport);$('#app-error').hidden=true;toast(dirty()?'本次提交已保存，后续编辑仍需保存':'配置已保存，下次调用生效');}
  catch(error){reportError(error);}finally{busy=false;$('#save').textContent='保存配置';updateSaveState();}
});
$('#discard').addEventListener('click',()=>{draft=copy(saved);editorDrafts={};initializing=false;render();toast('已放弃未保存的变更');});
$('#refresh-catalog').addEventListener('click',async()=>{
  try{const result=await api('/api/as/catalog',{});catalog=result.catalog;renderAgents();render();toast('模型目录已刷新，草稿已保留');}catch(error){reportError(error);}
});
async function activity(event){
  if(!event.isTrusted||!csrf||expired||Date.now()-lastActivity<15000)return;
  lastActivity=Date.now();try{updateLifetime(await api('/api/activity',{}));}catch(error){if(error.status===410)reportError(error);}
}
['pointerdown','keydown','input','scroll'].forEach(name=>document.addEventListener(name,activity,{passive:true,capture:true}));
window.addEventListener('beforeunload',event=>{if(draft&&dirty()){event.preventDefault();event.returnValue='';}});
(async()=>{
  try{
    const token=location.hash.slice(1);if(token){const auth=await api('/api/session',{token});csrf=auth.csrf;history.replaceState(null,'',location.pathname);}
    await load();
    setInterval(async()=>{if(expired)return;try{updateLifetime(await api('/api/lifetime'));}catch(error){if(error.status===410)reportError(error);}},30000);
  }catch(error){reportError(error);$('#save-state').textContent='请通过有效预览链接打开';}
})();
