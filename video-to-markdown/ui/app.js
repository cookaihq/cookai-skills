"use strict";
const $ = id => document.getElementById(id);
const labels = {"main-agent":"主 Agent 看图","host-agent":"宿主子 Agent","independent-agent":"独立 Agent","api":"直接调用 API"};
let snapshot, session, csrf, target="project", projectTarget="project", drafts={}, reveals={}, previews={}, busy=false, previewTimer;
const field = id => snapshot.fields.find(f=>f.id===id);
const row = (id,t=target) => field(id)?.rows.find(r=>r.layer===t);
const draft = () => drafts[target] ||= {};
const visionEdit = id => field(id)?.group==="vision";
const inheritsVision = () => !!draft().inherit || (!row("ask")?.present && !Object.keys(draft()).some(visionEdit));
const previewKey = () => snapshot.revision+JSON.stringify(draft());
const visionView = () => draft().inherit && row("ask")?.present
  ? (previews[target]?.key===previewKey()?previews[target].config:null) : snapshot;
const val = (id,fallback="") => {
  if(visionEdit(id)&&inheritsVision())return visionView()?.fields.find(f=>f.id===id)?.effective.value ?? fallback;
  const op=draft()[id]; if(op) return op.op==="unset"?fallback:op.value;
  return row(id)?.value ?? fallback;
};
function editorState(){
  const inherited=inheritsVision(), view=visionView(), layer=snapshot.layers.find(l=>l.id===target);
  $("inherit").checked=inherited;$("inherit").disabled=layer.readonly;
  $("vision-panel").hidden=inherited&&!view;
  const dirty=Object.keys(draft()).some(visionEdit)||!!draft().inherit;
  const source=view?.fields.find(f=>f.id==="method")?.effective.source;
  const sourceName=snapshot.layers.find(l=>l.id===source)?.name||"默认设置";
  $("vision-state").textContent=inherited
    ? !view?"未保存草稿 · 正在预览移除覆盖后的配置…"
      : (dirty?"未保存草稿 · 移除图像识别覆盖后":"此文件未配置图像识别 · 下方")+"沿用"+sourceName+"；连接信息不会复制到此文件。"
    : (dirty?"未保存草稿 · ":"此文件已保存图像识别配置 · ")+(field("method").effective.source!==target?"当前生效仍来自"+(snapshot.layers.find(l=>l.id===field("method").effective.source)?.name||"默认设置")+"；":"")+"下方编辑此文件的独立配置。";
  $("ask").disabled=inherited||layer.readonly;
}
function el(tag, className, text) {const e=document.createElement(tag); if(className)e.className=className;if(text!=null)e.textContent=text;return e;}
function message(text,error=false) {$("feedback").hidden=false;$("feedback").textContent=text;$("feedback").dataset.error=String(error);}
async function api(path,data) {
  const response=await fetch(path,{method:data?"POST":"GET",credentials:"same-origin",headers:data?{"Content-Type":"application/json","X-CSRF-Token":csrf||""}:{},body:data?JSON.stringify(data):undefined});
  const result=await response.json();if(!response.ok)throw new Error(result.message||"请求失败");return result;
}
function change(id,value,unset=false) {draft()[id]=unset?{op:"unset"}:{op:"set",value};delete previews[target];$("feedback").hidden=true;updatePreview();}
function control(id,{choices=null,placeholder="",type=null}={}) {
  const f=field(id), box=el("label","field",f.label);box.append(el("span","config-key",f.config_key));
  let input;
  if(choices){input=el("select");for(const [value,label] of choices){const option=el("option",null,label);option.value=value;input.append(option);}}
  else {input=el("input");input.type=type||(f.sensitive?"password":f.type==="number"?"number":f.type==="boolean"?"checkbox":"text");input.autocomplete="off";input.placeholder=placeholder;}
  input.id="field-"+id;input.dataset.field=id;
  const inherited=f.group==="vision"&&inheritsVision();
  input.disabled=inherited||snapshot.layers.find(l=>l.id===target).readonly;
  if(f.sensitive){input.value=inherited?"":draft()[id]?.op==="set"?draft()[id].value:(reveals[target]?.[id]||"");input.placeholder=inherited?"沿用来源文件的 Key；不复制":row(id)?.configured?"已配置；留空保留原 Key":"输入 Key";}
  else if(input.type==="checkbox")input.checked=!!val(id,false);
  else input.value=val(id);
  input.addEventListener("input",()=>{
    const value=input.type==="checkbox"?input.checked:input.type==="number"?(input.value?Number(input.value):null):input.value;
    change(id,value,value===null||value===""); if(id==="asr")renderAdvanced();
  });box.append(input);
  if(f.sensitive){const reveal=el("button","text-button","显示 / 隐藏");reveal.type="button";reveal.disabled=input.disabled;reveal.addEventListener("click",async()=>{
    if(input.type==="text"){input.type="password";return;}
    try {if(!input.value&&row(id)?.configured){const r=await api("/api/reveal",{id,target,revision:snapshot.revision});input.value=r.value||"";(reveals[target]||={})[id]=input.value;}input.type="text";}catch(e){message(e.message,true);}
  });box.append(reveal);}
  const source=field(id).effective.source;box.append(el("small",null,inherited?"沿用来源配置；取消上方继承后可单独配置":"当前生效来源："+source+"；编辑此文件"));return box;
}
function renderRoutes(){
  const caps=field("capabilities").effective.value;
  const selected=val("method");$("route-grid").replaceChildren();
  const icons=["◎","⑂","◇","</>"];
  caps.choices.forEach((choice,index)=>{const card=el("label","route-card"),input=el("input");input.type="radio";input.name="route";input.value=choice.method;input.checked=choice.method===selected;input.disabled=inheritsVision()||snapshot.layers.find(l=>l.id===target).readonly;
    const top=el("span","card-top");top.append(el("span","route-icon",icons[index]),el("span","radio-dot"));card.append(input,top,el("strong",null,choice.label),el("span","card-description",choice.reason),el("span","model-hint",choice.available===true?"入口可用；模型按任务核验":choice.available===false?"当前不可用":"能力尚未核验"));
    input.addEventListener("change",()=>{change("method",choice.method);if(["api","independent-agent"].includes(choice.method)){change("protocol",choice.method==="api"?"chat-completions":"responses");if(choice.method==="independent-agent")change("runtime","codex");}renderRoutes();});$("route-grid").append(card);
  });
  const container=$("route-fields");container.replaceChildren();
  if(!selected)container.append(el("p","rule-note","尚未固定识别方式；新任务按当前设置询问。取消上方继承后可在此文件单独配置。"));
  else if(selected==="main-agent")container.append(el("p","rule-note","使用主会话当前模型。需要当前会话确认图片能力；不改变写作或语音模型。"));
  else {
    const models=caps.models||[];
    if(selected==="host-agent"&&models.length){const list=el("div","rule-note");list.append(el("strong",null,"当前宿主报告的模型"));for(const model of models)list.append(el("div",null,(model.name||model.id)+" · "+model.id+" · "+(model.available===false?"不可用":model.vision===false?"不支持图片":model.vision===true&&model.available===true?"可用于看图":"图片或账号能力未核验")));container.append(list);}
    container.append(control("model",{placeholder:selected==="host-agent"?"填写当前宿主支持的模型 ID":"填写服务模型 ID"}));
    if(selected==="host-agent"&&models.length){const list=el("datalist");list.id="host-models";for(const model of models.filter(m=>m.available!==false&&m.vision!==false)){const option=el("option");option.value=model.id;option.label=model.name||model.id;list.append(option);}container.append(list);$("field-model").setAttribute("list",list.id);}
    if(selected!=="host-agent"){
      container.append(control("base_url",{placeholder:"https://service.example/v1"}),control("api_key"));
      if(selected==="independent-agent")container.append(control("runtime",{choices:[["codex","Codex CLI"]]}));
      container.append(el("p","rule-note",selected==="api"?"使用 Chat Completions 图片输入协议。保存只校验配置，不发起付费请求。":"使用 Codex CLI 与 Responses 兼容服务。CLI 内部模型请求次数可能无法计数，实际模型也可能无法核验；启动任务会明确检查。"));
    }
    container.append(control("accept_unverified_model"));
  }
  container.append(el("p","rule-note","能力来源："+caps.source+"。无法取得列表时，只有宿主已确认可指定模型才允许手填；不会静默改用其他模型。"));
}
function renderAdvanced(){
  const box=$("advanced-fields");box.replaceChildren();
  box.append(control("asr",{choices:[["","沿用其他来源（默认 AIhub Audio）"],["aihub-audio","AIhub Audio"],["feishu-minutes","飞书妙记"]]}),control("output"));
  if(val("asr") === "feishu-minutes")for(const id of ["feishu-profile","feishu-app","feishu-user"])box.append(control(id));
  for(const id of ["source","call_limit","ffmpeg","ffprobe","max_images","max_request_bytes","max_context_chars"])box.append(control(id));
}
function renderSources(){
  const box=$("source-list");box.replaceChildren();
  for(const f of snapshot.fields.filter(f=>f.id!=="capabilities")){
    const entry=el("div"),details=el("details"),summary=el("summary",null,f.label+" · 生效来源："+f.effective.source);details.append(summary);
    for(const layer of snapshot.layers){const r=f.rows.find(r=>r.layer===layer.id);const value=f.sensitive?(r.configured?r.mask:"未配置"):r.present?JSON.stringify(r.value):"未配置";details.append(el("p","rule-note",layer.name+" · "+layer.path+" · "+(!layer.enabled?"已关闭，未读取":value)));}entry.append(details);box.append(entry);
  }
}
function render(){
  const selectedLayer=snapshot.layers.find(l=>l.id===target);
  $("project-label").textContent=session.project+" / "+field("capabilities").effective.value.host;
  const effective=id=>field(id).effective.value;
  $("effective-value").textContent=(effective("ask")?"每次新任务询问 · ":"")+(labels[effective("method")]||"尚未固定方式")+(effective("model")?" · "+effective("model"):"");
  $("effective-source").textContent=field("method").effective.source;$("global-note").textContent=snapshot.note;
  $("target-path").textContent=selectedLayer.path;
  $("scope-help").textContent=selectedLayer.readonly?selectedLayer.message:target==="global"?"影响显式启用全局配置的项目；项目覆盖仍优先。":"只影响从当前目录启动的新任务。";
  document.querySelectorAll("[data-scope]").forEach(b=>{const active=(target==="global"?"global":"project")===b.dataset.scope;b.classList.toggle("active",active);b.setAttribute("aria-pressed",active);});
  $("project-file").value=projectTarget;$("project-file").disabled=target==="global";
  renderVision();renderAdvanced();renderSources();updatePreview();
}
function renderVision(){
  editorState();$("ask").checked=!!val("ask",true);$("ask-note").hidden=!$("ask").checked;
  renderRoutes();
}
function updatePreview(){
  $("settings").inert=busy;
  editorState();
  const count=Object.keys(draft()).length;$("save").disabled=busy||snapshot.layers.find(l=>l.id===target).readonly||!count;
  $("save-preview").textContent=count?"保存到 "+snapshot.layers.find(l=>l.id===target).name+" · "+count+" 项修改":"尚未修改";
  clearTimeout(previewTimer);if(!count){$("save-detail").textContent="保存前校验，写入后回读；不会调用模型。";return;}
  const previewTarget=target,revision=snapshot.revision,changes=JSON.parse(JSON.stringify(draft()));
  previewTimer=setTimeout(async()=>{try{const r=await api("/api/preview",{target:previewTarget,changes,revision});if(target!==previewTarget||snapshot.revision!==revision||JSON.stringify(draft())!==JSON.stringify(changes))return;previews[target]={key:previewKey(),config:r.config};const effective=id=>r.config.fields.find(f=>f.id===id).effective.value;const m=r.config.fields.find(f=>f.id==="method");$("save-detail").textContent="保存后生效："+(effective("ask")?"每次新任务询问 · ":"")+(labels[m.effective.value]||"尚未固定方式")+(effective("model")?" · "+effective("model"):"")+" · 来源 "+m.effective.source+"；点击保存才写入";if(changes.inherit){renderVision();renderAdvanced();}}catch(e){if(target===previewTarget&&snapshot.revision===revision&&JSON.stringify(draft())===JSON.stringify(changes))$("save-detail").textContent=e.message;}},250);
}
$("ask").addEventListener("change",()=>{change("ask",$("ask").checked);if(!$("ask").checked&&!val("method"))change("method","main-agent");$("ask-note").hidden=!$("ask").checked;});
$("inherit").addEventListener("change",()=>{
  if($("inherit").checked){for(const k of Object.keys(draft()))if(visionEdit(k))delete draft()[k];if(row("ask")?.present)change("inherit",true);}
  else {delete draft().inherit;if(!row("ask")?.present){change("ask",true);change("method","main-agent");}}
  delete previews[target];render();
});
document.querySelectorAll("[data-scope]").forEach(b=>b.addEventListener("click",()=>{target=b.dataset.scope==="global"?"global":projectTarget;render();}));
$("project-file").addEventListener("change",()=>{projectTarget=target=$("project-file").value;render();});
$("source-jump").addEventListener("click",()=>{$("sources").open=true;$("sources").scrollIntoView({behavior:"smooth"});});
$("reset").addEventListener("click",()=>{drafts[target]={};reveals[target]={};delete previews[target];render();});
$("reload").addEventListener("click",async()=>{try{session=await api("/api/state");snapshot=session.config;reveals={};previews={};render();message("已重新读取，原有草稿仍保留；核对后再保存。");}catch(e){message(e.message,true);}});
$("settings").addEventListener("submit",async e=>{e.preventDefault();if(busy)return;const saveTarget=target,changes=JSON.parse(JSON.stringify(draft())),revision=snapshot.revision;busy=true;updatePreview();try{const r=await api("/api/save",{target:saveTarget,changes,revision});snapshot=r.config;drafts[saveTarget]={};reveals[saveTarget]={};previews={};render();message("已保存并回读："+snapshot.layers.find(l=>l.id===saveTarget).path+"。当前生效来源见页面上方。");}catch(error){message(error.message,true);}finally{busy=false;updatePreview();}});
let lastActivity=0;for(const event of ["pointerdown","keydown","input"])document.addEventListener(event,()=>{if(csrf&&Date.now()-lastActivity>10000){lastActivity=Date.now();api("/api/activity",{}).catch(()=>{});}}, {passive:true});
$("stop").addEventListener("click",async()=>{try{await api("/api/stop",{});message("配置服务已关闭。已保存的配置保留；可通过 Skill 再次打开。");}catch(e){message(e.message,true);}});
async function initialize(){try{const token=location.hash.slice(1);history.replaceState(null,"",location.pathname);if(token){const r=await api("/api/session",{token});csrf=r.csrf;}session=await api("/api/state");csrf=session.csrf;snapshot=session.config;reveals={};render();}catch(e){message(e.message+"。请通过 Skill 的 config-ui start 获取新链接。",true);}}
window.addEventListener("hashchange",()=>{if(location.hash.slice(1))initialize();});
initialize();
