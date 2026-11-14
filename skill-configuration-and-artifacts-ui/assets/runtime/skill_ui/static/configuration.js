const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const scopes = {global:'个人通用', project:'仅本项目'};
const statuses = {effective:'此项来自这里', shadowed:'此项被其他来源覆盖', empty:'空值', missing:'未设置', skipped:'本次未启用', error:'读取错误'};
const show = (f, r) => f.sensitive ? r.mask || '未填写' : r.value == null ? '未设置' : r.value === '' ? '空值' : typeof r.value === 'object' ? JSON.stringify(r.value) : String(r.value);
const hasValue = (f, r) => f.sensitive ? !!r.configured : r.value != null && r.value !== '' && (!Array.isArray(r.value) || r.value.length > 0);

export class Configuration {
  constructor({root, api, render, reload, toast}) {
    Object.assign(this, {root, api, render, reload, toast});
    this.drafts = {}; this.inputs = {}; this.errors = {}; this.revealed = {};
    this.target = ''; this.visible = new Set(); this.details = new Set(); this.scopeTargets = {};
    this.preview = null; this.generation = 0; this.busy = false; this.message = ''; this.error = '';
    root.addEventListener('click', ev => this.onClick(ev));
    root.addEventListener('input', ev => this.onInput(ev));
    root.addEventListener('compositionstart', () => { this.composing=true; clearTimeout(this.timer); });
    root.addEventListener('compositionend', ev => { this.composing=false; this.onInput(ev); });
    root.addEventListener('change', ev => {
      if (this.busy) return;
      if (ev.target.name === 'config-scope') this.selectTarget(this.preferred(ev.target.value));
      if (ev.target.id === 'draft-file') this.selectTarget(ev.target.value);
    });
    root.addEventListener('submit', ev => {
      if (ev.target.id === 'configuration-form') { ev.preventDefault(); this.save(); }
    });
    root.addEventListener('toggle', ev => {
      const id = ev.target.dataset.configDetail;
      if (id && ev.target.isConnected) ev.target.open ? this.details.add(id) : this.details.delete(id);
    }, true);
  }
  fields() { return this.snapshot.fields; }
  field(id) { return this.fields().find(f => f.id === id); }
  layer(id = this.target) { return this.snapshot.layers.find(l => l.id === id); }
  changes() { return this.drafts[this.target] || {}; }
  row(f, target = this.target) { return f.rows.find(r => r.layer === target) || {}; }
  after(f) { return this.preview?.fields.find(n => n.id === f.id) || f; }
  missing() { return (this.preview || this.snapshot).readiness?.missing || []; }
  targets(f) { return this.snapshot.layers.filter(l => !l.readonly && (!f ? this.fields().some(n => !n.readonly && n.targets?.includes(l.id)) : !f.readonly && f.targets?.includes(l.id))); }
  writable(f) { return this.targets(f).some(l => l.id === this.target) && this.after(f).active !== false; }
  source(r) { return r.source === 'default' ? '默认值' : this.layer(r.source)?.name || '尚未设置'; }
  preferred(scope) {
    const choices = this.targets().filter(l => !scope || l.scope === scope);
    const focused = this.focus.map(id => this.field(id)).filter(Boolean);
    const common = choices.filter(l => focused.every(f => f.readonly || f.targets?.includes(l.id)));
    const suitable = common.length ? common : choices;
    return suitable.find(l => l.id === this.scopeTargets[scope])?.id ||
      suitable.find(l => focused.some(f => f.effective.source === l.id))?.id ||
      suitable.find(l => l.id === this.snapshot.default_targets?.[scope])?.id || suitable[0]?.id || '';
  }
  set(snapshot) {
    const first = !this.snapshot;
    this.snapshot = snapshot;
    this.invalidate(); this.revealed = {}; this.error = ''; this.message = ''; this.retrySave=false;
    if (first) {
      const params = new URLSearchParams(location.search), presentation = snapshot.presentation || {};
      const requested = params.has('fields') ? params.get('fields').split(',').map(id => id.trim()) : presentation.focus || [];
      this.focus = requested.filter(id => this.field(id));
      if (!this.focus.length && !this.missing().length && !params.has('fields'))
        this.focus = this.fields().filter(f => !f.advanced && f.active !== false).slice(0, 3).map(f => f.id);
      this.focus.forEach(id => this.visible.add(id));
      const requestedTarget = params.get('target');
      const existing = this.focus.map(id => this.field(id)).map(f => f.effective.source);
      this.target = this.targets().find(l => l.id === requestedTarget)?.id ||
        this.targets().find(l => l.id === presentation.initial_target)?.id ||
        this.targets().find(l => existing.includes(l.id) && this.focus.every(id => this.field(id).readonly || this.field(id).targets?.includes(l.id)))?.id ||
        this.preferred('global') || this.preferred('project') || this.preferred();
    }
    if (!this.targets().some(l => l.id === this.target)) this.target = this.preferred('global') || this.preferred();
    this.rememberMissing();
  }
  rememberMissing() { this.missing().forEach(id => this.visible.add(id)); }
  invalidate() { clearTimeout(this.timer); this.generation++; this.preview = null; }
  count() { return Object.values(this.drafts).reduce((n, d) => n + Object.keys(d).length, 0); }
  detail(id, title, body, className = '') {
    return `<details class="${className}" data-config-detail="${esc(id)}" ${this.details.has(id)?'open':''}><summary>${title}</summary>${body}</details>`;
  }
  html() {
    const presentation = this.snapshot.presentation || {}, layer = this.layer(), availableScopes = [...new Set(this.targets().map(l => l.scope))].sort((a,b) => (a === 'global' ? -1 : b === 'global' ? 1 : 0));
    const eligible = this.fields().filter(f => this.after(f).active !== false || this.changes()[f.id]);
    const primary = eligible.filter(f => this.visible.has(f.id)), other = eligible.filter(f => !this.visible.has(f.id));
    return `<section class="content context-content"><form id="configuration-form" novalidate>
      <div class="heading"><div><div class="eyebrow">CONFIGURATION</div><h1>${esc(presentation.title || '配置设置')}</h1><p>${esc(presentation.description || '先调整这次需要的内容，其他设置可以随时展开。')}</p></div><button type="button" data-config="reload">重新载入</button></div>
      <div class="context-scope"><div class="scope-line"><span>保存到</span>${availableScopes.length > 1 ? `<div class="scope-options" role="group" aria-label="保存范围">${availableScopes.map(scope=>`<label class="scope-choice"><input type="radio" name="config-scope" value="${esc(scope)}" ${scope===layer?.scope?'checked':''} ${this.busy?'disabled':''}><span>${esc(scopes[scope] || scope)}</span></label>`).join('')}</div>` : `<strong>${esc(scopes[layer?.scope] || '只读配置')}</strong>`}</div><p>${availableScopes.includes('global') && availableScopes.includes('project') ? '本项目单独设置的内容优先；没有单独设置的，继续使用个人通用设置。' : esc(layer?.scope_label || '可查看当前配置及其来源。')}</p></div>
      <div id="config-readiness" role="status"></div>
      ${this.snapshot.note?`<p class="config-note">${esc(this.snapshot.note)}</p>`:''}
      <div class="context-fields">${primary.map(f => this.fieldHTML(f)).join('') || '<p class="config-note">当前没有需要补填的配置，可展开其他设置查看或修改。</p>'}</div>
      ${other.length ? this.detail('other',`其他设置 <span class="detail-count">${other.length}</span>`, [...new Set(other.map(f=>f.group || '其他'))].map(group=>`<section class="context-group"><h2>${esc(group)}</h2>${other.filter(f=>(f.group||'其他')===group).map(f=>this.fieldHTML(f)).join('')}</section>`).join(''), 'other-settings') : ''}
      ${this.detail('storage','保存位置与配置来源',this.storageHTML(),'storage-details')}
      <footer class="context-save"><div id="config-drafts"></div><div class="context-save-line"><span id="config-save-count"></span><button type="submit" class="primary" id="config-save">保存修改</button></div><div id="config-feedback" role="status" aria-live="polite"></div></footer>
    </form></section>`;
  }
  fieldHTML(f) {
    const edit = this.changes()[f.id], writable = this.writable(f), missing = this.missing().includes(f.id), key = `${f.id}:effective`;
    return `<section class="context-field" data-field="${esc(f.id)}"><div class="context-field-label"><label for="field-${esc(f.id)}">${esc(f.label)}</label>${missing?'<span class="field-tag missing">需要补填</span>':f.required?'<span class="field-tag">必要配置</span>':''}</div>
      ${writable ? this.controlHTML(f) : `<div class="readonly-value">${esc(show(f, this.after(f).effective))}</div><p class="config-note">${this.after(f).active === false ? '当前不适用，请撤销此项修改或核对关联设置。' : '此项不能写入当前保存位置。'} ${this.targets(f).map(l=>`<button type="button" class="text-button" data-config="target" data-target="${esc(l.id)}">改存${esc(scopes[l.scope] || l.name)} · ${esc(l.name)}</button>`).join('')}</p>`}
      ${f.help?`<p class="field-help">${esc(f.help)}</p>`:''}
      <p class="field-error error" id="error-${esc(f.id)}">${esc(this.errors[this.target]?.[f.id])}</p>
      <div class="field-result" id="result-${esc(f.id)}"></div>
      ${this.detail(`field:${f.id}`, '查看来源与调整', `<p class="config-note">${esc(f.config_key || f.id)}${f.effect?` · ${esc(f.effect)}`:''}</p><p class="config-note">当前使用：${esc(show(f,f.effective))} · ${esc(this.source(f.effective))}</p>${f.sensitive && hasValue(f,f.effective)?`<button type="button" data-config="reveal" data-id="${esc(f.id)}" data-layer="effective">${this.revealed[key]!==undefined?'隐藏':'查看'}已保存的值</button>${this.revealed[key]!==undefined?`<code class="revealed-value">${esc(this.revealed[key])}</code>`:''}`:''}<div class="field-actions">${writable?`<button type="button" data-config="clear" data-id="${esc(f.id)}">移除此文件的设置</button><button type="button" data-config="empty" data-id="${esc(f.id)}">设为空值</button>`:''}</div><p class="config-note">移除后是否使用其他配置，以保存前的计算结果为准。</p>${this.traceHTML(f)}`, 'field-details')}
      <button type="button" class="text-button undo" data-config="undo" data-id="${esc(f.id)}" ${!edit?'hidden':''}>撤销本项修改</button>
    </section>`;
  }
  controlHTML(f) {
    const edit=this.changes()[f.id], row=this.row(f), raw=this.inputs[this.target]?.[f.id];
    const saved=edit?.op==='unset'?null:edit?.op==='set'?edit.value:row.present?row.value:f.effective.value;
    const base=`id="field-${esc(f.id)}" data-config-field="${esc(f.id)}" aria-describedby="error-${esc(f.id)}" ${this.busy?'disabled':''}`;
    if(f.sensitive) {
      const key=`${f.id}:${this.target}`, revealed=this.revealed[key];
      return `<div class="context-secret"><input ${base} type="${revealed!==undefined?'text':'password'}" value="${esc(edit?.op==='set'?edit.value:revealed??'')}" placeholder="${esc(edit?.op==='unset'?'将移除此文件的值':row.configured?row.mask:f.effective.configured?'已配置，输入新值可替换':'填写新值')}" autocomplete="new-password"><button type="button" data-config="reveal" data-id="${esc(f.id)}" data-layer="${esc(this.target)}" ${!edit && !row.configured?'disabled':''}>${revealed!==undefined?'隐藏':'显示'}</button></div>`;
    }
    if(f.type==='boolean')return `<label class="context-boolean"><input ${base} type="checkbox" ${saved?'checked':''}>启用</label>`;
    if(f.type==='select')return `<select ${base}><option value="">未设置</option>${(f.choices||[]).map(c=>`<option value="${esc(c)}" ${c===saved?'selected':''}>${esc(c)}</option>`).join('')}</select>`;
    if(['list','json'].includes(f.type))return `<textarea ${base}>${esc(raw??(f.type==='list'?(saved||[]).join('\n'):JSON.stringify(saved??null,null,2)))}</textarea>${f.type==='list'?'<small>每行一项，按填写顺序使用。</small>':''}`;
    return `<input ${base} type="${f.type==='number'?'number':'text'}" ${f.type==='number'?'step="any"':''} value="${esc(raw??saved??'')}" placeholder="${esc(edit?.op==='unset'?'将移除此文件的值':show(f,f.effective))}">`;
  }
  fileHTML(l) {
    const hasValues=this.fields().some(f=>hasValue(f,this.row(f,l.id))), writable=this.targets().some(n=>n.id===l.id);
    return `<div class="context-file ${hasValues?'has-values':''} ${l.id===this.target?'selected':''}"><div><strong>${esc(l.name)}</strong><span>${l.id===this.target?'本次保存位置':hasValues?'有已填写的配置':l.readonly?'只读':'尚未填写'}</span></div><code>${esc(l.path)}</code><small>${esc(l.scope_label)}${l.enabled===false?' · 本次未启用':''}${l.message?' · '+esc(l.message):''}</small>${writable && l.id!==this.target?`<button type="button" data-config="target" data-target="${esc(l.id)}">保存到这里</button>`:''}</div>`;
  }
  storageHTML() {
    const scope=this.layer()?.scope;
    const relevant=this.snapshot.layers.filter(l=>l.scope===scope && (l.id===this.target || l.id===this.snapshot.default_targets?.[scope] || this.fields().some(f=>hasValue(f,this.row(f,l.id))) || l.message));
    return `<p class="config-note">这里选择本次修改写入的位置。每项配置分别读取，保存位置不一定是它当前的来源。</p>${relevant.map(l=>this.fileHTML(l)).join('')}${this.detail('rules','全部文件规则',`<p class="config-note">以下按读取优先级从高到低排列；缺失项和空值的处理遵循此 Skill 的规则。默认保存位置由开发者指定。</p>${this.snapshot.layers.map(l=>this.fileHTML(l)).join('')}`)}`;
  }
  traceHTML(f) {
    return this.detail(`trace:${f.id}`, '各文件中的此项配置', `<ol class="context-trace">${this.snapshot.layers.map(l=>{const row=this.row(f,l.id), key=`${f.id}:${l.id}`;return `<li><strong>${esc(l.name)}</strong> · ${esc(statuses[row.status]||row.status)}<code>${esc(l.path)}</code><p>${esc(this.revealed[key]??show(f,row))}</p>${row.message?`<p class="error">${esc(row.message)}</p>`:''}${f.sensitive&&row.configured?`<button type="button" data-config="reveal" data-id="${esc(f.id)}" data-layer="${esc(l.id)}">${this.revealed[key]!==undefined?'隐藏':'查看'}原值</button>`:''}</li>`;}).join('')}</ol>`);
  }
  afterRender() { this.paint(); }
  rerender() {
    const active=document.activeElement, id=active?.id, start=active?.selectionStart, end=active?.selectionEnd;
    const scopeFocus=active?.name==='config-scope';
    this.render();
    const replacement=id && document.getElementById(id);
    if(replacement) { replacement.focus({preventScroll:true}); if(start!=null && replacement.setSelectionRange)replacement.setSelectionRange(start,end); }
    else if(scopeFocus)this.root.querySelector('[name=config-scope]:checked')?.focus({preventScroll:true});
  }
  canSave() { return !this.busy && !!Object.keys(this.changes()).length && !!this.preview && (!this.error || this.retrySave) && !Object.keys(this.errors[this.target]||{}).length && !this.preview.validation_errors?.length; }
  paint() {
    const feedback=this.root.querySelector('#config-feedback'); if(!feedback)return;
    const changes=this.changes(), count=Object.keys(changes).length, missing=this.missing();
    this.root.querySelector('#config-readiness').innerHTML=missing.length?`<p class="readiness-warning">还需填写：${missing.map(id=>esc(this.field(id)?.label||id)).join('、')}。可以先保存其他有效修改，补齐后才能继续业务任务。</p>`:((this.preview||this.snapshot).readiness?.ready===false?'<p class="readiness-warning">配置还需检查，请核对下方提示后再继续业务任务。</p>':'');
    feedback.innerHTML=(this.error?`<p class="error">${esc(this.error)}</p>`:'')+(count&&!this.preview&&!this.error?'<p class="config-note">正在检查本次修改…</p>':'')+(this.preview?.validation_errors||[]).map(s=>`<p class="error">${esc(s)}</p>`).join('')+(this.message?`<div class="save-success"><strong>${esc(this.message)}</strong><p>请回到 Agent 对话窗口，发送：</p><div class="continue-message"><span>配置已保存，请继续。</span><button type="button" data-config="copy">复制消息</button></div><small>${esc(this.snapshot.validation || '保存校验通过')}${missing.length?'；必要配置尚未补齐。':''}</small></div>`:'');
    this.root.querySelector('#config-save-count').textContent=count?`本次保存 ${count} 项修改`:'尚未修改';
    const button=this.root.querySelector('#config-save'); button.disabled=!this.canSave(); button.textContent=this.busy?'正在保存…':'保存修改';
    const others=Object.keys(this.drafts).filter(id=>id!==this.target && Object.keys(this.drafts[id]).length);
    this.root.querySelector('#config-drafts').innerHTML=others.length?`<label>其他位置还有未保存的修改<select id="draft-file"><option value="${esc(this.target)}">继续编辑当前文件</option>${others.map(id=>`<option value="${esc(id)}">${esc(scopes[this.layer(id)?.scope]||'')} · ${esc(this.layer(id)?.name)}</option>`).join('')}</select></label>`:'';
    for(const f of this.fields()) {
      const node=document.getElementById(`result-${f.id}`); if(!node)continue;
      const next=this.after(f), edit=changes[f.id];
      const undo=node.closest('[data-field]').querySelector('[data-config=undo]'); if(undo)undo.hidden=!edit;
      const error=document.getElementById(`error-${f.id}`);error.textContent=this.errors[this.target]?.[f.id]||'';
      document.getElementById(`field-${f.id}`)?.setAttribute('aria-invalid',String(!!error.textContent));
      node.innerHTML=edit && this.preview?`<p class="config-note">保存后使用：${esc(show(next,next.effective))} · ${esc(this.source(next.effective))}</p>${next.effective.source!==this.target?`<p class="field-warning">${edit.op==='unset'?'移除后按上面的来源读取。':'这次修改会保存；当前使用的值仍由上面的来源提供。'}</p>`:''}`:!edit && f.effective.source && f.effective.source!==this.target?`<p class="config-note">当前值来自${esc(this.source(f.effective))}${this.writable(f)?`，在这里修改后将保存到${esc(scopes[this.layer()?.scope] || '所选位置')}`:''}。</p>`:'';
      if(next.effective.message)node.innerHTML+=`<p class="error">${esc(next.effective.message)}</p>`;
    }
  }
  queuePreview() {
    this.invalidate(); this.error=''; this.message=''; this.retrySave=false; this.paint();
    if(!this.target||!Object.keys(this.changes()).length)return;
    if(Object.keys(this.errors[this.target]||{}).length){this.error='请先修正此文件草稿中的格式错误。';this.paint();return;}
    if(this.composing)return;
    const generation=this.generation, target=this.target, revision=this.snapshot.revision, changes=structuredClone(this.changes());
    this.timer=setTimeout(async()=>{try{const result=await this.api('/api/preview',{target,revision,changes});if(generation!==this.generation)return;this.preview=result.config;this.rememberMissing();this.rerender();}catch(error){if(generation!==this.generation)return;this.error=error.message;this.paint();}},160);
  }
  selectTarget(target) {
    if(this.busy || !this.targets().some(l=>l.id===target))return;
    const scopeFocus=document.activeElement?.name==='config-scope';
    if(this.layer())this.scopeTargets[this.layer().scope]=this.target;
    this.target=target;this.scopeTargets[this.layer().scope]=target;this.revealed={};this.invalidate();this.render();
    if(scopeFocus)this.root.querySelector('[name=config-scope]:checked')?.focus({preventScroll:true});
    this.queuePreview();
  }
  stage(id, change, raw) {
    const f=this.field(id), row=this.row(f);
    const unchanged=change.op==='unset' ? !row.present : !f.sensitive && row.present && JSON.stringify(change.value)===JSON.stringify(row.value);
    if(unchanged)delete (this.drafts[this.target]||{})[id];
    else (this.drafts[this.target]??={})[id]=change;
    if(raw!==undefined)(this.inputs[this.target]??={})[id]=raw;else delete (this.inputs[this.target]||{})[id];
    this.queuePreview();
  }
  onInput(event) {
    const input=event.target;if(!input.dataset.configField || !this.snapshot)return;
    const f=this.field(input.dataset.configField);if(!f||this.busy||!this.writable(f))return;
    let v=input.value;
    try {
      if(f.type==='number'){if(v===''||!Number.isFinite(Number(v)))throw Error();v=Number(v);}
      if(f.type==='boolean')v=input.checked;
      if(f.type==='list')v=input.value.split('\n').filter(line=>line!=='');
      if(f.type==='json')v=JSON.parse(input.value);
      delete (this.errors[this.target]||{})[f.id];
    } catch { (this.errors[this.target]??={})[f.id]='请输入有效的 '+(f.type==='json'?'JSON':'数值')+'。'; }
    this.stage(f.id,{op:'set',value:v},input.value);
  }
  async save() {
    if(!this.canSave())return;
    this.busy=true;this.error='';this.retrySave=false;this.render();const target=this.target;
    try {
      const result=await this.api('/api/save',{target,revision:this.snapshot.revision,changes:structuredClone(this.changes())});
      delete this.drafts[target];delete this.inputs[target];delete this.errors[target];this.set(result.config);
      this.message='修改已保存。';
    } catch(error) {this.error=error.message;this.retrySave=![401,403,409,410,422].includes(error.status);this.toast(error.message);}
    finally {this.busy=false;this.render();}
  }
  async onClick(event) {
    const b=event.target.closest('[data-config]');if(!b||b.disabled||this.busy)return;
    const action=b.dataset.config,id=b.dataset.id,f=this.field(id);
    try {
      if(action==='target')this.selectTarget(b.dataset.target);
      if((action==='clear'||action==='empty') && f && this.writable(f)) {
        delete (this.errors[this.target]||{})[id];this.revealed={};
        this.stage(id,action==='clear'?{op:'unset'}:{op:'set',value:''});this.rerender();
      }
      if(action==='undo') {delete this.changes()[id];delete (this.inputs[this.target]||{})[id];delete (this.errors[this.target]||{})[id];this.revealed={};this.queuePreview();this.rerender();}
      if(action==='reload'){await this.reload();this.toast('已重新载入；草稿保留，请核对新结果。');this.queuePreview();}
      if(action==='copy'){await navigator.clipboard.writeText('配置已保存，请继续。');this.toast('已复制，请回到 Agent 对话窗口发送。');}
      if(action==='reveal' && f?.sensitive) {
        const layer=b.dataset.layer, key=`${id}:${layer}`, generation=this.generation;
        if(this.revealed[key]!==undefined)delete this.revealed[key];
        else if(layer===this.target && this.changes()[id]?.op==='set')this.revealed[key]=this.changes()[id].value;
        else {const result=await this.api('/api/reveal',{id,target:layer==='effective'?null:layer,revision:this.snapshot.revision});if(generation!==this.generation)return;this.revealed[key]=result.value;}
        this.rerender();
      }
    } catch(error) {this.error=error.message;this.paint();this.toast(error.message);}
  }
}
