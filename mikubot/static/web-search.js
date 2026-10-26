"use strict";

window.MikubotWebSearch = (() => {
  const presets = {
    public:{name:"公共搜索（无需密钥）",endpoint:""},
    tavily:{name:"Tavily",endpoint:"https://api.tavily.com/search",docs:"https://docs.tavily.com/documentation/api-reference/endpoint/search"},
    deepseek:{name:"DeepSeek 搜索",endpoint:"https://api.deepseek.com/v1/tools/search",docs:"https://api-docs.deepseek.com/guides/search/"},
    brave:{name:"Brave Search",endpoint:"https://api.search.brave.com/res/v1/web/search",docs:"https://api-dashboard.search.brave.com/app/documentation/web-search/get-started"}
  };
  const channels = {public:"公共搜索",tavily:"Tavily",deepseek:"DeepSeek 搜索",brave:"Brave Search",
    bing_html:"公共搜索 · Bing 网页",bing_rss:"公共搜索 · Bing RSS",duckduckgo_html:"公共搜索 · DuckDuckGo 网页"};
  const defaults = {enabled:true,provider:"public",timeout_seconds:12,max_results:5,daily_calls:100,fallback_public:false};
  const diagnostics = {
    disabled:"网页搜索已停用，请先启用并保存。",
    not_configured:"当前搜索渠道尚未保存独立 API 密钥，请先配置并保存。",
    daily_limit:"今日网页搜索额度已用完；额度为 0 时停止调用，请调整并保存或次日再试。",
    configuration_changed:"搜索设置在请求期间发生变化，请确认已保存配置后重试。",
    invalid_query:"搜索内容需为 1—300 个字符；DeepSeek 最多 128 个字符。",
    invalid_endpoint:"搜索接口地址无效，请检查已保存的完整公开 HTTPS 地址。",
    invalid_provider:"当前搜索渠道配置无效，请重新选择并保存。",
    queue_full:"网页搜索排队已满，请稍后再试。",
    timeout:"网页搜索超过等待上限，请稍后再试或调整超时。",
    http_error:"搜索供应商未接受请求，请检查已保存的密钥、服务配置和供应商额度。",
    provider_unavailable:"搜索服务暂不可用，请检查服务配置、网络或供应商额度。",
    invalid_response:"搜索服务未返回兼容的公开网页结果，请检查接口配置。",
    response_too_large:"搜索服务返回结果超过接收上限，请减少结果条数后重试。"
  };
  const drafts = new WeakMap();
  const known = id => Object.prototype.hasOwnProperty.call(presets,id);
  const saved = () => ({...defaults,...(state.settings?.search || {})});
  const selected = () => known(saved().provider) ? saved().provider : "public";
  const provider = id => (state.settings?.search_providers || []).find(item => item.id === id) || {};
  const number = value => typeof value === "number" && Number.isFinite(value) && Number.isInteger(value) && value >= 0;
  const text = (value,limit=1600) => typeof value === "string" ? value.slice(0,limit) : "";
  const diagnostic = (error,fallback) => Object.prototype.hasOwnProperty.call(diagnostics,error?.code) ? diagnostics[error.code] : fallback;

  function safeURL(value) {
    if(typeof value !== "string" || value.length > 4096 || /[\u0000-\u001f\u007f]/.test(value))return "";
    try {
      const url = new URL(value.trim());
      return ["https:","http:"].includes(url.protocol) && url.hostname && !url.username && !url.password ? url.href : "";
    } catch { return ""; }
  }

  function providerFields(id,draft={}) {
    if(id === "public")return '<p class="help-caption">使用现有公共搜索，无需填写 API 密钥。公共搜索可用性受搜索站点限制。</p>';
    const item = provider(id), preset = presets[id];
    return field("api_key",`${preset.name} API 密钥`,"","password",item.api_key_set ? "已保存此供应商密钥。留空继续使用，页面不会回显。" : "填写此供应商的 API 密钥；各供应商分别保存。",'autocomplete="new-password" spellcheck="false"')
      + toggle("clear_api_key","删除此供应商已保存的密钥","勾选并保存后删除；留空输入框不会删除密钥。",false)
      + `<details class="details"><summary>高级 · 搜索接口地址</summary>${field("base_url","完整 HTTPS 接口地址",draft.base_url ?? (item.base_url || preset.endpoint),"url","填写搜索接口的完整地址，不是聊天模型接口。",'required maxlength="2048"')}<a href="${e(preset.docs)}" target="_blank" rel="noopener noreferrer">${e(preset.name)} 官方接口文档</a></details>`
      + (id === "deepseek" ? '<p class="help-caption">DeepSeek 使用独立 Search API，查询最多 128 个字符；普通聊天接口不会代替网页搜索。</p>' : "");
  }

  function statusHTML(status) {
    if(!status)return '<p class="help-caption">用量状态暂不可用，请稍后刷新。</p>';
    const config = saved(), limit = number(status.daily_calls) ? status.daily_calls : config.daily_calls;
    const used = number(status.used_today) ? status.used_today : number(status.used) ? status.used : null;
    const remaining = number(status.remaining) ? status.remaining : number(limit) && used !== null ? Math.max(0,limit-used) : null;
    const id = known(status.provider) ? status.provider : selected();
    return `<p>已保存渠道：<strong>${e(presets[id].name)}</strong> · ${status.enabled === false ? "已关闭" : "已开启"}</p><p>今日已用 <strong>${e(used ?? "未知")}</strong> 次 / 额度 <strong>${e(limit ?? "未知")}</strong> 次 · 剩余 ${e(remaining ?? "未知")} 次</p>${limit === 0 ? '<p class="help-caption">今日额度设为 0，搜索调用已停止；0 不表示无限。</p>' : ""}`;
  }

  async function loadStatus(root) {
    let value = null;
    try { value = await api("/api/search/status"); } catch { /* Only our own safe status message is displayed. */ }
    const area = root?.querySelector?.('[data-web-search-status]');
    if(area)area.innerHTML = statusHTML(value);
    return value;
  }

  async function render() {
    const config = saved(), id = selected(), status = await loadStatus();
    return heading("WEB SEARCH","网页搜索","为聊天配置真实网页检索，查看来源链接和每日搜索次数。")
      + `<section class="panel settings-section" data-web-search><h2>搜索设置</h2><form data-form="web-search" data-provider="${e(id)}">`
      + toggle("enabled","启用网页搜索","关闭聊天、蒸馏与兴趣探索中的通用网页搜索。",config.enabled !== false)
      + select("provider","搜索渠道",Object.entries(presets).map(([key,item]) => [key,item.name]),id)
      + `<div data-web-search-provider>${providerFields(id)}</div><details class="details"><summary>高级 · 超时、条数与调用额度</summary><div class="form-grid">`
      + field("timeout_seconds","搜索等待上限（秒）",config.timeout_seconds,"number","1—60 秒。",'required min="1" max="60" step="1"')
      + field("max_results","每次最多结果数",config.max_results,"number","1—10 条，实际结果可能更少。",'required min="1" max="10" step="1"')
      + field("daily_calls","每日搜索调用额度",config.daily_calls,"number","0—100000；0 停止搜索调用，不表示无限。",'required min="0" max="100000" step="1"')
      + `</div>${toggle("fallback_public","供应商失败时尝试公共搜索","开启后，供应商失败时会把本次查询发送给公共搜索站点（如 Bing、DuckDuckGo）；实际渠道会显示在测试结果中。",config.fallback_public === true)}</details><div class="section-actions"><button class="btn primary" type="submit">保存网页搜索设置</button></div></form>`
      + `<div data-web-search-status aria-live="polite">${statusHTML(status)}</div>${button("刷新用量","web-search-status","ghost small","refresh")}`
      + `<h2>测试已保存配置</h2><p class="help-caption">修改后请先保存。测试使用已保存的渠道和密钥，会实际搜索，可能产生费用，并计入每日调用额度。</p><form data-form="web-search-test">`
      + field("query","搜索内容","","search",id === "deepseek" ? "DeepSeek 查询最多 128 个字符。" : "输入需要查找的关键词，最多 300 个字符，查看真实来源。",`required maxlength="${id === "deepseek" ? 128 : 300}"`)
      + '<button class="btn" type="submit">测试网页搜索</button></form><div data-web-search-results aria-live="polite"></div></section>';
  }

  function integer(value,min,max,label) {
    if(String(value ?? "").trim() === "")throw new Error(`请填写${label}。`);
    const parsed = Number(value);
    if(!Number.isInteger(parsed) || parsed < min || parsed > max)throw new Error(`${label}须为 ${min}—${max} 的整数。`);
    return parsed;
  }

  function endpoint(value) {
    try {
      const url = new URL(String(value || "").trim());
      if(url.protocol === "https:" && url.hostname && !url.username && !url.password && !url.search && !url.hash)return url.href;
    } catch { /* Fall through to the authored validation message. */ }
    throw new Error("请填写完整 HTTPS 搜索接口地址，不包含账号、密钥、查询参数或片段。");
  }

  function resultsHTML(result) {
    const channel = Object.prototype.hasOwnProperty.call(channels,result?.channel) ? channels[result.channel] : "未提供";
    const rows = (Array.isArray(result?.results) ? result.results : []).slice(0,10);
    const cards = rows.map(item => {
      const url = safeURL(item?.url);
      if(!url)return "";
      return `<article class="source-item"><h3><a href="${e(url)}" target="_blank" rel="noopener noreferrer">${e(text(item.title,300) || url)}</a></h3><p class="record-text">${e(text(item.content))}</p><small>${e(url)}</small></article>`;
    }).filter(Boolean);
    return `<p>实际搜索渠道：<strong>${e(channel)}</strong></p>`
      + (cards.length ? cards.join("") : '<p class="help-caption">本次没有返回可显示的网页来源链接。</p>');
  }

  async function handleSubmit(kind,form) {
    if(!["web-search","web-search-test"].includes(kind))return false;
    const data = getForm(form);
    if(kind === "web-search") {
      if(!known(data.provider))throw new Error("请选择有效的搜索渠道。");
      const payload = {search:{enabled:data.enabled === true,provider:data.provider,
        timeout_seconds:integer(data.timeout_seconds,1,60,"搜索等待上限"),
        max_results:integer(data.max_results,1,10,"结果条数"),daily_calls:integer(data.daily_calls,0,100000,"每日搜索额度"),
        fallback_public:data.fallback_public === true}};
      if(data.provider !== "public") {
        const secret = String(data.api_key || "").trim(), clear = data.clear_api_key === true;
        if(secret && clear)throw new Error("填写新密钥和删除已保存密钥不能同时选择。");
        const item = {id:data.provider,base_url:endpoint(data.base_url)};
        if(secret)item.api_key = secret;
        if(clear)item.clear_api_key = true;
        payload.search_providers = [item];
      }
      try { await api("/api/settings",{method:"PUT",body:payload}); }
      catch(error) { throw new Error(diagnostic(error,"网页搜索设置未保存，请检查配置和当前登录状态后重试。")); }
      const secret = form.querySelector('[name="api_key"]');
      if(secret)secret.value = "";
      drafts.delete(form);
      toast("网页搜索设置已保存");
      try { await bootstrap(); }
      catch { throw new Error("网页搜索设置已保存，页面刷新未完成，请刷新页面后查看。"); }
    } else {
      const query = String(data.query || "").trim();
      if(!query)throw new Error("请输入搜索内容。");
      if([...query].length > 300)throw new Error("搜索内容最多 300 个字符，请缩短关键词。");
      if(selected() === "deepseek" && [...query].length > 128)throw new Error("DeepSeek 查询最多 128 个字符，请缩短关键词。");
      const root = form.closest('[data-web-search]'), area = root?.querySelector('[data-web-search-results]');
      const submit = form.querySelector('[type="submit"]');
      if(submit)submit.disabled = true;
      if(area)area.textContent = "正在使用已保存配置搜索…";
      try {
        const result = await api("/api/search/test",{method:"POST",body:{query}});
        if(area)area.innerHTML = resultsHTML(result);
      } catch(error) {
        const message = diagnostic(error,"搜索测试未完成，请检查已保存配置、每日额度和供应商状态后重试。");
        if(area)area.textContent = message;
        throw new Error(message);
      } finally {
        if(submit)submit.disabled = false;
        await loadStatus(root);
      }
    }
    return true;
  }

  async function handleAction(action,target) {
    if(action !== "web-search-status")return false;
    target.disabled = true;
    try { await loadStatus(target.closest('[data-web-search]')); }
    finally { target.disabled = false; }
    return true;
  }

  function handleChange(target) {
    const form = target.closest?.('form[data-form="web-search"]');
    if(!form || target.name !== "provider")return false;
    const previous = known(form.dataset.provider) ? form.dataset.provider : selected(), next = target.value;
    if(!known(next))throw new Error("请选择有效的搜索渠道。");
    const panel = form.querySelector('[data-web-search-provider]');
    if(!panel)return false;
    const savedDrafts = drafts.get(form) || new Map();
    if(previous !== "public")savedDrafts.set(previous,{
      base_url:form.querySelector('[name="base_url"]')?.value,
      api_key:form.querySelector('[name="api_key"]')?.value || "",
      clear_api_key:form.querySelector('[name="clear_api_key"]')?.checked === true
    });
    drafts.set(form,savedDrafts);
    const draft = savedDrafts.get(next) || {};
    panel.innerHTML = providerFields(next,draft);
    // Restore only user-entered drafts to password properties, never HTML
    // attributes or saved server secrets. Other form controls are untouched.
    const secret = form.querySelector('[name="api_key"]'), clear = form.querySelector('[name="clear_api_key"]');
    if(secret)secret.value = draft.api_key || "";
    if(clear)clear.checked = draft.clear_api_key === true;
    form.dataset.provider = next;
    return true;
  }

  return {render,handleSubmit,handleAction,handleChange};
})();
