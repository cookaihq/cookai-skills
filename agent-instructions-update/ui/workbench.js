'use strict';
const root=document.querySelector('#app'),dialog=document.querySelector('#dialog');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const symbols={detect:'⌕',sync:'⇄',scenarios:'▤',layout:'⑂',review:'▧',history:'◷',settings:'⚙'};
const pages=[['detect','Agent 检测'],['sync','主规则同步'],['scenarios','场景规则'],['layout','存储与入口'],['review','审阅建议'],['history','任务历史']];
const query=new URLSearchParams(location.search);
const state={page:pages.some(x=>x[0]===query.get('view'))||query.get('view')==='settings'?query.get('view'):'sync',
  scope:query.get('scope')==='project'?'project':'global',project:query.get('project')||'',buckets:new Map(),
  workspace:null,discovery:null,csrf:'',lifetime:null,busy:false,stopped:false,personal:'',message:''};
const statusNames={waiting_agent:'等待 Agent',candidate_review:'审阅结果待选择',pending_confirmation:'计划待确认',confirmed:'已确认，待执行',
  completed:'已完成',stale:'输入已变化',applying:'执行中',rolled_back:'执行失败，已回滚',recovery_required:'待恢复'};
let toastTimer,activityAt=0,titleTimer;
function bucket(){
  const key=state.scope==='global'?'global':state.project;
  if(!state.buckets.has(key))state.buckets.set(key,{selected:'',drafts:{},titles:{},collapsed:new Set(),tasks:{},authority:'',targets:{},
    config:null,settingsDraft:null,saveScope:'personal',reviewPaths:new Set(),models:null,treeScroll:0,history:[],candidateDrafts:{}});
  return state.buckets.get(key);
}
function scopeValue(){return state.scope==='global'?'global':state.project;}
function button(text,action,cls='',disabled=false){return '<button type="button" data-action="'+escape(action)+'" class="'+cls+'" '+(disabled?'disabled':'')+'>'+escape(text)+'</button>';}
function badge(text,tone=''){return '<span class="badge '+tone+'">'+escape(text)+'</span>';}
function field(label,id,value='',hint='',type='text'){return '<div class="field"><label class="form-label" for="'+id+'">'+escape(label)+'</label><input id="'+id+'" type="'+type+'" value="'+escape(value)+'">'+(hint?'<p class="small muted">'+escape(hint)+'</p>':'')+'</div>';}
function area(label,id,value='',hint=''){return '<div class="field"><label class="form-label" for="'+id+'">'+escape(label)+'</label><textarea id="'+id+'">'+escape(value)+'</textarea>'+(hint?'<p class="small muted">'+escape(hint)+'</p>':'')+'</div>';}
function select(label,id,options,value){return '<div class="field"><label class="form-label" for="'+id+'">'+label+'</label><select id="'+id+'">'+options.map(([key,text])=>'<option value="'+escape(key)+'" '+(key===value?'selected':'')+'>'+escape(text)+'</option>').join('')+'</select></div>';}
function heading(name,description,actions=''){return '<div class="page-heading"><div><h1>'+name+'</h1><p>'+description+'</p></div><div class="actions">'+actions+'</div></div>';}
function notice(text){return '<div class="inline-note">'+escape(text)+'</div>';}
function toast(text){const node=document.querySelector('#toast');node.textContent=text;node.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('visible'),6500);}
function openDialog(title,body,actions=button('关闭','close')){dialog.innerHTML='<div class="dialog-head"><h2 id="dialog-title">'+escape(title)+'</h2>'+button('关闭','close','text')+'</div><div class="dialog-body">'+body+'</div><div class="dialog-foot">'+actions+'</div>';if(!dialog.open)dialog.showModal();}
async function api(path,data){
  const response=await fetch(path,{method:data===undefined?'GET':'POST',headers:data===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':state.csrf},
    body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){const error=new Error(result.message||'请求未完成');error.code=result.error||result.code;throw error;}
  return result;
}
async function call(action,data={},project=state.project){return (await api('/api/rules',{action,data,project})).result;}
function lines(value){return value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);}
function pathJoin(base,relative){return base.replace(/[\\/]+$/,'')+'/'+relative.replaceAll('\\','/');}
function currentScenario(){return state.workspace.scenarios.find(x=>x.path===bucket().selected);}
function scenarioName(item){return bucket().titles[item.path]??item.title??'未命名场景';}
function currentTask(){return bucket().tasks[state.page];}
function remember(task,page=state.page){bucket().tasks[page]=task;updateURL();}
function updateURL(){
  const params=new URLSearchParams({view:state.page,scope:state.scope,project:state.project});
  if(currentTask())params.set('task',currentTask().id);
  history.replaceState(null,'',location.pathname+'?'+params);
}
async function loadWorkspace(){
  const view=await call('workspace.read',{scope:state.scope});
  state.project=view.project.path;state.workspace=view;
  if(state.scope==='project'&&view.project.availability!=='ready'&&!currentTask()){state.scope='global';return loadWorkspace();}
  const b=bucket();
  if(!view.scenarios.some(x=>x.path===b.selected))b.selected=view.scenarios[0]?.path||'';
  const config=await call('config.read',{scope:scopeValue()});
  if(!b.config||!b.settingsDraft||!settingsChanges().length){b.config=config;b.settingsDraft=structuredClone(config.values);}
  if(!b.authority)b.authority=config.values.binding?.authority||view.main_files.find(x=>x.status==='found'&&x.content)?.path||'';
}
async function refreshHistory(){bucket().history=(await call('history.list',{scope:scopeValue()})).tasks;}
function lifecycle(){return state.stopped?'页面已结束':'空闲 '+Math.ceil((state.lifetime?.remaining??3600)/60)+' 分钟后到期';}
function render(){
  if(!state.workspace)return;
  const context=state.workspace.project,global=state.scope==='global';
  const available=context.availability==='ready',reason=context.availability==='error'?'检测未完成':context.availability==='none'?'未关联项目':'尚未建立项目规则';
  const navigation=pages.map(([key,name])=>button(symbols[key]+'　'+name,'page:'+key,state.page===key?'active':'')).join('');
  root.innerHTML='<div class="shell scope-'+(global?'personal':'project')+'"><aside class="rail"><div class="brand"><span class="brand-mark">▤</span>Agent 规则维护</div><div class="rail-note">WORKSPACE</div><nav class="nav" aria-label="规则维护操作">'+navigation+'</nav><div class="rail-bottom">'+button('⚙　设置','page:settings','rail-settings '+(state.page==='settings'?'active':''))+'<div class="rail-footer">agent-instructions-update<br>0.1.1 · 规则维护工作台</div></div></aside><div class="workspace"><header class="topbar"><div class="scope-header"><span class="scope-caption">维护范围</span><div class="scope-switch" role="group" aria-label="选择规则维护范围"><button data-action="scope:global" class="scope-option '+(global?'selected':'')+'" aria-pressed="'+global+'"><span><strong>全局规则</strong><small>用户级 · 跨项目复用</small></span></button><button data-action="scope:project" class="scope-option '+(!global?'selected':'')+'" aria-pressed="'+!global+'" '+(!available&&global?'disabled':'')+'><span><strong>项目规则</strong><small>'+escape(available?context.name:reason)+'</small></span></button></div><div class="project-availability">项目：<code>'+escape(context.path)+'</code> '+button('选择项目','project-picker','text')+(!available?button(context.availability==='error'?'重新检测':'建立项目规则',context.availability==='error'?'reload':'project-create','text'):'')+'</div></div><div class="service"><span id="lifetime">'+lifecycle()+'</span> '+button('结束页面','stop','text')+'</div></header><main class="content"><section class="scope-context" aria-label="当前维护范围"><div class="scope-context-title"><div><strong>当前维护：'+(global?'全局规则':'项目规则 · '+escape(context.name))+'</strong><span>'+(global?'用户级主规则与场景；供多个项目复用。':'仅维护此项目 · '+escape(context.path))+'</span></div></div>'+(state.page==='settings'?'<span class="scope-subnote">设置保存位置在本页单独选择。</span>':'')+'</section>'+(state.message?notice(state.message):'')+(state.workspace.errors.length?notice('部分位置读取失败：'+state.workspace.errors.join('；')):'')+bindingControls()+view()+taskView()+'</main></div></div>';
  const tree=root.querySelector('.scenario-tree-scroll');if(tree)tree.scrollTop=bucket().treeScroll;
  updateURL();
}
function view(){return ({detect:detectView,sync:syncView,scenarios:scenarioView,layout:layoutView,review:reviewView,history:historyView,settings:settingsView}[state.page])();}
function detectView(){
  const data=bucket().discovery;
  return heading('Agent 检测','安装、规则入口与实际加载证据分别核对。',button('刷新检测','detect'))+(!data?notice('点击“刷新检测”，检查当前原生系统中的已知安装位置。'):
    '<p class="small muted">'+escape(data.environment.platform+' · '+data.environment.kind+' · '+new Date(data.environment.detected_at*1000).toLocaleString())+'</p>'+data.products.map(p=>'<section class="card below"><div class="card-head"><h2>'+escape(p.name)+'</h2>'+badge(p.supported?({found:'发现安装',not_found:'未发现安装',unreadable:'安装不可读'}[p.installation_status]||p.installation_status):'尚未支持',p.supported?'blue':'gray')+'</div>'+(p.supported?'<div class="card-body">'+p.installations.map(i=>'<div class="endpoint"><strong>'+escape(i.version||'版本未核实')+'</strong><span class="file-path mono">'+escape(i.path||i.source)+'</span><span class="small muted">'+escape(i.instance_evidence||i.source)+'</span></div>').join('')+'<div class="table-wrap"><table class="table"><thead><tr><th>Profile / 文件职责</th><th>规则入口</th><th>读取状态</th></tr></thead><tbody>'+p.rules.map(r=>'<tr><td>'+escape((r.profile||'')+' · '+r.role)+'</td><td><code>'+escape(r.path)+'</code></td><td>'+escape(r.status)+'</td></tr>').join('')+'</tbody></table></div><p class="small muted">'+escape(p.mechanism.summary)+'</p><p class="small">实际加载：未验证 · 行为结果：未验证</p><details><summary>检测依据与模型信息</summary><pre>'+escape(JSON.stringify({mechanism:p.mechanism,models:p.models},null,2))+'</pre></details></div>':'')+'</section>').join('')+(data.errors.length?notice(data.errors.join('；')):''))+ '<p class="section-note">自定义安装及规则路径可在检测时补充；长期管理绑定保存在“设置”。</p>'+button('补充自定义位置','detect-custom');
}
function emptyBatch(){
  const b=bucket();
  return Object.keys(b.targets).length>0&&Object.entries(b.targets).every(([path,strategy])=>strategy==='empty'&&!state.workspace.main_files.find(f=>f.path===path)?.content);
}
function syncView(){
  const b=bucket(),files=state.workspace.main_files,authority=files.find(f=>f.path===b.authority);
  const rows=files.filter(f=>f.path!==b.authority).map(f=>{
    const shared=f.exists&&authority?.exists&&JSON.stringify(f.identity)===JSON.stringify(authority.identity);
    const same=f.content===authority?.content;
    const label=f.status==='unreadable'?'不可读':f.status==='missing'?'不存在':shared?'共享实体':!f.content?'空文件':same?'正文相同':'正文不同';
    const options=[['merge','合并'],['overwrite','覆盖'],...(!f.content?[['empty','直接同步']]:[])];
    const compare=f.content&&!same?'<details class="below"><summary>查看正文差异</summary><div class="comparison"><section><h3>权威正文</h3><pre>'+escape(authority?.content||'')+'</pre></section><section><h3>此文件正文</h3><pre>'+escape(f.content)+'</pre></section></div></details>':'';
    return '<div class="sync-target"><div class="file-row"><input type="checkbox" data-sync-path="'+escape(f.path)+'" aria-label="同步到 '+escape(f.path)+'" '+(b.targets[f.path]?'checked':'')+' '+(f.status==='unreadable'?'disabled':'')+'><div class="grow"><strong>'+escape(f.product_name||'规则文件')+'</strong> '+badge(label,shared||same?'blue':'amber')+'<span class="file-path mono">'+escape(f.path)+'</span></div><select data-strategy-path="'+escape(f.path)+'" aria-label="'+escape(f.path)+'处理方式" '+(!b.targets[f.path]?'disabled':'')+'>'+options.map(([key,text])=>'<option value="'+key+'" '+(b.targets[f.path]===key?'selected':'')+'>'+text+'</option>').join('')+'</select></div>'+compare+'</div>';
  }).join('');
  const authorityView=authority?'<details class="below"><summary>查看权威正文</summary><pre>'+escape(authority.content)+'</pre>'+button('编辑权威正文','authority-edit')+'</details>':'';
  return heading('主规则同步','选择权威正文与参与同步的目标。',button('补充规则文件','sync-add'))+
    '<section class="card"><div class="card-head"><h2>规则文件</h2>'+button('刷新文件','reload','text')+'</div><div class="card-body">'+
    select('权威文件','sync-authority',files.filter(f=>f.status==='found'&&f.content).map(f=>[f.path,(f.product_name?f.product_name+' · ':'')+f.path]),b.authority)+
    (files.length?'':'<p>尚未发现主规则。可补充明确路径，或先建立规则。</p>')+authorityView+rows+
    '</div><div class="card-foot">'+button(emptyBatch()?'直接同步所选空目标':'生成具体同步计划','sync-create','primary',!b.authority||!Object.keys(b.targets).length)+
    '<p class="small muted">只有权威与选择“合并”的正文参与合并。共享实体的其他入口会在计划中列明。</p></div></section>';
}
function treeView(){
  const b=bucket(),tree={folders:new Map(),files:[]};
  state.workspace.scenarios.forEach(item=>{const parts=item.relative_path.replaceAll('\\','/').split('/');let node=tree,prefix='';for(const part of parts.slice(0,-1)){prefix=prefix?prefix+'/'+part:part;if(!node.folders.has(part))node.folders.set(part,{path:prefix,folders:new Map(),files:[]});node=node.folders.get(part);}node.files.push(item);});
  function branch(node){
    return [...node.folders].sort(([a],[c])=>a.localeCompare(c)).map(([name,child])=>'<li><button class="folder-row" data-action="folder:'+encodeURIComponent(child.path)+'" aria-expanded="'+!b.collapsed.has(child.path)+'"><span class="folder-chevron">'+(b.collapsed.has(child.path)?'▸':'▾')+'</span>▱ <span class="tree-name">'+escape(name)+'</span></button><ul class="tree-children" '+(b.collapsed.has(child.path)?'hidden':'')+'>'+branch(child)+'</ul></li>').join('')+node.files.sort((a,c)=>a.path.localeCompare(c.path)).map(item=>'<li><button class="tree-file '+(b.selected===item.path?'active':'')+'" data-action="scenario:'+encodeURIComponent(item.path)+'" title="'+escape(scenarioName(item)+' · '+item.path)+'"><span class="file-indent"></span><span class="tree-label"><strong>'+escape(scenarioName(item))+'</strong><small>'+escape(item.relative_path.split('/').at(-1))+'</small></span><span class="draft-dot" '+(b.drafts[item.path]===undefined?'hidden':'')+' aria-label="有未保存草稿"></span></button></li>').join('');
  }
  return '<aside class="card scenario-tree-panel" aria-label="场景文件夹导航"><div class="tree-header"><div><strong>场景规则</strong><span>'+state.workspace.scenarios.length+' 份规则</span></div><code title="'+escape(state.workspace.scenario_root)+'">'+escape(state.workspace.scenario_root)+'</code></div><div class="tree-toolbar">'+button('全部展开','folders-expand','text')+button('全部折叠','folders-collapse','text')+'</div><nav class="scenario-tree-scroll" aria-label="场景规则目录"><ul class="scenario-tree">'+branch(tree)+'</ul></nav><div class="tree-footer">按实际文件目录排列</div></aside>';
}
function readers(item){const seen=new Set();return state.workspace.files.filter(f=>{const key=JSON.stringify(f.identity||f.entity||f.path);if(seen.has(key)||f.path===item.path)return false;const match=(f.references||[]).some(r=>r.target===item.entity);if(match)seen.add(key);return match;});}
function scenarioView(){
  const b=bucket(),item=currentScenario();
  return heading('场景规则','按文件夹浏览；名称取自正文首个一级标题。',button('新增场景','scenario-new'))+'<div class="scenario-grid">'+treeView()+'<section class="card">'+(!item?'<div class="card-body">尚无场景规则，可从“新增场景”开始。</div>':'<div class="card-head"><div><h2 id="scenario-name">'+escape(scenarioName(item))+'</h2><span class="file-path mono">'+escape(item.path)+'</span><span id="scenario-draft-state" class="badge amber scenario-draft-state" '+(b.drafts[item.path]===undefined?'hidden':'')+'>未保存草稿</span></div>'+badge(readers(item).length+' 份引用文件','blue')+'</div><div class="card-body">'+(item.status==='unreadable'?notice('此文件不可读，修复后重新读取。'):area('场景正文','scenario-text',b.drafts[item.path]??item.content??'','名称随标题草稿更新；没有有效一级标题时显示“未命名场景”。'))+'<div class="actions below">'+button('预览修改','scenario-edit','primary',item.status==='unreadable')+button('重命名文件','scenario-rename')+button('删除','scenario-delete','text danger')+'</div><div class="below"><h3>已有读取关系</h3>'+readers(item).map(r=>'<p class="file-path">'+escape(r.path)+' → '+escape(item.relative_path)+'</p>').join('')+'<p class="small muted">文件改名及删除与引用调整进入同一计划。</p></div></div>')+'</section></div>';
}
function layoutView(){
  return heading('存储与入口','正文实体、读取入口和作用范围分别展示。',button('预览布局调整','layout-new'))+'<section class="card"><div class="card-head"><h2>当前规则布局</h2></div><div class="card-body"><p>场景根目录：<code>'+escape(state.workspace.scenario_root)+'</code></p>'+state.workspace.files.map(f=>'<div class="endpoint"><strong>'+escape(f.title||f.role||'规则入口')+'</strong><span class="file-path mono">'+escape(f.path)+'</span><p class="small muted">实体：'+escape(f.entity||'未确定')+' · '+escape(f.kind||f.status)+'</p>'+((f.role==='identity'||f.role==='memory')?button('单独查看和编辑','identity:'+encodeURIComponent(f.path),'text'):'')+'</div>').join('')+'</div><div class="card-foot small muted">文件存在和入口解析不代表宿主实际加载或行为已验证。</div></section>';
}
function reviewView(){
  const b=bucket(),files=state.workspace.files.filter(f=>f.status==='found'&&!f.directory);
  return heading('审阅建议','交给当前 Agent 分析，再回到同一任务查看结果。')+'<section class="card"><div class="card-body">'+area('本次审阅目标模型','review-models',(b.models??b.config.values.review_models).join('\n'),'每行一个模型 ID；本次选择不改动宿主配置。')+'<p class="small muted">配置默认模型与来源见“Agent 检测”；当前会话模型在没有证据时保持未知。</p><h3>选择规则</h3>'+files.map(f=>'<label class="check-line"><input type="checkbox" data-review-path="'+escape(f.path)+'" '+(b.reviewPaths.has(f.path)?'checked':'')+'> <span>'+escape(f.title||f.path)+'<small class="file-path">'+escape(f.path)+'</small></span></label>').join('')+'</div><div class="card-foot">'+button('建立审阅任务','review-create','primary',!b.reviewPaths.size)+'</div></section>';
}
function historyView(){return heading('任务历史','仅包含此范围实际产生的维护记录。',button('刷新历史','history-refresh')+button('清理已完成历史','history-prune','text'))+'<section class="card"><div class="table-wrap"><table class="table"><thead><tr><th>维护任务</th><th>状态</th><th>更新时间</th><th></th></tr></thead><tbody>'+bucket().history.map(t=>'<tr><td>'+escape(t.kind)+'<small class="file-path">'+escape(t.id)+'</small></td><td>'+escape(statusNames[t.status]||t.status)+'</td><td>'+new Date(t.updated_at*1000).toLocaleString()+'</td><td>'+button('查看 / 继续','task-open:'+t.id,'text')+button('预览恢复','restore:'+t.id,'text')+'</td></tr>').join('')+'</tbody></table></div></section>';}
function settingsChanges(){const b=bucket();return Object.keys(b.settingsDraft).filter(key=>JSON.stringify(b.settingsDraft[key])!==JSON.stringify(b.config.values[key]));}
function settingsView(){
  const b=bucket(),d=b.settingsDraft,c=b.config;
  const binding=d.binding||{scope:state.scope,project:state.scope==='project'?state.project:null,product:null,installation:null,profile:null,paths:[],authority:null,layout:null};
  const source=key=>'<span class="file-path small">当前来源：'+escape(c.sources[key])+'</span>';
  return heading('设置','Skill 持久偏好集中在此页；保存设置不会执行规则变更。',button('重读来源并保留编辑','settings-reload'))+'<div class="settings-wrap"><section class="card"><div class="card-head"><h2>保存位置</h2>'+badge(b.saveScope==='personal'?'个人设置':'项目覆盖','blue')+'</div><div class="card-body">'+select('本次保存范围','save-scope',[['personal','个人 · 跨项目复用'],['project','仅当前项目覆盖']],b.saveScope)+'<code>'+escape(c.save_locations[b.saveScope])+'</code></div><div class="settings-section"><div><h2>管理范围绑定</h2>'+source('binding')+'</div><div><label><input id="binding-enabled" type="checkbox" '+(d.binding?'checked':'')+'> 保存完整管理绑定</label>'+select('Agent 产品','binding-product',[['','未指定'],['codex','Codex'],['claude-code','Claude Code'],['workbuddy','WorkBuddy'],['hermes','Hermes'],['opencode','OpenCode']],binding.product||'')+field('安装实例','binding-installation',binding.installation||'')+field('Profile','binding-profile',binding.profile||'')+area('规则路径','binding-paths',binding.paths.join('\n'),'每行一个路径；相对路径按当前管理范围解析。')+field('权威文件','binding-authority',binding.authority||'')+select('布局偏好','binding-layout',[['','未指定'],['existing','保留现有布局'],['custom-root','自选物理根目录'],['personal-agents','个人 .agents 目录']],binding.layout||'')+'</div></div><div class="settings-section"><div><h2>常用审阅模型</h2>'+source('review_models')+'</div>'+area('模型 ID','setting-models',d.review_models.join('\n'))+'</div><div class="settings-section"><div><h2>历史保留</h2>'+source('history_keep')+'</div>'+field('已完成任务数量','setting-history',d.history_keep,'未完成、结果不明及待恢复任务不会自动清理。','number')+'</div><div class="settings-section"><div><h2>自动检查更新</h2>'+source('auto_update')+'</div><div><label><input id="setting-update" type="checkbox" '+(d.auto_update?'checked':'')+' '+(b.saveScope==='project'?'disabled':'')+'> 自动检查新版本</label><p class="small muted">此项仅保存在个人设置；安装更新仍须确认。</p></div></div></section><div class="save-dock"><div><p id="settings-dirty">'+settingsChanges().length+' 项未保存修改</p><span id="settings-result" class="file-path" role="status"></span></div>'+button('保存设置','settings-save','primary',!settingsChanges().length)+'</div></div>';
}
function taskView(){
  const task=currentTask();if(!task||state.page==='settings'||state.page==='detect')return '';
  let controls=button('刷新任务','task-refresh-view','text');
  if(task.status==='waiting_agent')controls+=button('复制 Agent 任务提示词','task-prompt','primary');
  if(['pending_confirmation','confirmed'].includes(task.status))controls+=button(task.status==='confirmed'?'执行已确认计划':'确认并应用计划','task-apply','primary',bucket().candidateDrafts[task.id]!==undefined&&bucket().candidateDrafts[task.id]!==task.candidate);
  if(task.status==='stale')controls+=button('重读输入并保留候选','task-replan');
  let body='';
  if(task.kind==='sync'&&task.candidate!==undefined&&task.status==='pending_confirmation')body+=area('候选正文','task-candidate',bucket().candidateDrafts[task.id]??task.candidate)+button('更新候选并重算计划','task-candidate-save');
  if(task.kind==='review'&&task.suggestions&&!task.suggestions_expired){
    body+=task.suggestions.map(s=>'<section class="review-item"><label><input type="checkbox" data-suggestion="'+escape(s.id)+'" '+(task.selected?.includes(s.id)?'checked':'')+'> '+escape(s.recommendation)+'</label><p>位置：'+escape(s.path)+' · '+s.line+' 行</p><p>依据：'+escape(s.evidence.join('；')||'待核实')+'</p><p>影响：'+escape(s.impact)+'</p>'+badge({pending:'待验证',text:'文本依据',behavior:'行为依据'}[s.verification]||s.verification,'blue')+'<details open><summary>候选差异</summary><pre>'+escape(s.diff??'当前记录缺少差异，请让 Agent 按当前输入修订重新回传。')+'</pre></details><details><summary>候选正文</summary><pre>'+escape(s.content)+'</pre></details></section>').join('')+button('将所选建议组成计划','review-select','primary');
  }
  if(task.writes)body+=task.writes.map(w=>'<details class="plan-file" open><summary>'+escape(w.path)+' · '+escape(w.op||'write')+'</summary>'+(w.target?'<p>入口目标：'+escape(w.target)+'</p>':'')+'<pre>'+escape(w.diff||w.content||'此操作只调整入口或目录')+'</pre></details>').join('');
  if(task.shared_impacts?.length)body+=notice('共享入口联动：'+task.shared_impacts.map(g=>g.entrances.join('、')).join('；'));
  if(task.status==='stale')body+=notice('输入已变化，旧确认已失效。原候选仍保留；先重新核对。');
  if(task.receipt)body+=['written','unwritten','restored','uncertain','recovery_required'].map((key,i)=>task.receipt[key]?.length?'<p>'+['已写入','未写入','已恢复','结果不明','待恢复'][i]+'：'+escape(task.receipt[key].join('、'))+'</p>':'').join('')+'<p class="small muted">实际加载：未验证 · 行为结果：未验证</p>';
  if(task.history_warning)body+=notice(task.history_warning.message);
  return '<section class="card below" aria-label="当前维护任务"><div class="card-head"><div><h2>'+escape(task.intent)+'</h2><span class="file-path">'+escape(task.id)+'</span></div>'+badge(statusNames[task.status]||task.status,'blue')+'</div><div class="card-body">'+body+'</div><div class="card-foot actions">'+controls+'</div></section>';
}
function bindingControls(){
  const b=bucket(),binding=b.config?.values.binding;
  if(!binding||['detect','settings','history'].includes(state.page))return '';
  return '<div class="inline-note"><p>管理绑定：'+escape([binding.product,binding.installation,binding.profile,binding.layout].filter(Boolean).join(' · ')||'所选规则路径')+'</p><label><input id="task-paths-only" type="checkbox" '+(b.pathsOnly?'checked':'')+'> 仅按本次选择的路径操作，不沿用已保存的管理绑定</label></div>';
}
function captureSettings(){
  const b=bucket(),value=id=>root.querySelector('#'+id)?.value??'';
  b.settingsDraft.history_keep=Number(value('setting-history'));
  b.settingsDraft.review_models=lines(value('setting-models'));
  b.settingsDraft.auto_update=root.querySelector('#setting-update').checked;
  b.settingsDraft.binding=root.querySelector('#binding-enabled').checked?{scope:state.scope,project:state.scope==='project'?state.project:null,
    product:value('binding-product')||null,installation:value('binding-installation')||null,profile:value('binding-profile')||null,
    paths:lines(value('binding-paths')),authority:value('binding-authority')||null,layout:value('binding-layout')||null}:null;
  root.querySelector('#settings-dirty').textContent=settingsChanges().length+' 项未保存修改';
  root.querySelector('[data-action="settings-save"]').disabled=!settingsChanges().length;
}
async function copy(text){try{await navigator.clipboard.writeText(text);toast('已复制。');}catch{openDialog('复制文本',area('请求内容','copy-content',text));}}
async function namedDraft(path,text,b,project){
  const result=await call('scenario.title',{content:text},project);
  if(b.drafts[path]!==text)return;
  b.titles[path]=result.title||'未命名场景';
  if(b!==bucket())return;
  const row=[...root.querySelectorAll('.tree-file')].find(x=>x.dataset.action==='scenario:'+encodeURIComponent(path));
  if(row){row.querySelector('strong').textContent=b.titles[path];row.querySelector('.draft-dot').hidden=false;row.title=b.titles[path]+' · '+path;}
  if(b.selected===path){
    const name=root.querySelector('#scenario-name');if(name)name.textContent=b.titles[path];
    const label=root.querySelector('#scenario-draft-state');if(label)label.hidden=false;
  }
}
root.addEventListener('input',event=>{
  const el=event.target,b=bucket();
  if(state.page==='settings')captureSettings();
  if(el.id==='scenario-text'){
    const item=currentScenario(),project=state.project,text=el.value;
    delete b.tasks.scenarios;root.querySelector('[aria-label="当前维护任务"]')?.remove();updateURL();
    if(text===item.content){delete b.drafts[item.path];delete b.titles[item.path];render();}
    else{b.drafts[item.path]=text;clearTimeout(titleTimer);titleTimer=setTimeout(()=>namedDraft(item.path,text,b,project).catch(e=>toast(e.message)),180);}
  }
  if(el.id==='task-candidate'){
    const task=currentTask();b.candidateDrafts[task.id]=el.value;
    const apply=root.querySelector('[data-action="task-apply"]');if(apply)apply.disabled=el.value!==task.candidate;
  }
  if(el.id==='review-models')b.models=lines(el.value);
});
root.addEventListener('scroll',event=>{if(event.target.classList?.contains('scenario-tree-scroll'))bucket().treeScroll=event.target.scrollTop;},true);
root.addEventListener('change',event=>{
  const el=event.target,b=bucket();
  if(el.id==='task-paths-only'){b.pathsOnly=el.checked;delete b.tasks[state.page];render();}
  if(el.id==='save-scope'){b.saveScope=el.value;render();}
  if(el.id==='sync-authority'){b.authority=el.value;delete b.targets[el.value];delete b.tasks.sync;render();}
  if(el.dataset.syncPath){if(el.checked)b.targets[el.dataset.syncPath]=state.workspace.main_files.find(f=>f.path===el.dataset.syncPath)?.content?'merge':'empty';else delete b.targets[el.dataset.syncPath];delete b.tasks.sync;render();}
  if(el.dataset.strategyPath){b.targets[el.dataset.strategyPath]=el.value;delete b.tasks.sync;render();}
  if(el.dataset.reviewPath){el.checked?b.reviewPaths.add(el.dataset.reviewPath):b.reviewPaths.delete(el.dataset.reviewPath);delete b.tasks.review;render();}
  if(el.dataset.suggestion){const apply=root.querySelector('[data-action="task-apply"]');if(apply)apply.disabled=true;}
});
async function act(action){
  state.message='';
  const b=bucket(),item=currentScenario(),task=currentTask(),known=state.workspace.files.filter(f=>f.status!=='unreadable').map(f=>f.path);
  const taskData={scope:scopeValue(),known_paths:known,...(!b.config.values.binding||b.pathsOnly?{binding:null}:{})};
  const scenarioData={...taskData,rules_root:state.workspace.root,path:item?.path,entrances:known};
  if(action==='close'){dialog.close();return;}
  if(action.startsWith('page:')){state.page=action.slice(5);if(state.page==='history')await refreshHistory();render();window.scrollTo(0,0);return;}
  if(action.startsWith('scope:')){state.scope=action.slice(6);await loadWorkspace();render();window.scrollTo(0,0);return;}
  if(action==='reload'){await loadWorkspace();render();return;}
  if(action==='project-picker'){openDialog('选择项目',field('项目目录','project-path',state.project,'可指定其他项目或项目内子目录。'),button('取消','close')+button('读取项目','project-select','primary'));return;}
  if(action==='project-select'){const project=dialog.querySelector('#project-path').value.trim();const next=await call('workspace.read',{scope:'project'},project);state.project=next.project.path;state.scope=next.project.availability==='ready'?'project':'global';state.workspace=next;await loadWorkspace();dialog.close();render();return;}
  if(action==='project-create'){openDialog('建立项目规则','<p>写入位置：<code>'+escape(pathJoin(state.project,'AGENTS.md'))+'</code></p>'+area('初始主规则','project-body','# 项目规则\n\n'),'创建前会先展示具体计划。'+button('取消','close')+button('预览建立计划','project-create-plan','primary'));return;}
  if(action==='project-create-plan'){const path=pathJoin(state.project,'AGENTS.md'),before=await call('file.read',{path});if(before.exists)throw new Error('主规则已经存在，请重新检测并编辑现有文件。');const result=await call('task.create',{kind:'edit',scope:state.project,binding:null,intent:'建立项目规则',changes:[{path,content:dialog.querySelector('#project-body').value}]});state.scope='project';state.page='history';remember(result);dialog.close();await loadWorkspace();render();return;}
  if(action==='detect'){b.discovery=await call('discover.scan');render();return;}
  if(action==='detect-custom'){openDialog('补充自定义位置',select('Agent','custom-product',[['codex','Codex'],['claude-code','Claude Code'],['workbuddy','WorkBuddy'],['hermes','Hermes'],['opencode','OpenCode']],'codex')+field('安装路径（可留空）','custom-install')+area('规则文件路径','custom-paths',''),button('取消','close')+button('检测这些位置','detect-custom-run','primary'));return;}
  if(action==='detect-custom-run'){const custom={product:dialog.querySelector('#custom-product').value,paths:lines(dialog.querySelector('#custom-paths').value)};const install=dialog.querySelector('#custom-install').value.trim();if(install)custom.installation=install;b.discovery=await call('discover.scan',{custom:[custom]});dialog.close();render();return;}
  if(action==='sync-add'){openDialog('补充规则文件',field('规则文件完整路径','sync-path'),button('取消','close')+button('读取此文件','sync-add-read','primary'));return;}
  if(action==='authority-edit'){const file=await call('file.read',{path:b.authority});openDialog('编辑权威正文',area('主规则正文','authority-body',file.content),button('取消','close')+button('预览主规则修改','authority-plan','primary'));return;}
  if(action==='authority-plan'){remember(await call('task.create',{...taskData,kind:'edit',intent:'修改权威主规则',changes:[{path:b.authority,content:dialog.querySelector('#authority-body').value}]}));dialog.close();render();return;}
  if(action==='sync-add-read'){const file=await call('file.read',{path:dialog.querySelector('#sync-path').value.trim()});if(file.directory)throw new Error('请选择文件。');if(!state.workspace.main_files.some(f=>f.path===file.path))state.workspace.main_files.push({...file,status:file.exists?'found':'missing',role:'main'});dialog.close();render();return;}
  if(action==='sync-create'){remember(await call('task.create',{...taskData,kind:'sync',authority:b.authority,targets:Object.entries(b.targets).map(([path,strategy])=>({path,strategy})),
    classifications:state.workspace.files.filter(f=>['identity','memory'].includes(f.role)&&(f.path===b.authority||b.targets[f.path])).map(f=>({path:f.path,role:f.role,evidence:'Known host file role'}))}));
    if(emptyBatch()&&currentTask().status==='pending_confirmation')return act('task-apply');
    render();return;}
  if(action.startsWith('folder:')){const path=decodeURIComponent(action.slice(7));b.collapsed.has(path)?b.collapsed.delete(path):b.collapsed.add(path);render();return;}
  if(action==='folders-expand'||action==='folders-collapse'){b.collapsed.clear();if(action==='folders-collapse')for(const f of state.workspace.scenarios){const parts=f.relative_path.split('/');for(let i=1;i<parts.length;i++)b.collapsed.add(parts.slice(0,i).join('/'));}render();return;}
  if(action.startsWith('scenario:')){b.selected=decodeURIComponent(action.slice(9));render();return;}
  if(action==='scenario-new'){openDialog('新增场景',field('场景名称','new-name','','将写入正文一级标题。')+field('相对文件路径','new-path','new-scenario.md')+field('读取触发条件','new-trigger')+select('建立读取入口的主规则','new-entry',state.workspace.main_files.filter(f=>f.status==='found').map(f=>[f.path,f.path]),b.authority)+area('规则正文','new-content',''),button('取消','close')+button('预览新增计划','scenario-add-plan','primary'));return;}
  if(action==='scenario-add-plan'){const title=dialog.querySelector('#new-name').value.trim();if(!title)throw new Error('请填写场景名称。');remember(await call('scenario.plan',{...taskData,rules_root:state.workspace.root,operation:'add',
    path:pathJoin(state.workspace.scenario_root,dialog.querySelector('#new-path').value.trim()),entry:dialog.querySelector('#new-entry').value,trigger:dialog.querySelector('#new-trigger').value.trim(),content:'# '+title+'\n\n'+dialog.querySelector('#new-content').value}));dialog.close();render();return;}
  if(action==='scenario-edit'){remember(await call('scenario.plan',{...scenarioData,operation:'edit',content:b.drafts[item.path]??item.content}));render();return;}
  if(action==='scenario-rename'){openDialog('重命名场景文件',field('新文件完整路径','rename-path',item.path,'场景名称仍取自正文标题；已知读取入口一起调整。'),button('取消','close')+button('预览重命名计划','scenario-rename-plan','primary'));return;}
  if(action==='scenario-rename-plan'){remember(await call('scenario.plan',{...scenarioData,operation:'rename',destination:dialog.querySelector('#rename-path').value.trim()}));dialog.close();render();return;}
  if(action==='scenario-delete'){const references=readers(item);openDialog('删除场景及调整引用',notice('将删除 '+item.path+'。请同步修改以下引用文件，移除该场景的读取入口。')+references.map((f,i)=>area(f.path,'delete-ref-'+i,f.content)).join(''),button('取消','close')+button('预览删除计划','scenario-delete-plan','danger'));return;}
  if(action==='scenario-delete-plan'){remember(await call('scenario.plan',{...scenarioData,operation:'delete',reference_changes:readers(item).map((f,i)=>({path:f.path,content:dialog.querySelector('#delete-ref-'+i).value}))}));dialog.close();render();return;}
  if(action.startsWith('identity:')){const f=await call('file.read',{path:decodeURIComponent(action.slice(9))});state.identityPath=f.path;openDialog('单独编辑身份文件',area('正文','identity-body',f.content),button('取消','close')+button('预览单文件计划','identity-plan','primary'));return;}
  if(action==='identity-plan'){remember(await call('task.create',{...taskData,kind:'edit',intent:'单独编辑身份文件',changes:[{path:state.identityPath,content:dialog.querySelector('#identity-body').value}]}));dialog.close();render();return;}
  if(action==='layout-new'){openDialog('预览布局调整',select('现有主规则','layout-main',state.workspace.main_files.filter(f=>f.status==='found').map(f=>[f.path,f.path]),b.authority)+field('现有场景目录（可留空）','layout-scenes',state.workspace.scenario_root)+field('新物理根目录','layout-root',state.workspace.root)+area('需要链接到新主规则的入口','layout-entrances','', '每行一个路径；仅列出你希望调整的入口。')+area('需要链接到新场景目录的入口','layout-scenario-entrances',''),button('取消','close')+button('生成具体计划','layout-plan','primary'));return;}
  if(action==='layout-plan'){remember(await call('layout.plan',{...taskData,source_main:dialog.querySelector('#layout-main').value,source_scenarios:dialog.querySelector('#layout-scenes').value.trim()||undefined,
    root:dialog.querySelector('#layout-root').value.trim(),entrances:lines(dialog.querySelector('#layout-entrances').value),scenario_entrances:lines(dialog.querySelector('#layout-scenario-entrances').value)}));dialog.close();render();return;}
  if(action==='review-create'){remember(await call('task.create',{...taskData,kind:'review',paths:[...b.reviewPaths],models:{targets:b.models??b.config.values.review_models}}));render();return;}
  if(action==='review-select'){remember(await call('task.select',{task_id:task.id,suggestions:[...root.querySelectorAll('[data-suggestion]:checked')].map(x=>x.dataset.suggestion)}));render();return;}
  if(action==='task-prompt'){await copy((await call('task.prompt',{task_id:task.id})).prompt);return;}
  if(action==='task-refresh-view'){remember(await call('task.get',{task_id:task.id}));if(currentTask().status==='completed')await loadWorkspace();render();return;}
  if(action==='task-candidate-save'){remember(await call('task.result',{task_id:task.id,input_revision:task.input_revision,content:root.querySelector('#task-candidate').value}));delete b.candidateDrafts[task.id];render();return;}
  if(action==='task-replan'){remember(await call('task.refresh',{task_id:task.id}));render();return;}
  if(action==='task-apply'){
    if(task.status==='pending_confirmation')remember(await call('task.confirm',{task_id:task.id,plan_revision:task.plan_revision}));
    const result=await call('task.apply',{task_id:task.id});remember(result);
    if(result.status==='completed'){for(const write of result.writes){delete b.drafts[write.path];delete b.titles[write.path];}await loadWorkspace();await refreshHistory();}
    render();toast(statusNames[result.status]||result.status);return;
  }
  if(action==='history-refresh'){await refreshHistory();render();return;}
  if(action.startsWith('task-open:')){remember(await call('task.get',{task_id:action.slice(10)}));render();return;}
  if(action.startsWith('restore:')){remember(await call('history.restore',{task_id:action.slice(8)}));render();return;}
  if(action==='history-prune'){openDialog('清理已完成历史',field('每个范围保留的已完成任务数','prune-count',b.config.values.history_keep,'只清理当前范围；未完成和待恢复任务保留。','number'),button('取消','close')+button('清理当前范围','history-prune-run','danger'));return;}
  if(action==='history-prune-run'){const result=await call('history.prune',{scope:scopeValue(),keep:Number(dialog.querySelector('#prune-count').value)});dialog.close();await refreshHistory();render();toast('已清理 '+result.removed.length+' 个已完成任务。');return;}
  if(action==='settings-reload'){
    captureSettings();const changes=Object.fromEntries(settingsChanges().map(key=>[key,b.settingsDraft[key]]));
    b.config=await call('config.read',{scope:scopeValue()});b.settingsDraft={...structuredClone(b.config.values),...changes};
    state.message='';render();root.querySelector('#settings-result').textContent='已重读最新来源，并保留本次编辑。请核对后保存。';return;
  }
  if(action==='settings-save'){
    captureSettings();const changes=Object.fromEntries(settingsChanges().map(key=>[key,b.settingsDraft[key]]));
    const saved=await call('config.save',{scope:scopeValue(),save_scope:b.saveScope,revision:b.config.revision,values:changes});
    b.config=saved;b.settingsDraft=structuredClone(saved.values);
    if('binding' in changes){b.authority=saved.values.binding?.authority||'';await loadWorkspace();}
    render();
    root.querySelector('#settings-result').textContent='已保存到 '+saved.saved_to+(saved.overridden.length?'；部分值仍被高优先级来源覆盖':'');return;
  }
  if(action==='stop'){await api('/api/stop',{});state.stopped=true;root.innerHTML='<div class="loading"><h1>工作台已结束</h1><p>任务和已保存记录仍保留，可让 Agent 重新打开。</p></div>';return;}
}
document.addEventListener('click',event=>{
  const target=event.target.closest('[data-action]');if(!target||state.busy||state.stopped)return;
  state.busy=true;root.setAttribute('aria-busy','true');
  act(target.dataset.action).catch(async error=>{
    state.message=error.message;toast(error.message);
    if(target.dataset.action==='task-apply'&&currentTask()){try{remember(await call('task.get',{task_id:currentTask().id}));render();}catch{}}
    const result=root.querySelector('#settings-result');if(result)result.textContent=error.message;
  }).finally(()=>{state.busy=false;root.removeAttribute('aria-busy');});
});
async function activity(){if(state.stopped||!state.csrf||Date.now()-activityAt<25000)return;activityAt=Date.now();try{state.lifetime=await api('/api/activity',{});}catch(error){state.stopped=true;toast(error.message);}}
for(const event of ['pointerdown','keydown','scroll'])document.addEventListener(event,activity,{passive:true,capture:true});
async function start(){
  try{
    if(location.hash){state.csrf=(await api('/api/session',{token:location.hash.slice(1)})).csrf;history.replaceState(null,'',location.pathname+location.search);}
    const initial=await api('/api/state');state.csrf=initial.csrf;state.personal=initial.personal;state.lifetime=initial.lifetime;state.project=state.project||initial.project;
    if(query.get('task')){
      const task=await call('task.get',{task_id:query.get('task')});
      state.scope=task.scope==='global'?'global':'project';
      if(state.scope==='project')state.project=task.project;
      remember(task);
    }
    await loadWorkspace();
    if(state.page==='history')await refreshHistory();
    render();
    setInterval(async()=>{if(state.stopped)return;try{state.lifetime=await api('/api/lifetime');const node=document.querySelector('#lifetime');if(node)node.textContent=lifecycle();}catch(error){state.stopped=true;toast(error.message);}},30000);
  }catch(error){root.innerHTML='<div class="loading"><h1>无法打开工作台</h1><p>'+escape(error.message)+'</p><p>请核对配置和目录，或让 Agent 提供新的访问链接。</p></div>';}
}
start();
