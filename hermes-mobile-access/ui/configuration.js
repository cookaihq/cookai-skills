// Business Server URL and profile controls; composed with fixed vendored UI.
function mobileUrlDraft() {
  const urlField = state?.config.fields.find(item => item.id === 'subdomain');
  const suffix = state?.config.fields.find(item => item.id === 'suffix')?.value;
  const name = String(urlField ? effective(urlField) || '' : '').trim().toLowerCase();
  if (!suffix || suffix === '未配置' || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(name) || `${name}.${suffix}`.length > 253) return null;
  return `https://${name}.${suffix}/`;
}

function decorateProfileChoices(profile) {
  let group = document.querySelector('#field-profile');
  if (!profile || !group) return;
  if (group.tagName === 'SELECT') {
    const choices = document.createElement('div');
    choices.id = group.id; choices.className = 'profile-choices';
    choices.setAttribute('role', 'radiogroup');
    choices.setAttribute('aria-label', profile.label);
    choices.setAttribute('aria-required', String(Boolean(profile.required)));
    choices.setAttribute('aria-describedby', 'error-profile');
    choices.innerHTML = (profile.choices || []).map((option, index) =>
      `<label class="profile-choice"><input type="radio" id="profile-option-${index}" name="mobile-profile" data-field="profile" value="${e(option)}" ${profile.readonly ? 'disabled' : ''} ${profile.required ? 'required' : ''}><span>${e(option)}</span></label>`
    ).join('');
    const label = group.closest('.field').querySelector('label[for="field-profile"]');
    if (label) label.htmlFor = 'profile-option-0';
    group.replaceWith(choices);
    group = choices;
  }
  for (const option of group.querySelectorAll('input[type="radio"]')) {
    option.checked = option.value === effective(profile);
  }
}

