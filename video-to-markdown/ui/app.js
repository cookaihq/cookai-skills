"use strict";
const $ = id => document.getElementById(id);
const labels = { "main-agent": "主 Agent 看图", "host-agent": "宿主子 Agent", "independent-agent": "独立 Agent", api: "直接调用 API" };
let snapshot, session, csrf, store, target = "project", projectTarget = "project";
let busy = false, ended = false, uncertain = null, needsReload = false, previews = {}, reveals = {}, previewTimer, pollTimer, expiry = 0, generation = 0;
const field = id => store.field(id);
const row = (id, t = target) => store.row(id, t);
const layer = (t = target) => snapshot.layers.find(l => l.id === t);
const changes = () => store.changes(target);
const mode = () => store.mode(target);
const name = t => layer(t)?.name || (t === "default" ? "默认设置" : t);
const key = () => snapshot.revision + JSON.stringify(changes());
const effective = (view, id) => view.fields.find(f => f.id === id).effective;
const val = (id, fallback = "") => store.value(target, id, fallback);
const editable = () => !busy && !ended && !uncertain && !layer().readonly;
function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}
function message(text, error = false, focus = false) {
  $("feedback").hidden = false;
  $("feedback").textContent = text;
  $("feedback").dataset.error = String(error);
  if (focus) $("feedback").focus({ preventScroll: true });
}
function description(view) {
  const ask = effective(view, "ask").value, method = effective(view, "method").value, model = effective(view, "model").value;
  return ask ? "每次任务询问" + (method ? " · 预存建议：" + labels[method] + (model ? " / " + model : "") : " · 未预存建议")
    : (labels[method] || "固定方式尚未完整配置") + (model ? " · " + model : "");
}
function capabilityText(view = snapshot) {
  if (effective(view, "ask").value) return "任务确认后才执行；保存不会发起模型请求。";
  const method = effective(view, "method").value, caps = effective(view, "capabilities").value;
  const choice = caps.choices.find(c => c.method === method);
  const model = caps.models.find(m => m.id === effective(view, "model").value);
  if (method === "host-agent" && model && (model.available === false || model.vision === false)) return "此模型已报告不可用或不支持图片；只能保留设置供未来会话使用。";
  return choice?.available === false ? "此方式当前不可用；保存仅保留设置供未来会话使用。"
    : choice?.available == null ? "当前会话能力尚未核验；任务开始前必须核验。"
    : "入口已发现；模型、图片能力和鉴权仍按任务核验。";
}
function lifetime(value) {
  expiry = Date.now() + value.remaining * 1000;
  $("lifetime").textContent = "空闲 " + Math.ceil(value.remaining / 60) + " 分钟后到期";
}
function closePage(text) {
  if (ended) return;
  ended = true; clearTimeout(previewTimer); clearInterval(pollTimer); generation++;
  $("lifetime").textContent = "服务已结束";
  message(text + " 输入已保留。请回到 Agent 对话，要求重新打开 video-to-markdown 配置页面。", true);
  lockControls();
}
async function api(path, data) {
  let response, result;
  try {
    response = await fetch(path, { method: data ? "POST" : "GET", credentials: "same-origin",
      signal: AbortSignal.timeout(30000),
      headers: data ? { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" } : {},
      body: data ? JSON.stringify(data) : undefined });
    result = await response.json();
  } catch (_) {
    const error = new Error(path === "/api/save" ? "保存结果未知，请先重新读取并核对。" : "无法连接配置服务，请重新读取核对或让 Agent 重开页面。");
    error.unknown = path === "/api/save"; throw error;
  }
  if (!response.ok) {
    const error = new Error(result.message || "请求失败");
    error.code = result.error; error.status = response.status;
    error.unknown = path === "/api/save" && (response.status >= 500 || ["readback", "permission"].includes(error.code));
    if ([401, 410].includes(response.status)) closePage(result.message);
    throw error;
  }
  return result;
}
function lockControls() {
  $("stop").disabled = ended || busy;
  if (!store) return;
  $("settings").inert = busy;
  for (const input of document.querySelectorAll("#settings input, #settings select")) {
    if (!input.dataset.baseDisabled) input.dataset.baseDisabled = String(input.disabled);
    const blocked = ended || !!uncertain || input.dataset.baseDisabled === "true";
    if (input.tagName === "INPUT" && !["checkbox", "radio"].includes(input.type)) input.readOnly = blocked;
    else input.disabled = blocked;
  }
  for (const button of document.querySelectorAll("#settings button")) {
    if (button.id === "reload") button.disabled = ended || busy;
    else if (button.id === "save") button.disabled = !editable() || store.file(target).review || !Object.keys(changes()).length;
    else if (["reset", "acknowledge"].includes(button.id)) button.disabled = ended || busy || !!uncertain || layer().readonly || button.id === "acknowledge" && needsReload || button.id === "reset" && !Object.keys(changes()).length && !store.parked(target);
    else if (button.hasAttribute("data-scope") || button.hasAttribute("data-draft-target")) button.disabled = ended || busy || !!uncertain;
    else if (button.dataset.editAction) button.disabled = !editable();
  }
}
function changed() {
  delete previews[target]; $("feedback").hidden = true; updatePreview();
}
function control(id, { choices = null, placeholder = "", required = false } = {}) {
  const f = field(id), box = el("div", "field"), label = el("label", null, f.label + (required ? "（必填）" : ""));
  label.htmlFor = "field-" + id; box.append(label, el("span", "config-key", f.config_key));
  let input;
  if (choices) {
    input = el("select");
    for (const [value, text] of choices) { const option = el("option", null, text); option.value = value; input.append(option); }
  } else {
    input = el("input"); input.type = f.sensitive ? "password" : f.type === "number" ? "number" : f.type === "boolean" ? "checkbox" : "text";
    input.autocomplete = f.sensitive ? "new-password" : "off"; input.spellcheck = false; input.autocapitalize = "none";
    if (input.type === "number") { input.min = "1"; input.step = "1"; }
  }
  input.id = "field-" + id; input.name = id; input.placeholder = placeholder; input.required = required;
  input.disabled = layer().readonly || f.group === "vision" && mode() === "inherit";
  input.dataset.baseDisabled = String(input.disabled);
  if (f.sensitive) input.value = store.file(target).edits[id]?.op === "set" ? store.file(target).edits[id].value : "";
  else if (input.type === "checkbox") input.checked = !!val(id, false);
  else input.value = val(id);
  const hint = el("small"); hint.id = "hint-" + id; input.setAttribute("aria-describedby", hint.id);
  const secretStatus = () => {
    const op = store.file(target).edits[id];
    const text = op?.op === "unset" ? "待删除此文件 Key；保存后生效。" : op?.op === "set" ? "已输入替换值；清空输入可取消替换。" : row(id)?.configured ? "此文件已保存 Key；留空保留，清空替换值也会保留。" : "此文件未保存 Key，不能借用其他文件的连接。";
    input.placeholder = row(id)?.configured ? "留空保留已保存 Key" : "输入 Key";
    hint.textContent = text;
  };
  if (f.sensitive) {
    secretStatus();
    const actions = el("div", "key-actions");
    const reveal = el("button", "text-button", "显示 Key"), cancel = el("button", "text-button", "取消 Key 修改"), remove = el("button", "text-button danger", "删除此文件 Key");
    for (const b of [reveal, cancel, remove]) { b.type = "button"; b.dataset.editAction = "true"; }
    reveal.setAttribute("aria-pressed", "false");
    reveal.addEventListener("click", async () => {
      if (!editable()) return;
      if (input.type === "text") { input.type = "password"; reveal.textContent = "显示 Key"; reveal.setAttribute("aria-pressed", "false"); return; }
      const requestedTarget = target, requestedRevision = snapshot.revision, initialValue = input.value;
      try {
        if (!input.value && row(id)?.configured && store.file(target).edits[id]?.op !== "unset") {
          const r = await api("/api/reveal", { id, target, revision: requestedRevision });
          if (!editable() || target !== requestedTarget || snapshot.revision !== requestedRevision || !input.isConnected || input.value !== initialValue) return;
          reveals[target] ||= {}; reveals[target][id] = r.value || "";
          store.reveal(target, id, r.value || ""); input.value = r.value || "";
        }
        input.type = "text"; reveal.textContent = "隐藏 Key"; reveal.setAttribute("aria-pressed", "true");
      } catch (e) { message(e.message, true); }
    });
    cancel.addEventListener("click", () => {
      if (!editable()) return;
      store.undo(target, id); delete reveals[target]; input.value = ""; input.type = "password";
      reveal.textContent = "显示 Key"; reveal.setAttribute("aria-pressed", "false"); secretStatus(); changed();
    });
    const confirmation = el("div", "key-confirm");
    confirmation.hidden = true; confirmation.setAttribute("role", "group"); confirmation.setAttribute("aria-label", "确认删除此文件 Key");
    confirmation.append(el("p", null, "只删除此文件连接中的 Key，不会回退借用其他层 Key。固定远程方式须补齐连接后才能保存；询问模式可保留缺项建议。"));
    const confirm = el("button", "button danger", "确认加入删除修改"), keep = el("button", "button secondary", "保留 Key");
    for (const b of [confirm, keep]) { b.type = "button"; b.dataset.editAction = "true"; }
    confirmation.append(confirm, keep);
    const hide = () => { confirmation.hidden = true; remove.focus(); };
    keep.addEventListener("click", hide);
    confirm.addEventListener("click", () => {
      if (!editable()) return;
      store.removeKey(target); input.value = ""; input.type = "password"; delete reveals[target];
      reveal.textContent = "显示 Key"; reveal.setAttribute("aria-pressed", "false"); hide(); secretStatus(); changed();
    });
    remove.addEventListener("click", () => { if (editable()) { confirmation.hidden = !confirmation.hidden; if (!confirmation.hidden) keep.focus(); } });
    confirmation.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); hide(); } });
    document.addEventListener("pointerdown", e => { if (confirmation.isConnected && !confirmation.contains(e.target) && e.target !== remove) confirmation.hidden = true; }, { signal: secretListeners.signal });
    actions.append(reveal, cancel, remove); box.append(input, actions, confirmation, hint);
  } else {
    hint.textContent = f.group === "vision" && !row(id)?.present
      ? "此文件未填写；连接信息不会从其他来源补齐。"
      : (row(id)?.present ? "此文件已保存；" : "此文件未设置；") + "当前生效来源：" + name(f.effective.source);
    box.append(input, hint);
  }
  input.addEventListener("input", () => {
    if (!editable()) return;
    const value = input.type === "checkbox" ? input.checked : input.type === "number" ? input.value === "" ? null : Number(input.value) : input.value;
    store.edit(target, id, value);
    if (f.sensitive) {
      secretStatus();
      if (!input.value) {
        input.type = "password";
        const reveal = box.querySelector(".key-actions button");
        reveal.textContent = "显示 Key"; reveal.setAttribute("aria-pressed", "false");
      }
    }
    changed();
    if (id === "asr") renderAdvanced();
  });
  return box;
}
let secretListeners = new AbortController();
function renderRoutes() {
  secretListeners.abort(); secretListeners = new AbortController();
  const caps = field("capabilities").effective.value, selected = val("method");
  $("route-grid").replaceChildren();
  for (const [index, choice] of caps.choices.entries()) {
    const card = el("label", "route-card"), input = el("input");
    input.type = "radio"; input.name = "route"; input.id = "route-" + choice.method; input.value = choice.method;
    input.checked = choice.method === selected; input.disabled = layer().readonly || choice.available === false && !store.file(target).future;
    input.dataset.baseDisabled = String(input.disabled); card.htmlFor = input.id;
    card.append(input, el("span", "route-icon", ["◎", "⑂", "◇", "</>"][index]), el("strong", null, choice.label),
      el("span", "card-description", choice.reason),
      el("span", "model-hint", choice.available === true ? "入口已发现 · 模型仍需核验" : choice.available === false ? "当前不可用" : "能力尚未核验 · 可预先配置"));
    input.addEventListener("change", () => {
      if (!editable()) return;
      store.selectMethod(target, choice.method); changed(); renderRoutes(); lockControls(); $("route-" + choice.method).focus();
    });
    $("route-grid").append(card);
  }
  const choice = caps.choices.find(c => c.method === selected);
  $("route-capability").textContent = !selected ? (mode() === "fixed" ? "请选择一种识别方式。" : "未预存建议；每个新任务会等待选择。")
    : choice?.available === false ? "此方式当前不可用。设置可供未来会话使用；当前任务不能据此执行。"
    : choice?.available == null ? "能力尚未核验；允许保存设置，任务执行前必须取得实际能力证据。"
    : "模型与鉴权将在任务开始前核验，保存不会调用模型。";
  const container = $("route-fields"); container.replaceChildren();
  if (selected === "main-agent") container.append(el("p", "rule-note", "使用主会话当前模型；不改变写作或语音模型。任务开始前核验当前会话的图片能力。"));
  else if (selected) {
    const fixed = mode() === "fixed";
    container.append(control("model", { placeholder: selected === "host-agent" ? "当前宿主支持的模型 ID" : "服务模型 ID", required: fixed }));
    const models = caps.models || [];
    if (selected === "host-agent" && models.length) {
      const list = el("datalist"); list.id = "host-models";
      for (const model of models.filter(m => m.available !== false && m.vision !== false)) { const option = el("option"); option.value = model.id; option.label = model.name || model.id; list.append(option); }
      container.append(list); $("field-model").setAttribute("list", list.id);
      const evidence = el("div", "rule-note");
      for (const model of models) evidence.append(el("p", null, (model.name || model.id) + " · " + model.id + " · " + (model.available === false ? "当前不可用" : model.vision === false ? "不支持图片" : model.vision === true && model.available === true ? "图片能力与账号已报告可用" : "图片或账号能力未核验")));
      container.append(evidence);
    }
    if (selected !== "host-agent") {
      container.append(control("base_url", { placeholder: "https://service.example/v1", required: fixed }), control("api_key"));
      // A stored password stays an empty placeholder, so required is checked by the real loader.
      if (selected === "independent-agent") container.append(control("runtime", { choices: [["codex", "Codex CLI"]] }));
      container.append(el("p", "rule-note", selected === "api" ? "接口协议：Chat Completions 图片输入。" : "接口协议：Responses；使用独立 Codex CLI。实际模型与请求次数仍需任务核验。"));
    }
    container.append(control("accept_unverified_model"));
  }
  container.append(el("p", "rule-note", "能力依据：" + caps.source + "。没有可用列表时，仅在宿主确认可指定模型后手填；不会静默替换模型。"));
}
function renderVision() {
  const m = mode();
  $("suggestions").hidden = m !== "ask"; $("fixed-content").hidden = m !== "fixed";
  $("ask-note").hidden = m !== "ask"; $("inherited-summary").hidden = m !== "inherit";
  const editor = $("route-editor"); editor.hidden = m === "inherit";
  if (m !== "inherit") {
    (m === "ask" ? $("suggestion-content") : $("fixed-content")).append(editor);
    $("future-routes").checked = store.file(target).future; renderRoutes();
  }
}
function renderAdvanced() {
  const active = document.activeElement?.id, box = $("advanced-fields"); box.replaceChildren();
  box.append(control("asr", { choices: [["", "沿用其他来源（默认 AIhub Audio）"], ["aihub-audio", "AIhub Audio"], ["feishu-minutes", "飞书妙记"]] }), control("output"));
  if ((val("asr") || field("asr").effective.value) === "feishu-minutes") for (const id of ["feishu-profile", "feishu-app", "feishu-user"]) box.append(control(id));
  for (const id of ["source", "ffmpeg", "ffprobe"]) box.append(control(id));
  if (mode() !== "inherit") for (const id of ["call_limit", "max_images", "max_request_bytes", "max_context_chars"]) box.append(control(id));
  if (active) $(active)?.focus({ preventScroll: true });
  lockControls();
}
function renderSources() {
  const box = $("source-list"); box.replaceChildren();
  for (const f of snapshot.fields.filter(f => ["vision", "advanced"].includes(f.group))) {
    const details = el("details"), summary = el("summary", null, f.label + " · 生效来源：" + name(f.effective.source));
    details.append(summary, el("code", "config-key", f.config_key));
    for (const source of snapshot.layers) {
      const r = f.rows.find(r => r.layer === source.id);
      const value = !source.enabled ? "已关闭，未读取" : f.sensitive ? r.configured ? r.mask : "未配置" : r.present ? JSON.stringify(r.value) : "未配置";
      const entry = el("div", "source-entry"); entry.append(el("strong", null, source.name), el("code", null, source.path), el("span", null, value)); details.append(entry);
    }
    box.append(details);
  }
}
function editorState() {
  const current = changes(), count = Object.keys(current).length, parked = store.parked(target);
  for (const input of document.querySelectorAll('input[name="strategy"]')) {
    input.checked = input.value === mode(); input.disabled = layer().readonly;
    input.dataset.baseDisabled = String(input.disabled);
  }
  $("vision-state").textContent = mode() === "inherit"
    ? (current.inherit ? "待保存：恢复继承将移除此文件的整个图像识别组。" : "此文件沿用其他来源的图像识别设置。") + (parked ? " 独立识别草稿已保留，切回询问或固定方式可继续编辑；本次不提交它。" : "")
    : (count ? "当前文件有未保存修改。" : "正在查看此文件已保存的设置。") + " 连接仅取自此文件，不复制继承来源的 Key。";
  const view = previews[target]?.key === key() ? previews[target].config : !current.inherit ? snapshot : null;
  if (mode() === "inherit") $("inherited-summary").textContent = view
    ? "沿用结果：" + description(view) + " · 来源：" + name(effective(view, "ask").source)
    : "正在通过真实加载器计算恢复继承后的结果…";
  const others = store.pending().filter(t => t !== target), notice = $("other-drafts"); notice.replaceChildren(); notice.hidden = !others.length;
  if (others.length) {
    notice.append(el("span", null, "其他位置仍有未保存草稿："));
    for (const t of others) {
      const b = el("button", "text-button", name(t)); b.type = "button"; b.dataset.draftTarget = t;
      b.addEventListener("click", () => switchTarget(t)); notice.append(b);
    }
  }
  $("review-notice").hidden = !store.file(target).review;
  $("reset").disabled = !count && !parked;
}
function render() {
  $("project-label").textContent = session.project + " / " + field("capabilities").effective.value.host;
  $("effective-value").textContent = description(snapshot); $("effective-source").textContent = name(field("ask").effective.source);
  $("effective-capability").textContent = capabilityText(); $("global-note").textContent = snapshot.note;
  $("target-path").textContent = layer().path;
  $("scope-help").textContent = layer().readonly ? layer().message : target === "global" ? "影响显式启用该全局文件的项目；项目覆盖仍优先。" : target === "project" ? "只影响从当前目录启动的新任务。" : "此项目文件可能由其他 Skill 共用；保留无关字段。";
  document.querySelectorAll("[data-scope]").forEach(b => { const active = (target === "global" ? "global" : "project") === b.dataset.scope; b.classList.toggle("active", active); b.setAttribute("aria-pressed", String(active)); });
  $("project-file").value = projectTarget; $("project-file").disabled = target === "global"; $("project-file").dataset.baseDisabled = String(target === "global");
  renderVision(); renderAdvanced(); renderSources(); updatePreview();
}
function updatePreview() {
  if (!store) return;
  editorState(); clearTimeout(previewTimer); const requestGeneration = ++generation;
  const count = Object.keys(changes()).length, parked = store.parked(target);
  $("save-preview").textContent = count ? "待保存到 " + name(target) : parked ? "独立识别草稿已保留，本次未提交" : "当前文件没有未保存修改";
  if (uncertain) $("save-detail").textContent = "上次保存结果未知；先重新读取，核对回执与实际配置。";
  else if (store.file(target).review) $("save-detail").textContent = "请核对外部变化后，再允许应用此文件草稿。";
  else if (!count) $("save-detail").textContent = "仅保存当前文件；其他文件草稿会保留。不会调用模型。";
  else $("save-detail").textContent = "正在校验保存后的生效结果…";
  lockControls();
  if (!count || !editable() || store.file(target).review) return;
  const requestedTarget = target, revision = snapshot.revision, edits = changes(), requestKey = key();
  previewTimer = setTimeout(async () => {
    try {
      const r = await api("/api/preview", { target: requestedTarget, changes: edits, revision });
      if (ended || requestGeneration !== generation || target !== requestedTarget || key() !== requestKey) return;
      previews[target] = { key: requestKey, config: r.config };
      $("save-detail").textContent = "保存后：" + description(r.config) + " · 生效来源：" + name(effective(r.config, "ask").source) + "。" + capabilityText(r.config);
      editorState();
    } catch (e) {
      if (!ended && requestGeneration === generation && target === requestedTarget) $("save-detail").textContent = "尚未通过保存校验：" + e.message;
    }
  }, 300);
}
function switchTarget(next) {
  if (ended || busy || uncertain) return;
  target = next; if (next !== "global") projectTarget = next;
  reveals = {}; $("feedback").hidden = true; render();
}
document.querySelectorAll('input[name="strategy"]').forEach(input => input.addEventListener("change", () => {
  if (!editable()) return;
  store.setMode(target, input.value); changed(); renderVision(); renderAdvanced(); updatePreview(); input.focus();
}));
document.querySelectorAll("[data-scope]").forEach(b => b.addEventListener("click", () => switchTarget(b.dataset.scope === "global" ? "global" : projectTarget)));
$("project-file").addEventListener("change", () => switchTarget($("project-file").value));
$("future-routes").addEventListener("change", () => { if (editable()) { store.file(target).future = $("future-routes").checked; renderRoutes(); lockControls(); } });
$("source-jump").addEventListener("click", () => { $("sources").open = true; $("sources").scrollIntoView({ behavior: "smooth" }); $("sources").querySelector("summary").focus(); });
$("reset").addEventListener("click", () => { if (editable()) { store.discard(target); delete reveals[target]; delete previews[target]; render(); message("已放弃此文件未保存草稿；已保存配置未改动。"); } });
$("acknowledge").addEventListener("click", () => { if (editable()) { store.acknowledge(target); updatePreview(); message("已按新基线核对，保存前将重新校验。"); } });
$("reload").addEventListener("click", async () => {
  if (busy || ended) return;
  busy = true; lockControls();
  try {
    const latest = await api("/api/state"), receipt = latest.config.fields.find(f => f.id === "save-receipt")?.effective.value;
    const recovered = uncertain && receipt?.request_id === uncertain.id && receipt.target === uncertain.target && receipt.after_revision === latest.config.revision;
    store.rebase(latest.config, recovered ? uncertain.target : null, recovered ? receipt.field_results : null);
    if (uncertain && !recovered) store.file(uncertain.target).review = true;
    uncertain = null; needsReload = false; session = latest; csrf = latest.csrf; snapshot = latest.config; previews = {}; reveals = {}; lifetime(latest.lifetime);
    render(); message(recovered ? "已回读确认上次保存成功。配置已保存，请继续。" : "已重新读取；未保存草稿已保留。有变化的草稿需核对后再保存。");
  } catch (e) { message(e.message, true); }
  finally { busy = false; updatePreview(); }
});
$("settings").addEventListener("submit", async e => {
  e.preventDefault();
  if (!editable() || store.file(target).review || !Object.keys(changes()).length) return;
  const caps = field("capabilities").effective.value, method = val("method"), model = caps.models.find(m => m.id === val("model"));
  const unavailable = caps.choices.find(c => c.method === method)?.available === false || method === "host-agent" && model && (model.available === false || model.vision === false);
  if (Object.keys(changes()).some(id=>field(id)?.group==='vision') && mode() !== "inherit" && unavailable && !store.file(target).future && (method !== row("method")?.value || val("model") !== row("model")?.value)) {
    return message("所选方式或模型当前不可用。请选择其他方式，或勾选仅保存供未来会话使用。", true, true);
  }
  const saveTarget = target, edits = changes(), revision = snapshot.revision, id = crypto.randomUUID();
  busy = true; updatePreview();
  try {
    const r = await api("/api/save", { target: saveTarget, changes: { ...edits, request_id: { op: "set", value: id } }, revision });
    const results=r.config.task?.field_results||{}, failures=Object.entries(results).filter(([,result])=>result.status!=='saved');
    store.rebase(r.config, saveTarget, results); snapshot = r.config; reveals = {}; previews = {}; render();
    if(failures.some(([,result])=>result.status==='uncertain'))uncertain={id,target:saveTarget};
    message(Object.entries(results).map(([id,result])=>(field(id)?.label||id)+'：'+(result.status==='saved'?'已保存并回读':result.message)).join('；') + (failures.length?'；失败草稿已保留。':'。实际生效来源：'+name(field('ask').effective.source)+'。'), !!failures.length, true);
  } catch (error) {
    if (error.unknown) uncertain = { id, target: saveTarget };
    if (error.code === "conflict") { store.file(saveTarget).review = true; needsReload = true; }
    message(error.message, true, true);
  } finally { busy = false; updatePreview(); }
});
let lastActivity = 0;
for (const event of ["pointerdown", "keydown", "input", "scroll"]) document.addEventListener(event, e => {
  if (!e.isTrusted || ended || !csrf || Date.now() - lastActivity < 10000) return;
  lastActivity = Date.now();
  api("/api/activity", {}).then(r => { if (!ended) lifetime(r); }).catch(() => {});
}, { passive: true, capture: true });
$("stop").addEventListener("click", async () => {
  if (busy || ended) return;
  try { await api("/api/stop", {}); closePage("配置服务已关闭，已保存配置保留。"); }
  catch (e) { closePage(e.message + " 服务状态未确定。"); }
});
async function initialize() {
  try {
    const token = location.hash.slice(1); history.replaceState(null, "", location.pathname);
    if (token) { const r = await api("/api/session", { token }); csrf = r.csrf; }
    session = await api("/api/state"); csrf = session.csrf; snapshot = session.config; store = new VideoConfigDrafts(snapshot);
    // New personal defaults use the established global target only when it is enabled.
    if (!row("ask")?.present && !layer("global").readonly && field("ask").effective.source === "default") target = "global";
    lifetime(session.lifetime); render();
    pollTimer = setInterval(async () => {
      if (ended || busy) return;
      if (Date.now() >= expiry) return closePage("页面已到期。");
      try {
        const health = await api("/health");
        if (health.instance !== session.instance) return closePage("配置服务实例已变化。");
        lifetime(health.lifetime);
      } catch (e) { closePage(e.message); }
    }, 15000);
  } catch (e) { closePage(e.message); }
}
initialize();
new ResizeObserver(entries => {
  document.documentElement.style.setProperty("--bar-height", Math.ceil(entries[0].target.getBoundingClientRect().height) + "px");
}).observe($("save-bar"));
