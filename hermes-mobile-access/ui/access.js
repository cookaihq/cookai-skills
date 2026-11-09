// Connection access controls; fixed UI core and business loading remain intact.
const accessBaseFieldHTML = fieldHTML;
const accessFields = () => (state?.config.fields || []).filter(f => /^(http_|websocket_|access_websocket_enabled$)/.test(f.id));
function accessCheckbox(field, title) {
  const current = effective(field), edit = pending[field.id];
  const master = state.config.fields.find(f => f.id === 'access_websocket_enabled');
  const disabled = field.readonly || (field.id.startsWith('websocket_') && !effective(master));
  return `<div class="access-protocol"><label><input id="field-${e(field.id)}" data-field="${e(field.id)}" type="checkbox" aria-label="${e(field.label)}" ${current?'checked':''} ${disabled?'disabled':''}><strong>${e(title)}</strong><span class="access-state">${current?'开启':'关闭'}${edit?' · 未保存':field.explicit?' · 已设置':' · 默认'}</span></label>
    <code class="access-key">${e(state.config.task.field_paths[field.id])}</code>
    <div class="access-edit"><button type="button" class="text-button small" data-action="clear" data-id="${e(field.id)}" ${disabled?'disabled':''}>恢复默认</button>${edit?`<button type="button" class="text-button small" data-action="undo" data-id="${e(field.id)}">撤销</button>`:''}</div>
    <div id="error-${e(field.id)}" class="error">${e(localErrors[field.id]||'')}</div></div>`;
}
fieldHTML = function(field) {
  if (!state.config.task?.access_groups) return accessBaseFieldHTML(field);
  if (field.id.startsWith('websocket_')) return '';
  if (field.id === 'access_websocket_enabled') {
    return `<div class="access-master">${accessCheckbox(field,'允许 WebSocket 连接')}<p>${e(field.help)}</p></div>`;
  }
  if (!field.id.startsWith('http_')) return accessBaseFieldHTML(field);
  const group = state.config.task.access_groups.find(g=>g.id===field.id.slice(5));
  const ws = state.config.fields.find(f=>f.id==='websocket_'+group.id);
  return `<article class="access-card"><h3>${e(group.label)}</h3><p>${e(group.help)}</p>${accessCheckbox(field,'HTTP')}${accessCheckbox(ws,'WebSocket')}
    <details><summary>接口范围</summary><p><strong>HTTP：</strong>${e(group.http)}</p><p><strong>WebSocket：</strong>${e(group.websocket)}</p></details></article>`;
};
function decorateAccess() {
  if (!state || ended || state.page!=='configuration' || !state.config.task?.access_groups) return;
  const panel = [...document.querySelectorAll('.group')].find(n=>n.querySelector('h2')?.textContent==='访问范围');
  if (panel && !panel.querySelector('.access-intro')) {
    const intro=document.createElement('div'); intro.className='access-intro';
    intro.innerHTML=`<div class="access-toolbar"><strong id="access-count"></strong><div class="actions"><button type="button" data-access-bulk="on">全部开启</button><button type="button" data-access-bulk="off">全部关闭</button></div></div>
      <p>每组分别勾选 HTTP 和 WebSocket，默认全部开启；未设置的项使用默认值。</p>
      <p>HTTP 控制读取及修改；WebSocket 控制 RPC、实时推送及重连回放。开启 WebSocket 时保留连接就绪和心跳。</p>
      <p>保存后应用到当前连接；权限变化会关闭旧连接并重新连接。</p>
      <p>这些设置控制手机直接接口。聊天中的 Agent 工具权限仍由 Hermes 管理；通用命令归入“其他接口与通用命令”。</p>
      <small>保存范围：当前连接 · <code>${e(state.config.task.access_target)}</code></small>`;
    panel.querySelector('h2').after(intro);
    const cards=document.createElement('div');cards.className='access-grid';
    [...panel.querySelectorAll('.access-card')].forEach(n=>cards.append(n));panel.append(cards);
  }
  const count=document.querySelector('#access-count');
  if(count) count.textContent=`HTTP ${accessFields().filter(f=>f.id.startsWith('http_')&&effective(f)).length}/12 · WebSocket ${accessFields().filter(f=>f.id.startsWith('websocket_')&&effective(f)).length}/12`;
  for(const field of accessFields()) {
    const input=document.getElementById('field-'+field.id);if(!input)continue;
    const master=state.config.fields.find(f=>f.id==='access_websocket_enabled');
    if(field.id.startsWith('websocket_')) input.disabled=!effective(master)||field.readonly;
    input.closest('.access-protocol').querySelector('.access-state').textContent=`${effective(field)?'开启':'关闭'}${pending[field.id]?' · 未保存':field.explicit?' · 已设置':' · 默认'}`;
  }
  const node=document.querySelector('#summary');
  if(node) for(const item of node.querySelectorAll('li')) {
    const field=accessFields().find(f=>item.textContent.startsWith(f.label+'：'));
    if(field) item.textContent=`${field.label}：${field.value?'开启':'关闭'} → ${effective(field)?'开启':'关闭'} · 当前连接 · ${field.path}`;
  }
}
const accessBaseRender=render;
render=function(){accessBaseRender();decorateAccess();};
const accessBaseSummary=summary;
summary=function(){accessBaseSummary();decorateAccess();};
root.addEventListener('input',event=>{
  if(!accessFields().some(f=>f.id===event.target.dataset.field))return;
  const field=accessFields().find(f=>f.id===event.target.dataset.field);
  if(event.target.checked===field.value)delete pending[field.id];
  // Keep keyboard focus on the same checkbox after updating dependent controls.
  const id=event.target.id;render();document.getElementById(id)?.focus({preventScroll:true});
});
root.addEventListener('click',event=>{
  const button=event.target.closest('[data-access-bulk]');if(!button||ended)return;
  const enabled=button.dataset.accessBulk==='on';
  for(const field of accessFields()){
    if(field.readonly)continue;
    if(field.value===enabled)delete pending[field.id];else pending[field.id]={op:'set',value:enabled};
    delete localErrors[field.id];
  }
  render();
});
render();
