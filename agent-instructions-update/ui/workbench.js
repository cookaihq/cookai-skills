// The workbench uses the same persistent tasks as the Agent entry point.
const boards = new Map();
const statuses = {waiting_agent:'等待 Agent 返回候选', candidate_review:'审阅结果待核对',
  pending_confirmation:'计划待确认', confirmed:'已确认，可执行', applying:'执行中',
  completed:'已完成', stale:'输入已变化，候选已保留', rolled_back:'执行失败，已恢复', recovery_required:'需要恢复'};
const lines = text => String(text || '').split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
function node(tag, text='', attrs={}) {
  const result=document.createElement(tag); result.textContent=text;
  for(const [key,value] of Object.entries(attrs)) result.setAttribute(key,value);
  return result;
}

export function register(add) {
  if(!document.querySelector('link[data-rules-workbench]')) {
    const style=node('link','',{rel:'stylesheet',href:'/extensions/workbench.css','data-rules-workbench':'1'});
    document.head.append(style);
  }
  add('rules-workbench', (container, data)=> {
    let state=boards.get(data.project);
    if(!state) {
      state={page:'主规则同步', scope:'project', fields:{}, inspected:[], strategies:{}, index:[], task:null, message:'', busy:false};
      boards.set(data.project,state);
    }
    async function call(action, value={}) {
      const response=await fetch('/api/state'); const session=await response.json();
      if(!response.ok) throw new Error(session.message || '页面访问已结束，请重新打开。');
      const result=await fetch('/api/rules', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},
        body:JSON.stringify({action,data:value})});
      const payload=await result.json(); if(!result.ok) throw new Error(payload.message || '操作未完成，请重新读取任务。');
      return payload.result;
    }
    function remember(task) {
      state.task=task; state.fields.task_id=task.id;
      const url=new URL(location.href); url.searchParams.set('task',task.id); history.replaceState({},'',url);
    }
    async function perform(work) {
      if(state.busy) return;
      state.busy=true; state.message='正在读取或处理…';
      container.querySelectorAll('input,textarea,select,button').forEach(x=>x.disabled=true);
      try {await work(); state.message='已更新。';}
      catch(error) {state.message=error.message;}
      finally {state.busy=false; render();}
    }
    function button(parent,label,work,selected=false) {
      const result=node('button',label,{type:'button'}); if(selected) result.className='selected';
      result.addEventListener('click',()=>perform(work)); parent.append(result); return result;
    }
    function field(parent,label,key,type='text') {
      const wrap=node('label',label,{class:'field'});
      const input=node(type==='textarea'?'textarea':'input','',{'data-field':key});
      if(type!=='textarea') input.type=type;
      input.value=state.fields[key] || ''; input.addEventListener('input',()=>state.fields[key]=input.value);
      wrap.append(input); parent.append(wrap); return input;
    }
    function box(parent,title) {const result=node('section','',{class:'box'}); if(title) result.append(node('h4',title)); parent.append(result); return result;}
    function text(parent,value,cls='muted') {parent.append(node('p',value,{class:cls}));}
    function content(parent,value) {parent.append(node('pre',value || '（空正文）'));}
    function scope() {return state.scope==='global'?'global':data.project;}
    function required(key,label) {const value=(state.fields[key] || '').trim(); if(!value) throw new Error('请填写'+label+'。'); return value;}
    function taskArgs() {if(!state.task) throw new Error('请先建立或读取维护任务。'); return {task_id:state.task.id};}
    async function refreshComparisons() {
      const compared=state.inspected, aliases=state.observed || [];
      if(!compared.length) return;
      // Never display old bodies as the result if a fresh read fails.
      state.inspected=[]; state.observed=[];
      const snapshots=await Promise.all([...compared,...aliases].map(x=>call('file.read',{path:x.path})));
      state.inspected=snapshots.slice(0,compared.length); state.observed=snapshots.slice(compared.length);
      for(const record of state.inspected.slice(1))
        if(record.content && state.strategies[record.path]==='empty') state.strategies[record.path]='ignore';
    }
    async function applyTask() {
      remember(await call('task.apply',taskArgs()));
      await refreshComparisons();
    }
    async function loadTask() {
      remember(await call('task.get',{task_id:required('task_id','任务标识')}));
      if(['completed','rolled_back','recovery_required'].includes(state.task.status)) await refreshComparisons();
    }
    async function planSync(quick=false) {
      if(!state.inspected.length) throw new Error('请先读取文件并比较。');
      const [authority,...targets]=state.inspected;
      if(state.fields.authority!==state.inspectedAuthority || state.fields.targets!==state.inspectedTargets || state.fields.aliases!==state.inspectedAliases)
        throw new Error('文件清单已变化，请重新读取并比较。');
      const selected=targets.filter(x=>state.strategies[x.path] && state.strategies[x.path]!=='ignore');
      if(!selected.length) throw new Error('请选择本次同步目标。');
      if(quick && selected.some(x=>state.strategies[x.path]!=='empty')) throw new Error('此操作只用于已选空目标。');
      const task=await call('task.create',{kind:'sync',scope:scope(),authority:authority.path,
        targets:selected.map(x=>({path:x.path,strategy:state.strategies[x.path]})),
        known_paths:[...state.inspected.map(x=>x.path),...(state.observed || []).map(x=>x.path)]});
      remember(task);
      if(quick) {
        const shown=[...state.inspected,...(state.observed || [])];
        const same=[...task.inputs,...task.observed].every(x=>shown.some(before=>JSON.stringify(before)===JSON.stringify(x)));
        if(!same || task.status!=='pending_confirmation') throw new Error('现场已变化，已保留新计划，请核对后确认。');
        remember(await call('task.confirm',{task_id:task.id,plan_revision:task.plan_revision}));
        await applyTask();
      }
    }
    function syncView(parent) {
      const form=box(parent,'选择权威文件与目标');
      field(form,'权威文件的完整路径','authority');
      field(form,'目标文件路径，每行一个','targets','textarea');
      field(form,'其他已知共享入口，每行一个（可选）','aliases','textarea');
      const bar=node('div','',{class:'bar'}); form.append(bar);
      button(bar,'读取并比较',async()=>{
        const paths=[required('authority','权威文件'),...lines(state.fields.targets)];
        if(new Set(paths).size!==paths.length) throw new Error('清单中有重复路径，请先去重。');
        state.inspected=[]; state.observed=[];
        const snapshots=await Promise.all([...paths,...lines(state.fields.aliases)].map(path=>call('file.read',{path})));
        state.inspected=snapshots.slice(0,paths.length); state.observed=snapshots.slice(paths.length);
        state.inspectedAuthority=state.fields.authority; state.inspectedTargets=state.fields.targets;
        state.inspectedAliases=state.fields.aliases;
        state.strategies=Object.fromEntries(state.inspected.slice(1).map(x=>[x.path,!x.content?'empty':'ignore']));
      });
      if(!state.inspected.length) return;
      const authority=state.inspected[0];
      for(const alias of state.observed || []) {
        const shared=state.inspected.filter(x=>JSON.stringify(x.identity)===JSON.stringify(alias.identity)&&x.identity);
        text(parent,alias.path+'：'+(shared.length?'与 '+shared.map(x=>x.path).join('、')+' 共享实体，写入会联动':'当前未与所选文件共享实体'),'warning');
      }
      const columns=node('div','',{class:'cards'}); parent.append(columns);
      state.inspected.forEach((record,index)=>{
        const card=box(columns,index===0?'权威文件':record.content===authority.content?'相同正文':record.content?'不同正文':'空目标');
        text(card,record.path,'path'); text(card,'实体：'+record.entity); text(card,'入口：'+record.kind+' · 加载与行为未验证'); content(card,record.content);
        if(index) {
          const label=node('label','本次处理方式',{class:'field'}), select=node('select','',{'aria-label':record.path+' 的处理方式'});
          for(const [value,title] of [['ignore','不选择'],['empty','同步空目标'],['merge','参与合并'],['overwrite','覆盖旧正文']]) {
            const option=node('option',title,{value}); option.disabled=value==='empty'&&!!record.content; select.append(option);
          }
          select.value=state.strategies[record.path]; select.addEventListener('change',()=>state.strategies[record.path]=select.value); label.append(select); card.append(label);
        }
      });
      const actions=node('div','',{class:'bar'}); parent.append(actions);
      button(actions,'形成同步计划',()=>planSync());
      button(actions,'同步已选空目标',()=>planSync(true));
      text(parent,'选择合并后整批等待 Agent 候选；覆盖目标的旧正文不会进入合并输入。');
    }
    function scenarioView(parent) {
      const form=box(parent,'场景文件与读取入口');
      if(state.scope==='global') field(form,'全局物理规则根目录','rules_root');
      field(form,'额外读取入口路径，每行一个（可选）','scenario_entrances','textarea');
      button(form,'读取规则索引',async()=>{
        const paths=lines(state.fields.scenario_entrances);
        if(state.scope==='global') paths.push(required('rules_root','全局规则根目录').replace(/[\\/]$/,'')+'/AGENTS.md');
        const result=await call('index.read',{paths}); state.index=result.files;
        if(result.errors?.length) throw new Error('部分目录不可读，索引不完整：'+result.errors.join('、'));
      });
      if(state.index.length) {
        const list=box(parent,'规则文件与引用');
        for(const record of state.index) {
          const row=node('div','',{class:'row'}), body=node('div','',{class:'grow'});
          text(body,record.path,'path'); text(body,`${record.status} · ${record.role || '职责待核对'} · 作用范围 ${record.scope || '待核对'}`);
          text(body,(record.references || []).map(x=>'第 '+x.line+' 行 → '+(x.target || '基准待核对：'+x.raw)).join('\n') || '未解析到静态引用'); row.append(body);
          button(row,'选择文件',async()=>{const file=await call('file.read',{path:record.path}); state.fields.scenario_path=file.path; state.fields.scenario_content=file.content; state.selectedEntity=file.entity;});
          list.append(row);
        }
      }
      const editor=box(parent,'编辑与场景操作');
      field(editor,'场景文件路径','scenario_path'); field(editor,'候选正文','scenario_content','textarea');
      const args=()=>({scope:scope(),path:required('scenario_path','场景文件路径'),
        ...(state.scope==='global'?{rules_root:required('rules_root','全局规则根目录')} : {}), entrances:lines(state.fields.scenario_entrances),
        known_paths:state.index.filter(x=>x.status!=='unreadable').map(x=>x.path)});
      button(editor,'预览正文修改',async()=>remember(await call('scenario.plan',{...args(),operation:'edit',content:state.fields.scenario_content || ''})));
      const add=box(parent,'新增场景');
      field(add,'主规则入口路径','scenario_entry'); field(add,'场景名称','scenario_title'); field(add,'读取时机与触发条件','scenario_trigger');
      button(add,'预览新增场景与入口',async()=>remember(await call('scenario.plan',{...args(),operation:'add',entry:required('scenario_entry','主规则入口'),
        title:required('scenario_title','场景名称'),trigger:required('scenario_trigger','读取条件'),content:state.fields.scenario_content || ''})));
      const rename=box(parent,'重命名场景'); field(rename,'新文件路径','scenario_destination');
      button(rename,'预览重命名与引用修改',async()=>remember(await call('scenario.plan',{...args(),operation:'rename',destination:required('scenario_destination','新文件路径')})));
      const deletion=box(parent,'删除场景');
      text(deletion,'先读取索引并选择文件。被引用的文件需要同时修订以下入口正文。');
      const references=state.index.filter(x=>x.path!==state.fields.scenario_path && (x.references || []).some(ref=>ref.target===state.selectedEntity));
      const affected=[...new Map(references.map(x=>[x.identity?JSON.stringify(x.identity):x.path,x])).values()];
      for(const record of affected) {const key='reference:'+record.path; if(state.fields[key]===undefined) state.fields[key]=record.content; field(deletion,record.path,key,'textarea');}
      button(deletion,'预览删除及引用修订',async()=>remember(await call('scenario.plan',{...args(),operation:'delete',
        reference_changes:affected.map(record=>({path:record.path,content:state.fields['reference:'+record.path]}))})));
    }
    function layoutView(parent) {
      const form=box(parent,'物理存储与宿主入口');
      text(form,'物理根目录保存 AGENTS.md 与 scenarios-agent-instructions。此处形成迁移与链接计划；确认后才执行。');
      field(form,'选择物理根目录','layout_root'); field(form,'现有主规则文件','layout_main');
      field(form,'现有场景目录（可选）','layout_scenarios');
      field(form,'主规则入口，每行一个','layout_entrances','textarea');
      field(form,'其他场景目录入口，每行一个（可选）','layout_scenario_entrances','textarea');
      button(form,'预览布局修改',async()=>remember(await call('layout.plan',{scope:scope(),root:required('layout_root','物理根目录'),
        source_main:required('layout_main','现有主规则'),source_scenarios:state.fields.layout_scenarios || undefined,
        entrances:lines(state.fields.layout_entrances),scenario_entrances:lines(state.fields.layout_scenario_entrances)})));
    }
    function taskView(parent) {
      const section=box(parent,'维护任务'); field(section,'任务标识（可接续聊天中建立的任务）','task_id');
      button(section,'读取当前任务',loadTask);
      const task=state.task; if(!task) return;
      text(section,statuses[task.status] || task.status,'notice'); text(section,task.id+' · '+task.scope);
      if(task.intent) text(section,task.intent);
      if(task.binding) text(section,`管理对象：${task.binding.product || '未知'} · 安装：${task.binding.installation || '未核实'} · Profile：${task.binding.profile || '未核实'}`);
      if(task.changed?.length) text(section,'已变化：'+task.changed.join('、'),'warning');
      if(task.kind==='review') {text(section,'该审阅任务请通过 Agent 对话接续。'); return;}
      if(task.merge_inputs?.length) text(section,'合并输入：'+task.merge_inputs.map(x=>x.path).join('、'));
      for(const impact of task.shared_impacts || []) text(section,(impact.effect==='entrance'?'共享入口关系将变化：':'共享正文会联动：')+impact.entrances.join('、'),'warning');
      for(const write of task.writes || []) {
        const detail=node('details','',{open:''}); detail.append(node('summary',(write.op || 'write')+' · '+write.path));
        if(write.target) text(detail,'目标：'+write.target);
        if(write.backup) text(detail,'原目录备份：'+write.backup);
        content(detail,write.diff || '正文没有差异；请核对入口操作。'); section.append(detail);
      }
      const bar=node('div','',{class:'bar'}); section.append(bar);
      if(task.status==='waiting_agent') button(bar,'复制给 Agent 的任务提示',async()=>{
        const result=await call('task.prompt',taskArgs()); state.fields.prompt=result.prompt;
        await navigator.clipboard.writeText(result.prompt);
      });
      if(state.fields.prompt) {const prompt=field(section,'任务提示词','prompt','textarea'); prompt.readOnly=true;}
      if(task.status==='pending_confirmation') button(bar,'确认以上计划并执行',async()=>{
        remember(await call('task.confirm',{...taskArgs(),plan_revision:task.plan_revision})); await applyTask();
      });
      if(task.status==='confirmed') button(bar,'执行已确认计划',applyTask);
      if(task.status==='stale') button(bar,'重读输入并保留候选',async()=>remember(await call('task.refresh',taskArgs())));
      if(task.receipt) {
        for(const [key,label] of [['written','已写入'],['unwritten','尚未写入'],['restored','已恢复'],['uncertain','结果不明'],['recovery_required','待恢复']])
          if(task.receipt[key]?.length) text(section,label+'：'+task.receipt[key].join('、'));
        text(section,'宿主实际加载：'+task.receipt.loading+' · 行为验证：'+task.receipt.behavior);
      }
      if(task.history_warning) text(section,task.history_warning.message,'warning');
    }
    function render() {
      container.replaceChildren(); const board=node('div','',{class:'rules-board'}); container.append(board);
      board.append(node('h3','Agent 规则维护')); text(board,'当前项目：'+data.project,'path');
      const bar=node('div','',{class:'bar'}); board.append(bar);
      for(const [value,label] of [['project','项目规则'],['global','全局规则']]) button(bar,label,async()=>{state.scope=value;state.index=[];},state.scope===value);
      const tabs=node('nav','',{class:'tabs','aria-label':'规则维护视图'}); board.append(tabs);
      for(const page of ['主规则同步','场景规则','存储与入口']) button(tabs,page,async()=>state.page=page,state.page===page);
      if(state.message) text(board,state.message,'notice');
      if(state.page==='主规则同步') syncView(board); else if(state.page==='场景规则') scenarioView(board); else layoutView(board);
      taskView(board);
    }
    const restored=new URL(location.href).searchParams.get('task');
    if(restored && !state.task) {state.fields.task_id=restored; perform(loadTask);} else render();
  });
}
