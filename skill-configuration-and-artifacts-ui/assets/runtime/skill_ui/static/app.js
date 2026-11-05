const root = document.querySelector('#app');
const e = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let state, csrf = '', ended = false, pending = {}, revealed = {}, localErrors = {}, remaining = 0, lastActivity = 0, toastTimer;
let collectionScroll = history.state?.collectionScroll || 0;
const eye = shown => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>${shown?'<path d="m3 3 18 18"/>':''}</svg>`;
const renderers = new Map();
const params = () => new URLSearchParams(location.search);
const value = (key, fallback = '') => params().get(key) ?? fallback;
const resourceURL = (id, download = false) => '/resources/' + encodeURIComponent(id) + (download ? '?download=1' : '');
function safeURL(url) { try { const result = new URL(url); return ['http:', 'https:'].includes(result.protocol) ? result.href : ''; } catch { return ''; } }
function toast(message) { clearTimeout(toastTimer); const box = document.querySelector('#toast'); box.textContent = message; box.classList.add('show'); toastTimer = setTimeout(() => box.classList.remove('show'), 2600); }
async function api(path, data) {
  const options = data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)};
  const response = await fetch(path, {...options, cache:'no-store'});
  const body = await response.json();
  if (!response.ok) { const error = new Error(body.message || '请求失败'); error.status = response.status; throw error; }
  return body;
}
function fail(error) {
  if (error.status === 410 || error.status === 401) ended = true;
  root.innerHTML = `<div class="shell"><div class="empty"><h1>${ended ? '页面已结束' : '暂时无法读取'}</h1><p>${e(error.message)}</p>${ended ? '<p>请让 Agent 重新打开此页面。已保存的配置和产物仍然保留。</p>' : '<button data-action="reload">重试</button>'}</div></div>`;
  root.setAttribute('aria-busy','false');
}
function life(data) { state.lifetime = data; remaining = data.remaining; tick(false); }
function tick(decrement = true) {
  if (!state || ended) return;
  if (!state.lifetime.resident && decrement) remaining = Math.max(0, remaining - 1);
  const text = state.lifetime.resident ? '本次服务常驻' : `${Math.floor(remaining/60)}:${String(remaining%60).padStart(2,'0')} 后到期`;
  const node = document.querySelector('#countdown'); if (node) node.textContent = text;
}
function header() {
  const artifacts = state.page === 'artifacts';
  return `<header class="topbar"><div class="identity"><span class="symbol" aria-hidden="true">${artifacts?'▧':'⚙'}</span><div><strong>${e(state.title)}</strong><small>当前项目 · <code>${e(state.project)}</code></small></div></div><div class="session"><div class="session-line"><strong id="countdown"></strong>${artifacts?`<label><input id="residency" type="checkbox" ${state.lifetime.resident?'checked':''}>常驻</label>`:''}<button class="text-button" data-action="stop">结束页面</button></div><small>${artifacts?'常驻会持续占用系统资源；仅对本次服务有效。':'实际操作会延长使用时间，保存后可结束页面。'}</small></div></header>${artifacts&&state.lifetime.resident?'<div class="notice">本次产物服务正在常驻，会持续占用系统资源。取消常驻后重新开始 60 分钟空闲倒计时。</div>':''}`;
}
function footer() { return '<footer class="footer"><span>仅查看当前项目 · 结束页面不会删除数据或停止业务任务</span><span>Skill UI</span></footer>'; }
function navigate(changes, replace = false) {
  history.replaceState({...history.state,scroll:window.scrollY}, '', location.href);
  const url = new URL(location.href);
  Object.entries(changes).forEach(([key,val]) => val == null || val === '' ? url.searchParams.delete(key) : url.searchParams.set(key,val));
  history[replace?'replaceState':'pushState']({scroll:0,collectionScroll},'',url);
  render(); window.scrollTo(0,0);
}
function effective(field) { const edit = pending[field.id]; return edit ? (edit.op === 'unset' ? field.default : edit.value) : field.value; }
function active(field) { return Object.entries(field.depends_on || {}).every(([id, expected]) => effective(state.config.fields.find(f=>f.id===id)||{}) === expected); }
function missing(field) { return active(field) && field.required && (field.sensitive ? !field.configured : field.value == null || field.value === '' || (Array.isArray(field.value)&&!field.value.length)); }
function fieldHTML(field) {
  const id = field.id, edit = pending[id], type = field.type || 'text', disabled = field.readonly ? 'disabled' : '';
  let control, current = effective(field);
  if (field.sensitive) {
    const editing = edit?.op === 'set', shown = revealed[id];
    control = `<div class="secret-row"><input data-field="${e(id)}" id="field-${e(id)}" type="${shown!==undefined?'text':'password'}" value="${e(editing?edit.value:(shown??''))}" placeholder="${e(edit?.op==='unset'?'保存后清除显式值':field.mask)}" autocomplete="new-password" ${disabled}><button type="button" data-action="reveal" data-id="${e(id)}" aria-label="${shown!==undefined?'隐藏':'查看'}${e(field.label)}原值" ${edit?.op==='unset'?'disabled':''}>${eye(shown!==undefined)}</button></div>`;
  } else if (type === 'boolean') control = `<input id="field-${e(id)}" data-field="${e(id)}" type="checkbox" ${current?'checked':''} ${disabled}>`;
  else if (type === 'select') control = `<select id="field-${e(id)}" data-field="${e(id)}" ${disabled}>${(field.choices||[]).map(option=>`<option value="${e(option)}" ${option===current?'selected':''}>${e(option)}</option>`).join('')}</select>`;
  else if (type === 'list' || type === 'json') control = `<textarea id="field-${e(id)}" data-field="${e(id)}" ${disabled}>${e(type==='list'?(current||[]).join('\n'):JSON.stringify(current??null,null,2))}</textarea>`;
  else control = `<input id="field-${e(id)}" data-field="${e(id)}" type="${type==='number'?'number':'text'}" value="${e(current)}" ${disabled}>`;
  return `<div class="field ${field.readonly?'readonly':''}"><div class="field-label"><label for="field-${e(id)}">${e(field.label)} ${field.required?'<span class="required">*</span>':''}</label><span class="state">${field.readonly?'只读':field.explicit?'已设置':'默认 / 继承'}</span><small>${e(field.help)}</small></div><div class="field-control">${control}<small>${e(field.effect)}${type==='list'?' 每行一项，顺序会被保留。':''}</small><small>${e(field.scope)} · ${e(field.path)}</small>${!field.readonly?`<div class="field-actions"><button class="text-button small" data-action="clear" data-id="${e(id)}">清除显式值</button>${edit?`<button class="text-button small" data-action="undo" data-id="${e(id)}">撤销修改</button>`:''}<span>${e(field.clear_help||'清除后使用默认值或继承值。')}</span></div>`:''}<div id="error-${e(id)}" class="error">${e(localErrors[id])}</div></div></div>`;
}
function configuration() {
  const required = value('mode','all') === 'required', advanced = value('advanced') === '1';
  const fields = state.config.fields.filter(f=>active(f) && (required?missing(f):advanced||!f.advanced));
  const groups = [...new Set(fields.map(f=>f.group||'配置'))];
  return `<section class="content config-content"><div class="heading"><div><div class="eyebrow">CONFIGURATION</div><h1>${required?'补齐必要配置':'全部配置'}</h1><p>${required?'填写当前任务缺少的必要信息，保存后由 Agent 继续处理。':'查看和调整这个 Skill 或 Plugin 的持久设置。'}</p></div><button data-action="reload">重新载入</button></div><div class="toolbar config-tabs"><div class="segmented"><button data-action="mode" data-mode="required" class="${required?'active':''}">补齐必要配置</button><button data-action="mode" data-mode="all" class="${!required?'active':''}">全部配置</button></div>${!required?`<label class="advanced-option"><input type="checkbox" id="advanced" ${advanced?'checked':''}>显示高级项</label>`:''}</div>${state.config.note?`<p class="muted">${e(state.config.note)}</p>`:''}<div id="fields">${groups.map(group=>`<section class="group"><h2>${e(group)}</h2>${fields.filter(f=>(f.group||'配置')===group).map(fieldHTML).join('')}</section>`).join('')||'<div class="empty"><h2>必要配置已齐全</h2><p>可切换到全部配置查看和修改其他设置。</p></div>'}</div><div class="save-box"><h3>本次修改</h3><div id="summary"></div><div id="save-message" role="status"></div><div class="save-line"><small>校验：${e(state.config.validation||'由业务适配器执行')}<br>编辑不会自动保存。</small><button class="primary" data-action="save" id="save">保存</button></div></div><div id="continuation"></div></section>`;
}
function summary() {
  if (state.page!=='configuration') return;
  const keys = Object.keys(pending), node = document.querySelector('#summary');
  if (!node) return;
  node.innerHTML = keys.length?`<ul>${keys.map(id=>{const f=state.config.fields.find(f=>f.id===id);return `<li>${e(f.label)}：${pending[id].op==='unset'?'清除显式值':'修改'} · ${e(f.scope)} · ${e(f.path)}${f.effect?' · '+e(f.effect):''}</li>`}).join('')}</ul>`:'<p class="muted">暂无修改。</p>';
  document.querySelector('#save').disabled = !keys.length || Object.keys(localErrors).length>0;
}
const kinds = {document:'文档',image:'图片',audio:'音频',video:'视频',table:'表格',remote:'远端资源',receipt:'操作回执',file:'文件',comparison:'对照'};
function filtered() { const query=value('q').toLocaleLowerCase(); return (state.artifacts.items||[]).filter(a=>(!value('type')||a.kind===value('type'))&&(!value('task')||a.task===value('task'))&&(!query||[a.title,a.summary,a.task].join(' ').toLocaleLowerCase().includes(query))); }
function card(a) {
  const preview=a.preview||{}, visual=preview.type==='image'?`<img loading="lazy" src="${resourceURL(preview.resource)}" alt="">`:preview.type==='markdown'||preview.type==='text'?`<div class="paper"><strong>${e(a.title)}</strong>${e((preview.text||'').slice(0,140))}</div>`:({audio:'♫',video:'▷',table:'▦',remote:'↗',receipt:'✓',comparison:'▥'}[a.kind]||'▧');
  return `<article class="artifact-card"><button class="card-open" data-action="open" data-id="${e(a.id)}"><div class="card-visual">${visual}</div><div class="card-info"><h3>${e(a.title)}</h3><p>${e(a.summary||kinds[a.kind]||'产物')}</p></div></button><div class="card-bottom"><span>${e(a.task||a.status||'当前项目')}</span><button class="text-button small" data-action="open" data-id="${e(a.id)}">查看 ↗</button></div></article>`;
}
function collection() {
  const items=state.artifacts.items||[], types=[...new Set(items.map(a=>a.kind).filter(Boolean))], tasks=[...new Set(items.map(a=>a.task).filter(Boolean))], list=value('layout')==='list';
  return `<section class="content"><div class="heading"><div><div class="eyebrow">YOUR ARTIFACTS</div><h1>产物集合</h1><p>${e(state.artifacts.note||'在这里查看、预览和取用当前项目的成果。')}</p></div><button data-action="reload">刷新</button></div><div class="toolbar"><input id="search" type="search" placeholder="搜索产物…" aria-label="搜索产物" value="${e(value('q'))}"><select id="type-filter" aria-label="按类型筛选"><option value="">全部类型</option>${types.map(type=>`<option value="${e(type)}" ${type===value('type')?'selected':''}>${e(kinds[type]||type)}</option>`).join('')}</select><select id="task-filter" aria-label="按任务筛选"><option value="">全部任务</option>${tasks.map(task=>`<option ${task===value('task')?'selected':''}>${e(task)}</option>`).join('')}</select><div class="segmented"><button data-action="layout" data-layout="cards" class="${!list?'active':''}">卡片</button><button data-action="layout" data-layout="list" class="${list?'active':''}">列表</button></div></div><div class="count">${filtered().length} 件产物</div><div id="collection" class="${list?'list':'grid'}">${filtered().map(card).join('')||'<div class="empty"><h2>没有找到产物</h2><p>调整筛选，或在任务完成后刷新。</p></div>'}</div></section>`;
}
function markdown(text) { return String(text||'').split(/\n\s*\n/).map(block=>{const heading=block.match(/^(#{1,3})\s+([^\n]+)$/);if(heading)return `<h${heading[1].length}>${e(heading[2])}</h${heading[1].length}>`;return `<p>${e(block)}</p>`;}).join(''); }
function previewHTML(a) {
  const p=a.preview||{};
  if (p.type==='markdown') return `<article class="prose">${markdown(p.text)}</article>`;
  if (p.type==='text') return `<pre class="raw-text">${e(p.text)}</pre>`;
  if (p.type==='image') return `<figure><img src="${resourceURL(p.resource)}" alt="${e(a.title)}"></figure>`;
  if (p.type==='audio'||p.type==='video') return `<${p.type} controls preload="metadata" src="${resourceURL(p.resource)}"></${p.type}>`;
  if (p.type==='table') return `<div class="table-scroll"><table><thead><tr>${(p.columns||[]).map(col=>`<th>${e(col)}</th>`).join('')}</tr></thead><tbody>${(p.rows||[]).map(row=>`<tr>${row.map(cell=>`<td>${e(cell)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  if (p.type==='comparison') return `<div class="comparison">${(p.panels||[]).map(panel=>`<article><h3>${e(panel.title)}</h3><div class="prose">${markdown(panel.text)}</div></article>`).join('')}</div>`;
  if (p.type==='custom') return '<div id="custom-preview"></div>';
  return `<div class="empty"><h2>${e(a.status||'已记录')}</h2><p>${e(a.summary||'此类型没有内置预览，可使用适用的打开或下载操作。')}</p></div>`;
}
function detail() {
  const a=(state.artifacts.items||[]).find(a=>a.id===value('id'));
  if (!a) return '<div class="empty"><h2>这件产物已不可用</h2><button data-action="back">返回产物集合</button></div>';
  const sidebar=value('sidebar','1')!=='0', info=value('info')==='1', link=safeURL(a.url), copy=a.copy_text??a.preview?.text;
  return `<nav class="viewer-nav"><button class="text-button" data-action="back">← 产物集合</button><button class="text-button sidebar-toggle" data-action="sidebar">${sidebar?'收起':'展开'}集合侧栏</button><span>${e(kinds[a.kind]||'产物')}</span></nav><div class="viewer-body">${sidebar?`<aside class="sidebar">${filtered().map(item=>`<button data-action="open" data-id="${e(item.id)}" class="${a.id===item.id?'active':''}">${e(item.title)}</button>`).join('')}</aside>`:''}<section class="viewer"><header class="preview-toolbar"><div><h2>${e(a.title)}</h2><small>${e(a.task||a.status||'当前项目')}</small></div><div class="actions">${copy!==undefined?'<button data-action="copy">复制内容</button>':''}${link?`<a class="button" href="${e(link)}" target="_blank" rel="noopener noreferrer">打开 ↗</a>`:''}${a.resource?`<a class="button" href="${resourceURL(a.resource,true)}" download>下载</a>`:''}<button data-action="info" aria-label="${info?'隐藏':'查看'}产物信息">ⓘ 信息</button></div></header><div class="preview">${previewHTML(a)}</div></section>${info?`<aside class="metadata"><h3>产物信息</h3><dl>${Object.entries(a.metadata||{}).map(([key,val])=>`<dt>${e(key)}</dt><dd>${e(typeof val==='object'?JSON.stringify(val):val)}</dd>`).join('')||'<dd>没有附加信息。</dd>'}</dl></aside>`:''}</div>`;
}
function render() {
  if (ended||!state) return;
  const detailView=state.page==='artifacts'&&value('view')==='detail';
  root.innerHTML=`<div class="shell">${header()}${state.page==='configuration'?configuration():detailView?detail():collection()}${footer()}</div>`;
  root.setAttribute('aria-busy','false'); tick(false); summary();
  if (detailView) { const artifact=state.artifacts.items.find(a=>a.id===value('id')), node=document.querySelector('#custom-preview'); if (node) { const renderer=renderers.get(artifact.preview.renderer); if(renderer) { try { renderer(node,artifact.preview.data,{resourceURL}); } catch { node.textContent='定制预览失败，请检查适配器。'; } } else node.textContent='这个产物的定制预览未加载。'; } }
}
async function load() {
  const next=await api('/api/state'); state=next;csrf=state.csrf;remaining=state.lifetime.remaining;
  if (state.page==='artifacts') { for(const name of state.extensions||[]) { const extension=await import('/extensions/'+encodeURIComponent(name)); if(typeof extension.register==='function')extension.register((name,renderer)=>renderers.set(name,renderer)); } if(state.artifacts.items?.length===1&&!params().has('view')){const url=new URL(location.href);url.searchParams.set('view','detail');url.searchParams.set('id',state.artifacts.items[0].id);history.replaceState({},'',url);} }
  document.title=state.title+' · '+(state.page==='artifacts'?'产物集合':'配置中心');render();
}
root.addEventListener('click', async event=>{
  const button=event.target.closest('[data-action]'); if(!button||ended)return;
  const action=button.dataset.action,id=button.dataset.id;
  try {
    if(action==='reload'){if(Object.keys(pending).length&&!confirm('重新载入会丢弃尚未保存的修改，是否继续？'))return;pending={};revealed={};localErrors={};await load();}
    if(action==='mode')navigate({mode:button.dataset.mode});
    if(action==='layout')navigate({layout:button.dataset.layout});
    if(action==='open'){if(value('view')!=='detail')collectionScroll=window.scrollY;navigate({view:'detail',id});}
    if(action==='back'){navigate({view:'collection',id:null,info:null});window.scrollTo(0,collectionScroll);}
    if(action==='sidebar')navigate({sidebar:value('sidebar','1')==='1'?'0':'1'},true);
    if(action==='info')navigate({info:value('info')==='1'?null:'1'},true);
    if(action==='clear'){pending[id]={op:'unset'};delete revealed[id];delete localErrors[id];render();}
    if(action==='undo'){delete pending[id];delete revealed[id];delete localErrors[id];render();}
    if(action==='reveal'){if(revealed[id]!==undefined)delete revealed[id];else if(pending[id]?.op==='set')revealed[id]=pending[id].value;else revealed[id]=(await api('/api/reveal',{id,revision:state.config.revision})).value;render();}
    if(action==='copy'){const a=state.artifacts.items.find(a=>a.id===value('id'));await navigator.clipboard.writeText(a.copy_text??a.preview.text);toast('已复制');}
    if(action==='stop'){await api('/api/stop',{});ended=true;fail({status:410,message:'此页面服务已结束。'});}
    if(action==='save'){
      button.disabled=true;
      try {const result=await api('/api/save',{revision:state.config.revision,changes:pending});state.config=result.config;pending={};revealed={};localErrors={};render();document.querySelector('#save-message').innerHTML='<p class="success">已保存；'+e(result.receipt.validation)+'通过。</p>';if(result.config.task?.continuation){document.querySelector('#continuation').innerHTML=`<div class="continuation">若 Agent 会话已结束，可复制发送：<p>${e(result.config.task.continuation)}</p><button data-action="copy-continuation">复制继续请求</button></div>`;}}
      catch(error){document.querySelector('#save-message').innerHTML=`<p class="error">${e(error.message)}</p>`;summary();}
    }
    if(action==='copy-continuation'){await navigator.clipboard.writeText(state.config.task.continuation);toast('已复制');}
  }catch(error){if(['reload'].includes(action)||error.status===410||error.status===401)fail(error);else toast(error.message);}
});
root.addEventListener('input',event=>{
  const input=event.target;
  if(input.id==='search'){
    const url=new URL(location.href);input.value?url.searchParams.set('q',input.value):url.searchParams.delete('q');
    history.replaceState({...history.state,scroll:window.scrollY},'',url);
    document.querySelector('#collection').innerHTML=filtered().map(card).join('')||'<div class="empty"><h2>没有找到产物</h2><p>调整筛选，或在任务完成后刷新。</p></div>';
    document.querySelector('.count').textContent=filtered().length+' 件产物';return;
  }
  if(!input.dataset.field)return;
  const field=state.config.fields.find(f=>f.id===input.dataset.field);let parsed;
  try{if(field.type==='boolean')parsed=input.checked;else if(field.type==='number'){if(input.value.trim()===''||!Number.isFinite(Number(input.value)))throw Error();parsed=Number(input.value);}else if(field.type==='list')parsed=input.value.split('\n').filter(line=>line!=='');else if(field.type==='json')parsed=JSON.parse(input.value);else parsed=input.value;pending[field.id]={op:'set',value:parsed};delete localErrors[field.id];}
  catch{localErrors[field.id]='请输入有效的'+(field.type==='json'?'JSON':'数值')+'。';}
  document.getElementById('error-'+field.id).textContent=localErrors[field.id]||'';summary();
});
root.addEventListener('change',async event=>{
  const input=event.target;
  if(input.id==='advanced')navigate({advanced:input.checked?'1':null});
  if(input.id==='type-filter')navigate({type:input.value});
  if(input.id==='task-filter')navigate({task:input.value});
  if(input.id==='residency'){try{life(await api('/api/residency',{resident:input.checked}));render();}catch(error){input.checked=!input.checked;toast(error.message);}}
  if(input.dataset.field&&state.config.fields.some(f=>Object.keys(f.depends_on||{}).includes(input.dataset.field)))render();
});
window.addEventListener('popstate',()=>{render();window.scrollTo(0,history.state?.scroll||0);});
window.addEventListener('beforeunload',event=>{if(Object.keys(pending).length){event.preventDefault();event.returnValue='';}});
for(const event of ['pointerdown','keydown','input','scroll'])window.addEventListener(event,()=>{if(ended||!state||Date.now()-lastActivity<15000)return;lastActivity=Date.now();api('/api/activity',{}).then(life).catch(error=>{if(error.status===410||error.status===401)fail(error);});},{passive:true});
setInterval(()=>tick(),1000);
setInterval(()=>{if(state&&!ended)api('/api/lifetime').then(life).catch(error=>{if(error.status===410||error.status===401||(!state.lifetime.resident&&remaining===0))fail({status:410,message:'页面访问已到期，请让 Agent 重新打开。'});else toast('暂时无法连接页面服务，请检查服务状态。');});},15000);
try{const token=location.hash.slice(1);if(token){await api('/api/session',{token});history.replaceState({},'',location.pathname+location.search);}await load();}catch(error){fail(error);}
