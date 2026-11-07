const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {project:'项目覆盖', global:'继承全局', process:'临时覆盖', default:'字段默认', missing:'缺失', optional:'未设置', error:'读取错误', invalid:'配置错误', custom:'显式设置'};
const rowLabels = {effective:'当前生效', shadowed:'被更高层覆盖', empty:'空值', missing:'未设置', skipped:'本次未启用', error:'读取错误'};
const show = (f, r) => f.sensitive ? r.mask || '未填写' : r.value == null ? '未设置' : r.value === '' ? '空值' : typeof r.value === 'object' ? JSON.stringify(r.value) : String(r.value);

export class Configuration {
  constructor({root, api, render, reload, toast}) {
    Object.assign(this, {root, api, render, reload, toast});
    this.drafts = {}; this.inputs = {}; this.errors = {}; this.revealed = {};
    this.selected = ''; this.target = ''; this.opened = false; this.filter = 'all';
    this.tab = 'edit'; this.preview = null; this.generation = 0; this.busy = false;
    this.message = ''; this.error = ''; this.expanded = false;
    root.addEventListener('click', ev => this.onClick(ev));
    root.addEventListener('input', ev => this.onInput(ev));
    root.addEventListener('change', ev => {
      if (ev.target.id === 'config-file') this.selectTarget(ev.target.value);
      if (ev.target.id === 'draft-file') { const target = ev.target.value; this.selected = Object.keys(this.drafts[target])[0]; this.selectTarget(target); }
    });
    document.addEventListener('keydown', ev => this.onKey(ev));
  }
  set(snapshot) {
    this.snapshot = snapshot;
    this.invalidate(); this.revealed = {}; this.error = ''; this.message = '';
  }
  invalidate() { clearTimeout(this.timer); this.generation++; this.preview = null; }
  count() { return Object.values(this.drafts).reduce((n, d) => n + Object.keys(d).length, 0); }
  fields() { return this.snapshot.fields; }
  field(id = this.selected) { return this.fields().find(f => f.id === id); }
  layer(id = this.target) { return this.snapshot.layers.find(l => l.id === id); }
  changes() { return this.drafts[this.target] || {}; }
  row(f = this.field(), target = this.target) { return f.rows.find(r => r.layer === target) || {}; }
  after(f = this.field()) { return this.preview?.fields.find(n => n.id === f.id) || f; }
  category(f) { const r = f.effective; return ['error','invalid','missing','optional','default'].includes(r.status) ? r.status : this.layer(r.source)?.scope || 'custom'; }
  source(r) { return r.source === 'default' ? '字段默认值' : this.layer(r.source)?.name || '无可用来源'; }
  sourceHTML(f) { return `<span class="source-tag ${esc(this.category(f))}">${esc(labels[this.category(f)] || '显式设置')}</span>`; }
  targets(f = this.field()) { return this.snapshot.layers.filter(l => !l.readonly && f.targets?.includes(l.id)); }
  preferred(scope, f = this.field()) {
    const choices = this.targets(f).filter(l => !scope || l.scope === scope);
    return choices.find(l => l.id === f.effective.source)?.id || choices.find(l => l.id === this.snapshot.default_targets?.[scope])?.id || choices[0]?.id || '';
  }
  html() {
    const params = new URLSearchParams(location.search), required = params.get('mode') === 'required', advanced = params.get('advanced') === '1';
    const eligible = this.fields().filter(f => this.after(f).active !== false && (required ? f.required : advanced || !f.advanced));
    const fields = eligible.filter(f => this.filter === 'all' || this.category(f) === this.filter);
    const project = eligible.filter(f => this.category(f) === 'project'), global = eligible.filter(f => this.category(f) === 'global');
    const scopes = new Set(this.snapshot.layers.map(l=>l.scope));
    const sourceFilters = [['all','全部来源'],...(scopes.has('project')?[['project','项目覆盖']]:[]),...(scopes.has('global')?[['global','继承全局']]:[])];
    const overview = !scopes.has('global') ? `当前显示 ${eligible.length} 项配置，按每项标记查看实际来源。` : project.length === 1 && global.length === eligible.length - 1 ? `此项目仅覆盖 ${project[0].label}，其余 ${global.length} 项继承全局。` : `当前显示 ${eligible.length} 项配置：${project.length} 项由项目设置，${global.length} 项继承全局。`;
    const groups = [...new Set(fields.map(f => f.group || '配置'))];
    return `<section class="content layers-content"><div class="config-main" ${this.opened ? 'inert' : ''}>
      <div class="heading"><div><div class="eyebrow">CONFIGURATION</div><h1>当前生效配置</h1><p>先看这个项目正在使用什么，再按需调整。</p></div><button data-config="reload">重新载入</button></div>
      <div class="config-overview"><strong>${esc(overview)}</strong><p>各项配置分别读取；项目仅覆盖已设置的项。</p></div>
      <div class="toolbar"><div class="segmented"><button data-action="mode" data-mode="required" class="${required?'active':''}">必要配置</button><button data-action="mode" data-mode="all" class="${!required?'active':''}">全部配置</button></div>${!required?`<label class="advanced-option"><input type="checkbox" id="advanced" ${advanced?'checked':''}>显示高级项</label>`:''}</div>
      <div class="source-filters">${sourceFilters.map(([id,label])=>`<button data-config="filter" data-filter="${id}" class="${this.filter===id?'active':''}">${label}<span>${id==='all'?eligible.length:eligible.filter(f=>this.category(f)===id).length}</span></button>`).join('')}</div>
      ${this.snapshot.note?`<p class="config-note">${esc(this.snapshot.note)}</p>`:''}
      ${groups.map(group=>`<section class="config-group"><h2>${esc(group)}</h2>${fields.filter(f=>(f.group||'配置')===group).map(f=>`<article class="effective-row"><div class="setting-name"><strong>${esc(f.label)}${f.required?'<span class="required"> *</span>':''}</strong><small>${esc(f.help)}</small></div><div class="effective-value"><code>${esc(show(f,f.effective))}</code>${f.effective.message?`<small class="error">${esc(f.effective.message)}</small>`:''}<small>${esc(this.source(f.effective))}</small></div><button class="source-button" data-config="trace" data-id="${esc(f.id)}">${this.sourceHTML(f)}</button><button data-config="edit" data-id="${esc(f.id)}">${this.targets(f).length?'编辑':'查看'}</button></article>`).join('')}</section>`).join('') || '<div class="empty">当前筛选下没有配置项。</div>'}
      ${this.count()?`<div class="draft-dock"><span>${this.count()} 项草稿尚未保存</span><button data-config="review">查看草稿</button><button data-config="discard">放弃全部</button></div>`:''}
      ${this.message?`<p class="success" role="status">${esc(this.message)}</p>`:''}
      ${this.snapshot.task?.continuation?`<details class="continuation"><summary>保存后继续任务</summary><p>${esc(this.snapshot.task.continuation)}</p><button data-action="copy-continuation">复制继续请求</button></details>`:''}
      </div>${this.opened?this.drawerHTML():''}</section>`;
  }
  drawerHTML() {
    const f=this.field(), l=this.layer(), writable=this.targets(), scopes=[...new Set(writable.map(l=>l.scope))];
    return `<button class="config-veil" tabindex="-1" data-config="close" aria-label="关闭编辑器"></button><aside class="config-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <header class="drawer-heading"><div><small>配置详情</small><h2 id="drawer-title">${esc(f.label)}</h2></div><button data-config="close" id="drawer-close" aria-label="关闭编辑器">✕</button></header>
      <div class="drawer-scroll"><div class="current-setting"><small>当前生效</small><div><strong>${esc(show(f,f.effective))}</strong>${this.sourceHTML(f)}</div><small>${esc(this.source(f.effective))}</small>${f.sensitive?`<button class="text-button" data-config="reveal" data-layer="effective">${this.revealed.effective!==undefined?'隐藏':'查看'}生效原值</button>${this.revealed.effective!==undefined?`<code>${esc(this.revealed.effective)}</code>`:''}`:''}</div>
      <div class="drawer-tabs"><button data-config="tab" data-tab="edit" class="${this.tab==='edit'?'active':''}">编辑设置</button><button data-config="tab" data-tab="source" class="${this.tab==='source'?'active':''}">来源与优先级</button></div>
      ${this.tab==='source'?this.traceHTML(f):`<p class="drawer-help">${esc(f.help || '选择保存范围，再修改此项配置。')}</p>${writable.length?`<label class="section-label">保存范围</label><div class="scope-buttons">${scopes.map(scope=>`<button data-config="scope" data-scope="${esc(scope)}" class="${l?.scope===scope?'active':''}">${esc({project:'仅此项目',global:'个人全局'}[scope]||scope)}</button>`).join('')}</div><div class="target-file"><strong>${esc(l?.name)}</strong><p>${esc(l?.scope_label)}</p><code>${esc(l?.path)}</code>${writable.length>1?`<details><summary>更改文件</summary><select id="config-file" aria-label="保存到哪个文件">${writable.map(layer=>`<option value="${esc(layer.id)}" ${layer.id===this.target?'selected':''}>${esc(layer.name)}${!layer.enabled?'（本次未启用）':''}</option>`).join('')}</select></details>`:''}</div>${!l?.enabled?'<p class="layer-warning">此层本次未启用，保存后不会改变当前生效配置。</p>':''}${this.controlHTML(f)}<div class="edit-actions"><button data-config="clear">移除此文件的设置</button><button data-config="empty">设为空值</button>${this.changes()[f.id]?'<button data-config="undo">撤销本项修改</button>':''}</div><p class="config-note">移除与空值的具体回退结果见下方预览。</p>`:'<p class="layer-warning">此字段只读。请在配置注入方修改。</p>'}`}
      </div><footer class="drawer-save"><div id="config-feedback" role="status"></div><div id="config-summary"></div><div class="drawer-save-actions"><span id="config-save-count"></span><button data-config="summary" aria-expanded="${this.expanded}">${this.expanded?'收起':'变更摘要'}</button><button class="primary" id="config-save" data-config="save">保存此文件</button></div></footer></aside>`;
  }
  traceHTML(f) {
    return `<p class="drawer-help">按业务加载器的实际顺序排列，优先级从高到低。整组绑定按完整记录读取。</p><ol class="layer-trace">${this.snapshot.layers.map(l=>{const r=this.row(f,l.id);return `<li class="${f.effective.source===l.id?'winning':''}"><div><strong>${esc(l.name)}</strong><span>${esc(rowLabels[r.status]||r.status)}</span></div><code>${esc(l.path)}</code><p>${esc(this.revealed[l.id] ?? show(f,r))}</p>${r.message?`<p class="error">${esc(r.message)}</p>`:''}<small>${esc(l.scope_label)}</small>${f.sensitive&&r.configured?`<button class="text-button" data-config="reveal" data-layer="${esc(l.id)}">${this.revealed[l.id]!==undefined?'隐藏':'查看'}原值</button>`:''}${this.targets(f).some(x=>x.id===l.id)?`<button class="text-button" data-config="target" data-target="${esc(l.id)}">编辑此文件</button>`:''}</li>`;}).join('')}<li><strong>全部来源之后</strong><p>按字段定义使用默认值，或报告缺失。</p></li></ol>`;
  }
  controlHTML(f) {
    const edit=this.changes()[f.id], row=this.row(f), saved=edit?.op==='unset'?null:edit?.op==='set'?edit.value:row.value;
    const raw=this.inputs[this.target]?.[f.id], revealed=this.revealed[this.target];
    let control;
    const base=`id="config-value" data-config-field="${esc(f.id)}"`;
    if(f.sensitive) control=`<div class="secret-row"><input ${base} type="${revealed!==undefined?'text':'password'}" value="${esc(edit?.op==='set'?edit.value:revealed??'')}" placeholder="${esc(edit?.op==='unset'?'将移除此文件的值':row.mask||'填写新值') }" autocomplete="new-password"><button data-config="reveal" data-layer="${esc(this.target)}">${revealed!==undefined?'隐藏':'查看'}原值</button></div>`;
    else if(f.type==='boolean') control=`<label class="boolean-control"><input ${base} type="checkbox" ${(saved??f.effective.value)?'checked':''}>启用</label>`;
    else if(f.type==='select') control=`<select ${base}><option value="">未设置／空值</option>${(f.choices||[]).map(c=>`<option value="${esc(c)}" ${c===saved?'selected':''}>${esc(c)}</option>`).join('')}</select>`;
    else if(['list','json'].includes(f.type)) control=`<textarea ${base}>${esc(raw??(f.type==='list'?(saved||[]).join('\n'):JSON.stringify(saved??null,null,2)))}</textarea>`;
    else control=`<input ${base} type="${f.type==='number'?'number':'text'}" value="${esc(raw??saved??'')}" placeholder="${esc(show(f,f.effective))}">`;
    return `<div class="config-control"><label for="config-value">${esc(row.present?'此文件的设置':'此文件尚未设置')}</label>${control}<small>${esc(f.effect)}${f.type==='list'?' 每行一项，保留顺序。':''}</small><p id="config-input-error" class="error">${esc(this.errors[this.target]?.[f.id])}</p></div>`;
  }
  afterRender() {
    this.root.querySelector('.topbar')?.toggleAttribute('inert',this.opened);
    this.root.querySelector('.footer')?.toggleAttribute('inert',this.opened);
    document.body.classList.toggle('config-editing',this.opened);
    this.paint();
  }
  paint() {
    if(!this.opened)return;
    const node=this.root.querySelector('#config-feedback'), f=this.field(), after=this.after(f), changes=this.changes(), count=Object.keys(changes).length;
    if(!node)return;
    let feedback=this.error?`<p class="error">${esc(this.error)}</p>`:this.message?`<p class="success">${esc(this.message)}</p>`:'';
    if(count && !this.error) {
      if(!this.preview) feedback+='<p>正在计算保存后的生效结果…</p>';
      else {
        feedback+=`<p>保存后：<strong>${esc(show(after,after.effective))}</strong> · ${esc(this.source(after.effective))}</p>`;
        if(changes[f.id] && after.effective.source!==this.target) feedback+=`<p class="layer-warning">${changes[f.id].op==='unset'?'移除此层设置后，使用上面所示来源。':'此文件会保存，但当前仍使用上面所示来源。'}</p>`;
        feedback+=(this.preview.validation_errors||[]).map(s=>`<p class="error">${esc(s)}</p>`).join('');
      }
    }
    node.innerHTML=feedback;
    const summary=this.root.querySelector('#config-summary');
    summary.innerHTML=this.expanded?`<div class="change-summary"><strong>本次只保存：${esc(this.layer()?.path)}</strong><ul>${Object.entries(changes).map(([id,change])=>{const field=this.field(id),next=this.after(field);return `<li>${esc(field.label)}：${change.op==='unset'?'移除设置':'修改'} → ${esc(show(next,next.effective))}（${esc(this.source(next.effective))}）</li>`;}).join('')}</ul><p>校验：${esc(this.snapshot.validation||'由业务适配器执行')}</p>${Object.keys(this.drafts).filter(id=>Object.keys(this.drafts[id]).length).length>1?`<label>切换其他文件草稿<select id="draft-file">${Object.entries(this.drafts).filter(([,d])=>Object.keys(d).length).map(([id])=>`<option value="${esc(id)}" ${this.target===id?'selected':''}>${esc(this.layer(id)?.name)}</option>`).join('')}</select></label>`:''}</div>`:'';
    this.root.querySelector('#config-save-count').textContent=`此文件 ${count} 项 · 共 ${this.count()} 项草稿`;
    this.root.querySelector('#config-save').disabled=this.busy||!count||!this.preview||!!this.error||Object.keys(this.errors[this.target]||{}).length>0||!!this.preview.validation_errors?.length;
  }
  queuePreview() {
    this.invalidate(); this.error=''; this.message=''; this.paint();
    if(!this.target||!Object.keys(this.changes()).length)return;
    if(Object.keys(this.errors[this.target]||{}).length){this.error='请先修正此文件草稿中的格式错误。';this.paint();return;}
    const generation=this.generation, target=this.target, revision=this.snapshot.revision, changes=structuredClone(this.changes());
    this.timer=setTimeout(async()=>{try{const result=await this.api('/api/preview',{target,revision,changes});if(generation!==this.generation)return;this.preview=result.config;this.paint();}catch(error){if(generation!==this.generation)return;this.error=error.message;this.paint();}},160);
  }
  selectTarget(target) { this.target=target;this.revealed={};this.invalidate();this.render();this.queuePreview(); }
  open(id, tab='edit', target) { this.selected=id;this.tab=tab;this.opened=true;this.target=target||this.preferred('project')||this.preferred('global')||this.preferred();this.revealed={};this.error='';this.invalidate();this.render();this.root.querySelector('#drawer-close')?.focus();this.queuePreview(); }
  close() { if(this.busy)return;this.opened=false;this.invalidate();this.revealed={};this.render();[...this.root.querySelectorAll('[data-config="edit"]')].find(b=>b.dataset.id===this.selected)?.focus(); }
  stage(change, raw) { (this.drafts[this.target]??={})[this.selected]=change; if(raw!==undefined)(this.inputs[this.target]??={})[this.selected]=raw;else delete (this.inputs[this.target]||{})[this.selected];this.queuePreview(); }
  onInput(event) {
    const input=event.target;if(!input.dataset.configField||this.busy)return;
    const f=this.field();let v=input.value;
    try {
      if(f.type==='number'&&v!==''){v=Number(v);if(!Number.isFinite(v))throw Error();}
      if(f.type==='boolean')v=input.checked;
      if(f.type==='list')v=input.value.split('\n').filter(line=>line!=='');
      if(f.type==='json')v=JSON.parse(input.value);
      delete (this.errors[this.target]||{})[f.id];
    } catch { (this.errors[this.target]??={})[f.id]='请输入有效的 '+(f.type==='json'?'JSON':'数值')+'。'; }
    this.stage({op:'set',value:v},input.value);
    this.root.querySelector('#config-input-error').textContent=this.errors[this.target]?.[f.id]||'';
  }
  async onClick(event) {
    const b=event.target.closest('[data-config]');if(!b||b.disabled||this.busy)return;
    const action=b.dataset.config,id=b.dataset.id;
    try {
      if(action==='edit'||action==='trace')this.open(id,action==='trace'?'source':'edit');
      if(action==='close')this.close();
      if(action==='filter'){this.filter=b.dataset.filter;this.render();}
      if(action==='tab'){this.tab=b.dataset.tab;this.revealed={};this.render();}
      if(action==='scope')this.selectTarget(this.preferred(b.dataset.scope));
      if(action==='target'){this.tab='edit';this.selectTarget(b.dataset.target);}
      if(action==='clear'||action==='empty'){delete (this.errors[this.target]||{})[this.selected];this.revealed={};this.stage(action==='clear'?{op:'unset'}:{op:'set',value:''});this.render();}
      if(action==='undo'){delete this.changes()[this.selected];delete (this.inputs[this.target]||{})[this.selected];delete (this.errors[this.target]||{})[this.selected];this.revealed={};this.queuePreview();this.render();}
      if(action==='summary'){this.expanded=!this.expanded;b.textContent=this.expanded?'收起':'变更摘要';b.setAttribute('aria-expanded',String(this.expanded));this.paint();}
      if(action==='review'){const [target,changes]=Object.entries(this.drafts).find(([,d])=>Object.keys(d).length);this.open(Object.keys(changes)[0],'edit',target);}
      if(action==='discard'&&confirm('放弃全部尚未保存的草稿？')){this.drafts={};this.inputs={};this.errors={};this.invalidate();this.render();}
      if(action==='reload'){await this.reload();this.toast('已重新载入；草稿保留，请核对新结果。');if(this.opened)this.queuePreview();}
      if(action==='reveal') {
        const key=b.dataset.layer;
        if(this.revealed[key]!==undefined)delete this.revealed[key];
        else if(key===this.target&&this.changes()[this.selected]?.op==='set')this.revealed[key]=this.changes()[this.selected].value;
        else {const selected=this.selected,target=this.target,generation=this.generation;const result=await this.api('/api/reveal',{id:selected,target:key==='effective'?null:key,revision:this.snapshot.revision});if(selected!==this.selected||target!==this.target||generation!==this.generation)return;this.revealed[key]=result.value;}
        this.render();
      }
      if(action==='save') {
        this.busy=true;this.paint();const target=this.target;
        try {
          const result=await this.api('/api/save',{target,revision:this.snapshot.revision,changes:this.changes()});
          delete this.drafts[target];delete this.inputs[target];delete this.errors[target];this.set(result.config);
          this.message='已保存此文件；'+(result.receipt.validation||'适配器校验')+'通过。';
          this.render();
        } finally {this.busy=false;this.paint();}
      }
    } catch(error) {this.error=error.message;this.paint();this.toast(error.message);}
  }
  onKey(event) {
    if(!this.opened)return;
    if(event.key==='Escape'){event.preventDefault();this.close();}
    if(event.key==='Tab'){
      const items=[...this.root.querySelectorAll('.config-drawer button:not(:disabled),.config-drawer input:not(:disabled),.config-drawer select:not(:disabled),.config-drawer textarea:not(:disabled),.config-drawer summary')].filter(el=>el.getClientRects().length);
      const first=items[0],last=items.at(-1);
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
    }
  }
}