function decorateDomainPreview() {
  if (!state || ended || state.page !== 'configuration') return;
  const field = id => state.config.fields.find(item => item.id === id);
  decorateProfileChoices(field('profile'));
  const keyInput = document.querySelector('#field-access_key');
  if (keyInput && !keyInput.closest('.field').querySelector('[data-generate-access-key]')) {
    const actions = document.createElement('div'); actions.className = 'actions';
    const generate = document.createElement('button'); generate.type = 'button';
    generate.dataset.generateAccessKey = 'true'; generate.textContent = '生成新密钥';
    const copy = document.createElement('button'); copy.type = 'button';
    copy.dataset.copyAccessKey = 'true'; copy.textContent = '复制密钥';
    actions.append(generate, copy); keyInput.closest('.secret-row').after(actions);
  }
  const suffix = field('suffix')?.value;
  const candidate = mobileUrlDraft();
  const input = document.querySelector('#field-subdomain');
  if (input) {
    input.type = 'text'; input.maxLength = 63; input.spellcheck = false; input.autocomplete = 'off'; input.placeholder = field('subdomain').default;
    input.setAttribute('aria-label', '服务子域名');
    input.style.width = `${Math.max(6, input.value.length + 1)}ch`;
    if (!input.closest('.url-composite')) {
      const row = document.createElement('div'); row.className = 'url-entry'; input.before(row);
      const composite = document.createElement('div'); composite.className = 'url-composite'; row.append(composite);
      const scheme = document.createElement('span'); scheme.className = 'url-fixed'; scheme.textContent = 'https://'; scheme.title = '固定协议';
      const ending = document.createElement('span'); ending.className = 'url-fixed url-parent'; ending.title = '来自 FRP Launch 配置';
      composite.append(scheme, input, ending);
      const button = document.createElement('button'); button.type = 'button'; button.dataset.copyUrl = 'draft';
      button.textContent = '复制地址'; button.setAttribute('aria-label', '复制完整 Server URL'); row.append(button);
    }
    input.closest('.url-composite').querySelector('.url-parent').textContent = suffix && suffix !== '未配置' ? `.${suffix}/` : '.[请先配置 FRP 域名]/';
    input.closest('.url-entry').querySelector('button').disabled = !candidate;
  }
  for (const [id, kind, title] of [['active_url', 'active', '复制当前地址']]) {
    const target = document.querySelector('#field-' + id);
    if (!target) continue;
    if (kind === 'active') { target.disabled = false; target.readOnly = true; }
    if (!target.closest('.url-entry')) {
      const row = document.createElement('div'); row.className = 'url-entry'; target.before(row); row.append(target);
      const button = document.createElement('button'); button.type = 'button'; button.dataset.copyUrl = kind;
      button.textContent = title; button.setAttribute('aria-label', kind === 'active' ? '复制当前生效的 Server URL' : '复制输入的 Server URL');
      row.append(button);
    }
    target.closest('.url-entry').querySelector('button').disabled = kind === 'draft' ? !candidate : !String(field('active_url')?.value || '').startsWith('https://');
  }
  if (pending.subdomain && !candidate) localErrors.subdomain = `只填写一个服务子域名，例如 ${field('subdomain').default}，不包含点、协议、端口或路径。`;
  else delete localErrors.subdomain;
  const urlError = document.querySelector('#error-subdomain');
  if (urlError) urlError.textContent = localErrors.subdomain || '';
  const save = document.querySelector('#save');
  if (save) { save.textContent = '保存并应用'; save.disabled = !Object.keys(pending).length || Object.keys(localErrors).length > 0; }
  const heading = document.querySelector('.heading h1');
  if (heading && value('mode', 'all') === 'all') heading.textContent = '手机接入配置';
  const text = document.querySelector('.heading p');
  if (text) text.textContent = '填写服务子域名，复制完整 Server URL 到 Hermes Mobile。';
  const bindingPanel = [...document.querySelectorAll('.group')].find(node => node.querySelector('h2')?.textContent === '运行绑定');
  if (bindingPanel) bindingPanel.classList.add('binding-panel');
  const result = document.querySelector('#field-last_result');
  if (result && result.tagName === 'INPUT') {
    const display = document.createElement('p');
    display.id = result.id;
    display.className = 'domain-result';
    display.textContent = field('last_result')?.value || '';
    result.replaceWith(display);
  }
  const summaryNode = document.querySelector('#summary');
  if (summaryNode && pending.subdomain) {
    let note = document.querySelector('#domain-change-note');
    if (!note) { note = document.createElement('p'); note.id = 'domain-change-note'; summaryNode.append(note); }
    note.textContent = candidate
      ? `当前：${field('active_url')?.value} → 配置地址：${candidate}。运行中的连接会先查占用，再停旧启新；失败尝试恢复旧连接。`
      : 'FRP 域名尚不可用，完整地址待核验；当前连接保持不变。';
  }
}
const originalRender = render;
render = function () { originalRender(); decorateDomainPreview(); };
const originalSummary = summary;
summary = function () { originalSummary(); decorateDomainPreview(); };
root.addEventListener('input', decorateDomainPreview);
root.addEventListener('click', async event => {
  if (event.target.closest('[data-generate-access-key]')) {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const key = btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
    pending.access_key = {op: 'set', value: key};
    delete revealed.access_key; delete localErrors.access_key;
    render(); toast('新密钥已生成，点击保存后生效。'); return;
  }
  if (event.target.closest('[data-copy-access-key]')) {
    try {
      if (pending.access_key?.op === 'unset') { toast('密钥待清除，请撤销修改后再复制。'); return; }
      const key = pending.access_key?.op === 'set' ? pending.access_key.value :
        (await api('/api/reveal', {id: 'access_key', revision: state.config.revision})).value;
      if (!key) { toast('请先生成或填写密钥。'); return; }
      await navigator.clipboard.writeText(key); toast('已复制密钥，请在手机本地填写。');
    } catch { toast('复制未完成，可点击眼睛按钮后手动复制。'); }
    return;
  }
  const button = event.target.closest('[data-copy-url]');
  if (!button || button.disabled) return;
  const text = button.dataset.copyUrl === 'draft' ? mobileUrlDraft() : state.config.fields.find(item => item.id === 'active_url')?.value;
  if (!text) return;
  try { await navigator.clipboard.writeText(text); toast('已复制完整 Server URL'); }
  catch { toast('复制未完成，可选中完整地址后手动复制。'); }
});
decorateDomainPreview();
