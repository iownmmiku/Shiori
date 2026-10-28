"use strict";

window.ShioriPages = (() => {
  let catalog = {providers:[],models:[],routing:{}}, selectedSource = "", modelQuery = "", sourceQuery = "";
  let discovered = [], discoverTimer, discoverSequence = 0, discoverController;
  let quickModelComplete = null, quickModelMode = false;
  let automaticManual = "";
  const sourcePresets = {deepseek:{name:"DeepSeek",url:"https://api.deepseek.com/v1",model:"deepseek-chat"},
    openai:{name:"OpenAI",url:"https://api.openai.com/v1"},local:{name:"Ollama",url:"http://127.0.0.1:11434/v1"},
    siliconflow:{name:"SiliconFlow",url:"https://api.siliconflow.cn/v1"},custom:{name:"我的模型服务",url:""}};
  function recommendChatModel(models, preferred = "") {
    const candidates=[...new Set(models.filter(value=>typeof value === "string" && value.trim()))]
      .filter(value=>!/(?:embed|rerank|\bbge\b|\be5-|whisper|\btts\b|audio|dall-e|stable-diffusion|sdxl|flux|moderation|sora|video)/i.test(value));
    if(candidates.includes(preferred))return preferred;
    const score=value=>{
      const name=value.toLowerCase();let rank=0;
      if(/(?:chat|instruct)/.test(name))rank+=20;
      if(/^(?:gpt-|chatgpt-)/.test(name))rank+=20;
      if(/(?:deepseek-chat|qwen|llama|gemma)/.test(name))rank+=10;
      if(/(?:mini|flash|small)/.test(name))rank+=5;
      if(/(?:reason|thinking|coder|code-|preview|experimental)/.test(name))rank-=15;
      if(/(?:\d{3,}b|\d{2,}x\d+b)/.test(name))rank-=5;
      return rank;
    };
    return candidates.map((model,index)=>({model,index,score:score(model)}))
      .sort((a,b)=>b.score-a.score || a.index-b.index)[0]?.model || "";
  }
  let logLines = [], logCursor = "", logLevel = "", logQuery = "", logPaused = false, logTimer, logGeneration = 0, logQueryTimer;
  const key = value => encodeURIComponent(value);
  const routeId = value => value === "default" ? "" : value;
  const sourceModels = source => catalog.models.filter(item => item.provider_id === source);
  const enabledModels = () => catalog.models.filter(item => item.model_type !== "embedding" && item.model && item.enabled !== false && item.effective_enabled !== false);
  const modelTitle = item => `${item.name || item.model}${item.name && item.name !== item.model ? " · " + item.model : ""}`;
  const sources = () => catalog.providers || [];
  const chosenSource = () => sources().find(item => item.id === selectedSource);
  async function loadCatalog() {
    const result = await api("/api/model-providers");
    catalog = {providers:result.providers || [],models:result.models || [],routing:result.routing || {},routes:result.routes || {}};
    if (!sources().some(item => item.id === selectedSource)) selectedSource = sources().find(item=>!item.legacy)?.id || sources()[0]?.id || "";
    return catalog;
  }
  async function renderModels() {
    if(window.ShioriEmbedding?.active())return window.ShioriEmbedding.render();
    await loadCatalog();
    const source = chosenSource(), models = source ? sourceModels(source.id).filter(item=>item.model_type !== "embedding") : [];
    const filtered = models.filter(item => `${item.name || ""} ${item.model}`.toLowerCase().includes(modelQuery.toLowerCase()));
    const cards = sources().filter(item=>`${item.name} ${item.base_url}`.toLowerCase().includes(sourceQuery.toLowerCase())).map(item=>`<button class="source-list-item ${item.id === selectedSource ? "active" : ""}" data-action="select-model-source" data-id="${e(item.id)}"><span class="source-initial">${e((item.name || "M").slice(0,1))}</span><span><strong>${e(item.name || "未命名供应商")}</strong><small>${e(item.base_url || "尚未设置地址")}</small><span class="source-count">${sourceModels(item.id).filter(model=>model.model_type!=="embedding" && model.model).length} 个对话模型${item.enabled === false ? " · 已停用" : ""}${item.legacy ? " · 原有配置" : ""}</span></span></button>`).join("");
    const modelCards = filtered.map(item=>`<article class="catalog-model"><div class="catalog-model-main"><strong>${e(item.name || item.model)}</strong><code>${e(item.model)}</code><div class="catalog-model-tags">${tag(item.effective_enabled === false || item.enabled === false ? "已停用" : "已启用",item.effective_enabled === false || item.enabled === false ? "gray" : "")}${item.vision ? tag("图片","gray") : ""}${tag(item.tools === false ? "不使用工具" : "允许工具","gray")}${Object.entries(catalog.routing).filter(([,value])=>value === item.id).map(([purpose])=>tag(routeNames[purpose] || purpose,"gray")).join("")}</div></div><div class="catalog-model-actions">${button("设为聊天模型","route-chat-model","ghost small","chat",`data-id="${e(item.id)}" ${item.effective_enabled === false || item.enabled === false ? "disabled" : ""}`)}${button("参数","edit-catalog-model","ghost small","settings",`data-id="${e(item.id)}"`)}${item.id !== "default" ? button(item.enabled === false ? "启用" : "停用","toggle-catalog-model","ghost small","",`data-id="${e(item.id)}"`) + button("删除","delete-catalog-model","ghost small","trash",`data-id="${e(item.id)}"`) : ""}</div></article>`).join("");
    const right = source ? `<header class="catalog-source-head"><div><h2>${e(source.name || "模型供应商")}</h2><p>${e(source.base_url || "尚未设置 API 地址")}</p><div class="catalog-model-tags">${tag(source.api_key_set ? "密钥已保存" : "未设置密钥","gray")}${source.legacy ? tag("原有单模型配置","gray") : ""}</div></div><div class="section-actions">${source.legacy ? button("打开原有配置","edit-base-model","","settings") : button("获取 / 添加模型","discover-source-models","primary","plus",`data-id="${e(source.id)}"`) + button("编辑供应商","edit-model-source","","edit",`data-id="${e(source.id)}"`) + button("移除","delete-model-source","ghost","trash",`data-id="${e(source.id)}"`)}</div></header>${source.legacy ? notice("原有模型与密钥继续使用。需要同一供应商管理多个模型时，新建供应商后获取模型即可。") : ""}<div class="catalog-model-toolbar"><label class="search-field">${icon("search")}<input id="catalog-model-query" type="search" value="${e(modelQuery)}" placeholder="搜索模型名称" aria-label="搜索模型名称"></label><span>${filtered.length} / ${models.length} 个模型</span></div><div class="catalog-model-list">${modelCards || empty("spark",modelQuery ? "没有匹配模型" : "还没有添加模型",modelQuery ? "换一个关键词试试。" : "填写供应商地址与密钥后，自动获取模型列表，再选择需要的模型。",source.legacy ? "edit-base-model" : "discover-source-models",source.legacy ? "配置默认模型" : "获取模型")}</div>` : empty("spark","先添加一个模型供应商","填写服务商的 API 地址和密钥，自动获取其模型列表。","new-model-source","添加供应商");
    return heading("MODELS","模型供应商","每个供应商保存一套连接，按需选择模型。聊天顶栏可为当前会话切换模型。",button("添加供应商","new-model-source","primary","plus")) + (window.ShioriEmbedding?.tabs() || "") + `<div class="model-catalog-layout"><aside class="model-source-panel"><label class="search-field">${icon("search")}<input id="catalog-source-query" type="search" aria-label="搜索供应商" placeholder="搜索供应商" value="${e(sourceQuery)}"></label><div class="model-source-list">${cards || '<p class="help-caption">还没有供应商。</p>'}</div></aside><section class="panel model-catalog-panel">${right}</section></div><details class="panel catalog-routing"><summary>为聊天、记忆、整理和人格蒸馏选择模型</summary><form data-form="catalog-routing"><div class="form-grid">${Object.entries(routeNames).filter(([purpose])=>purpose!=="embedding").map(([purpose,title])=>select(purpose,title,[["",purpose === "distillation" ? "继承日常对话模型" : "继承原有默认模型"],...enabledModels().filter(item=>item.id !== "default").map(item=>[item.id,`${sources().find(source=>source.id === item.provider_id)?.name || "模型"} · ${modelTitle(item)}`])],catalog.routing[purpose] || "")).join("")}</div><div class="section-actions"><button class="btn primary" type="submit">保存任务分工</button></div></form></details>`;
  }
  function sourceModal(source = null, addModels = false, quick = false) {
    clearTimeout(discoverTimer); discovered = []; discoverSequence++; discoverController?.abort();
    automaticManual = "";
    quickModelMode = quick; if(!quick)quickModelComplete=null;
    openModal(quick ? "连接聊天模型" : source ? (addModels ? "获取并添加模型" : "编辑模型供应商") : "添加模型供应商",quick ? "填写供应商 → 自动获取模型 → 选一个保存。已有模型与其他任务分工会保留。" : "填写地址与密钥后自动读取模型目录；读取目录不代表该模型已验证支持图片或工具。",`<input type="hidden" name="id" value="${e(source?.id || "")}">${!source ? `<div class="preset-row">${[["deepseek","DeepSeek"],["openai","OpenAI"],["local","Ollama"],["siliconflow","SiliconFlow"],["custom","自定义 / 中转"]].map(([id,title])=>button(title,"source-preset","ghost small","",`data-provider="${id}"`)).join("")}</div>` : ""}<div class="form-grid">${field("name","供应商名称",source?.name || (quick ? "我的模型服务" : ""),"text","起一个容易辨认的名字，例如 DeepSeek、我的中转服务。","required data-full maxlength=\"100\"")}${field("base_url","API 地址",source?.base_url || "","url","使用服务商提供的兼容 OpenAI 地址，通常以 /v1 结尾。","required data-full placeholder=\"https://api.example.com/v1\"")}${field("api_key","API 密钥","","password",source?.api_key_set ? "已保存，留空保留。修改后使用当前表单重新获取模型。" : "从供应商后台复制；本机模型服务未设密钥时可留空。","data-full autocomplete=\"off\"")}</div>${quick ? '<input type="hidden" name="enabled" value="true">' : toggle("enabled","启用供应商","停用后其模型不能继续参与路由。",source?.enabled !== false)}<div class="section-actions">${button("获取模型列表","fetch-source-models","","refresh")}</div><div id="source-discovery" class="source-discovery" aria-live="polite"><p class="help-caption">填好连接信息后会自动获取${quick ? "，然后点选一个聊天模型；保存时将发送一次短测试请求，计入模型用量" : "，也可以手动填写模型名称"}。读取目录不会发送聊天请求。</p></div>${quick ? field("manual_model","列表不可用？手动填写一个模型名称","","text","可留空。填写后优先使用这个名称；请按服务商提供的准确名称填写。","data-full maxlength=\"160\"") : '<div class="field"><label for="manual-model-names">手动添加模型（可选）</label><textarea id="manual-model-names" name="manual_models" rows="2" placeholder="每行一个模型名称"></textarea><small>目录接口不可用时，按服务商提供的准确模型名称填写。</small></div>'}`,"model-source",source,quick ? "测试并用于聊天 →" : "保存供应商并添加所选模型");
    if(source?.base_url) discoverTimer = setTimeout(()=>{const form=$('[data-form="model-source"]');if(form)discoverModels(form);},250);
  }
  async function startQuickModel(onComplete) {
    await loadCatalog(); quickModelComplete = onComplete;
    sourceModal(null,false,true);
  }
  async function discoverModels(form) {
    const data = getForm(form), result = $("#source-discovery",form); if(!result)return;
    if(!data.base_url?.trim())return;
    discoverController?.abort(); discoverController = new AbortController(); const sequence=++discoverSequence;
    result.innerHTML='<p class="help-caption"><span class="spinner small"></span> 正在读取当前表单的模型列表…</p>';
    try {
      const response=await api(data.id ? `/api/model-providers/${key(data.id)}/discover` : "/api/model-providers/discover",{method:"POST",body:{base_url:data.base_url,api_key:data.api_key || ""},signal:discoverController.signal});
      if(sequence !== discoverSequence || state.modalKind !== "model-source")return;
      discovered=[...new Set((response.models || []).map(item=>typeof item === "string" ? item : item.id).filter(value=>typeof value === "string" && value.trim()))];
      const previous=data.quick_chat_model;
      const recommended=quickModelMode ? recommendChatModel(discovered,discovered.includes(previous) ? previous : sourcePresets[form.dataset?.preset]?.model) : "";
      const manual=$('[name="manual_model"]',form);
      if(recommended && automaticManual && manual?.value===automaticManual)manual.value="";
      if(recommended)automaticManual="";
      result.innerHTML=`${recommended ? `<div class="local-connector-result"><strong>已推荐 ${e(recommended)}</strong><p>点击下方“测试并用于聊天”即可测试并设为默认；也可选择其他模型。不会自动产生聊天调用。</p></div>` : ""}<div class="discovery-heading"><strong>${quickModelMode ? "请选择一个聊天模型" : "发现 " + discovered.length + " 个模型"}</strong>${quickModelMode ? `<span>${discovered.length} 个可选</span>` : '<button class="btn ghost small" type="button" data-action="select-all-discovered">选择全部</button>'}</div><div class="discovered-models">${discovered.map(name=>`<label><input type="${quickModelMode ? "radio" : "checkbox"}" name="${quickModelMode ? "quick_chat_model" : "discovered_model"}" value="${e(name)}" ${(quickModelMode ? name===recommended : sourceModels(data.id).some(item=>item.model === name)) ? "checked" : ""}><span>${e(name)}</span></label>`).join("") || '<p class="help-caption">服务没有返回模型；可在下方手动填写。</p>'}</div>`;
    } catch(error) { if(error.name === "AbortError" || sequence !== discoverSequence)return;result.innerHTML=notice(`读取模型失败：${error.message}。可以修改连接信息后重试，或手动填写模型。`,"warning"); }
  }
  function chatModelPicker() {
    const current=scope(), options=enabledModels(), selected=current.model_id || "";
    return `<label class="chat-model-picker"><span>模型</span><select id="chat-model-id" aria-label="本会话模型" ${state.sending ? "disabled" : ""}><option value="">继承全局聊天模型</option>${options.map(item=>`<option value="${e(item.id)}" ${selected === item.id ? "selected" : ""}>${e(modelTitle(item))}</option>`).join("")}${selected && !options.some(item=>item.id === selected) ? `<option value="${e(selected)}" selected>原选择不可用，请重新选择</option>` : ""}</select></label>`;
  }
  async function chatModelControl() { try { await loadCatalog(); return chatModelPicker(); } catch { return '<span class="help-caption">模型目录暂不可用</span>'; } }
  function qqIngress(status) {
    if(!status || !('message_events_received' in status) && !status.diagnostic)return "";
    const last=status.last_message_result || {}, diagnostic=typeof status.diagnostic === "string" ? status.diagnostic : status.diagnostic?.message;
    const stages={accepted:"正在处理",sent:"平台已确认送达",blocked:"未进入回复",skipped:"本轮未回复",failed:"处理失败",command:"本地命令"};
    const guide=last.code === "group_not_allowed" || last.code === "private_not_allowed" ? "检查下面聊天是否被黑名单拦截，或尚未加入白名单，再发送新消息。聊天权限不会授予管理员身份。" : !status.message_events_received ? "用自己的账号私聊机器人，或在允许接收的群中 @ 它；若计数不增加，请开启连接器的消息上报。" : last.code === "sent" ? "收发链路已完成。群聊仍会按当前会话规则判断是否回复。" : "已收到消息。按上方原因检查接收规则、模型与默认回应规则，再发送一条新消息验证。";
    return `<section class="qq-ingress-summary"><strong>消息接收与回复</strong><p>${e(diagnostic || last.reason || "等待第一条 QQ 消息；连接在线不代表已收到消息上报。")}</p><small>收到消息 ${number(status.message_events_received)} 条 · 心跳 ${number(status.heartbeat_events_received)} 次${status.last_message_at ? " · 最近消息 " + e(fmt(status.last_message_at)) : ""}${last.stage ? " · " + e(stages[last.stage] || last.stage) : ""}</small>${last.user_id || last.scope_id ? `<small>${last.user_id ? "最近发送人：" + e(last.user_id) : ""}${last.scope_id ? " · 会话：" + e(last.scope_id) : ""}</small>` : ""}<p>${e(guide)}</p>${button("刷新接收状态","quick-diagnose","ghost small","refresh")}${button("查看运行日志","go-runtime-logs","ghost small","file")}</section>`;
  }
  function logMarkup() { return logLines.map(item=>`<div class="runtime-log-line level-${e(String(item.level || "INFO").toLowerCase())}" data-log-id="${e(item.id || "")}"><time>${e(item.time || "")}</time><span class="log-level">${e(item.level || "INFO")}</span><span class="log-message">${e(item.message || "")}</span></div>`).join("") || '<p class="runtime-log-empty">暂无匹配的运行日志。启动、错误与任务运行记录会显示在这里。</p>'; }
  async function fetchLogs(initial = false) {
    const generation=logGeneration, params={limit:200,level:logLevel,q:logQuery,...(!initial && logCursor ? {after:logCursor} : {})};
    const result=await api(`/api/logs?${query(params)}`); if(generation!==logGeneration)return;
    const items=result.lines || [], seen=new Set(logLines.map(item=>String(item.id)));
    logLines=initial ? items : [...logLines,...items.filter(item=>!seen.has(String(item.id)))];logLines=logLines.slice(-1000);logCursor=result.cursor ?? logCursor;
    const list=$("#runtime-log-feed");if(list){list.innerHTML=logMarkup();if(!logPaused)list.scrollTop=list.scrollHeight;}
    const status=$("#runtime-log-status");if(status)status.textContent=logPaused ? "已暂停更新" : `正在更新 · 显示 ${logLines.length} 条`;
  }
  async function renderLogs() {
    logGeneration++; logLines=[];logCursor="";await fetchLogs(true);
    return heading("RUNTIME","运行日志","查看本机启动、连接与任务错误。日志已做基础脱敏，分享前仍请检查内容。",button(logPaused ? "继续更新" : "暂停更新","toggle-log-tail","",logPaused ? "refresh" : "stop") + button("复制当前日志","copy-runtime-logs","","file") + `<a class="btn" href="/api/logs/export?${e(query({level:logLevel,q:logQuery}))}" download>下载日志</a>`) + `<section class="panel runtime-logs"><div class="runtime-log-toolbar"><label>级别<select id="runtime-log-level" aria-label="日志级别">${[["","全部"],["DEBUG","DEBUG"],["INFO","INFO"],["WARNING","WARNING"],["ERROR","ERROR"],["CRITICAL","CRITICAL"]].map(([value,title])=>`<option value="${value}" ${logLevel===value ? "selected" : ""}>${title}</option>`).join("")}</select></label><label class="search-field">${icon("search")}<input type="search" id="runtime-log-query" maxlength="256" aria-label="搜索运行日志" placeholder="搜索错误、连接或任务" value="${e(logQuery)}"></label><span id="runtime-log-status">${logPaused ? "已暂停更新" : `正在更新 · 显示 ${logLines.length} 条`}</span></div><div id="runtime-log-feed" class="runtime-log-feed" role="log" aria-live="off" tabindex="0">${logMarkup()}</div></section><p class="help-caption">仅在此页面每 2 秒读取新增日志，最多展示最近 1000 条。暂停只停止页面更新，不停止机器人。</p>`;
  }
  function stopLogs() { clearTimeout(logTimer); logTimer=null; }
  function scheduleLogs() { stopLogs();if(state.page !== "logs" || logPaused || !state.authenticated)return;logTimer=setTimeout(async()=>{try{if(!document.hidden)await fetchLogs();}catch(error){const status=$("#runtime-log-status");if(status)status.textContent=`读取失败：${error.message}`;}finally{scheduleLogs();}},2000); }
  async function handleAction(action,target) {
    const id=target.dataset.id;
    if(action === "go-model-sources"){await navigate("models");}
    else if(action === "go-runtime-logs"){await navigate("logs");}
    else if(action === "new-model-source"){sourceModal();}
    else if(action === "source-preset"){
      const form=target.closest("form"),preset=sourcePresets[target.dataset.provider];
      if(!preset)return true;clearTimeout(discoverTimer);discoverSequence++;discoverController?.abort();discovered=[];
      form.dataset ||= {};form.dataset.preset=target.dataset.provider;
      $('[name="name"]',form).value=preset.name;$('[name="base_url"]',form).value=preset.url;$('[name="api_key"]',form).value="";
      automaticManual=quickModelMode ? (preset.model || "") : "";
      const manual=$('[name="manual_model"]',form);if(manual)manual.value=automaticManual;
      const result=$("#source-discovery",form);if(result)result.innerHTML='<p class="help-caption">填写密钥后自动读取并推荐聊天模型。'+(automaticManual ? `已补齐预设型号 ${e(automaticManual)}；须通过实际测试才会设为默认。` : '推荐型号以当前服务实际返回的目录为准。')+'</p>';
      $('[name="api_key"]',form)?.focus();
    }
    else if(action === "select-model-source"){selectedSource=id;modelQuery="";await renderPage();}
    else if(action === "edit-model-source" || action === "discover-source-models"){const source=sources().find(item=>item.id === (id || selectedSource));if(!source || source.legacy)throw new Error("请先新建模型供应商。");sourceModal(source,action === "discover-source-models");}
    else if(action === "fetch-source-models"){await discoverModels(target.closest("form"));}
    else if(action === "select-all-discovered"){$$('input[name="discovered_model"]',target.closest("form")).forEach(item=>item.checked=true);}
    else if(action === "delete-model-source"){const source=sources().find(item=>item.id===id);if(!source || source.legacy)return true;if(sourceModels(id).length)throw new Error("请先移除该供应商下的对话和嵌入模型，再删除供应商。");await confirmAction("移除这个模型供应商？",`将移除「${source.name}」的连接配置与保存的密钥。`,async()=>{await api(`/api/model-providers/${key(id)}`,{method:"DELETE"});},"移除供应商");}
    else if(action === "toggle-catalog-model"){const item=catalog.models.find(item=>item.id===id);await api(`/api/model-catalog/models/${key(id)}`,{method:"PATCH",body:{enabled:item.enabled === false}});await bootstrap();}
    else if(action === "delete-catalog-model"){const item=catalog.models.find(item=>item.id===id);await confirmAction("删除这个模型？",`删除「${item?.model || id}」的本机配置；聊天与记忆保留。使用它的路由需要重新选择。`,async()=>{await api(`/api/model-catalog/models/${key(id)}`,{method:"DELETE"});},"删除模型");}
    else if(action === "route-chat-model"){await api("/api/model-catalog/routing",{method:"PUT",body:{chat:routeId(id)}});toast("已设为全局聊天模型；单独设置的会话保持其选择。");await bootstrap();}
    else if(action === "edit-catalog-model"){const item=catalog.models.find(item=>item.id===id);openModal("调整模型参数","图片与工具能力请按服务商实际支持填写，模型目录不会自动证明兼容性。",`${typeof modelTransportFields === "function" ? modelTransportFields(item) : ""}<input type="hidden" name="id" value="${e(id)}"><div class="form-grid">${field("name","显示名称",item.name || item.model,"text","","required data-full")}${field("timeout_seconds","模型等待超时（秒）",item.timeout_seconds ?? 90,"number","默认90秒，可设5—600秒；较慢的模型或图片任务可适当增大。","min=\"5\" max=\"600\" step=\"1\"")}${field("input_price","输入价格 / 百万 Token",item.input_price || 0,"number","人民币，未知保持0。","min=\"0\" step=\"0.01\"")}${field("output_price","输出价格 / 百万 Token",item.output_price || 0,"number","","min=\"0\" step=\"0.01\"")}</div>${typeof advancedModelFields === "function" ? advancedModelFields(item) : ""}${toggle("vision","支持图片输入","按服务商实际能力配置。",item.vision)}${toggle("tools","支持工具调用","电脑任务与使用工具需要模型支持。",item.tools !== false)}`,"catalog-model",item);}
    else if(action === "toggle-log-tail"){logPaused=!logPaused;target.textContent=logPaused ? "继续更新" : "暂停更新";const status=$("#runtime-log-status");if(status)status.textContent=logPaused ? "已暂停更新" : "正在更新";scheduleLogs();}
    else if(action === "copy-runtime-logs"){const text=logLines.map(item=>`${item.time || ""} ${item.level || "INFO"} ${item.message || ""}`).join("\n");try{await navigator.clipboard.writeText(text);toast("已复制当前显示的日志。");}catch{openModal("复制日志","选择下面内容，按 Ctrl+C 复制。",textarea("logs","当前日志",text,"","readonly rows=\"12\""));}}
    else return false;return true;
  }
  async function handleSubmit(kind,form) {
    const data=getForm(form);
    if(kind === "model-source"){
      const quick=quickModelMode, complete=quickModelComplete;
      const selected=$$('input[name="discovered_model"]:checked',form).map(item=>item.value), quickName=String(data.manual_model || data.quick_chat_model || "").trim();
      const names=quick ? (quickName ? [quickName] : []) : [...new Set([...selected,...String(data.manual_models || "").split(/[\n,，]+/).map(value=>value.trim()).filter(Boolean)])];
      if(quick && !names.length)throw new Error("请选择一个聊天模型，或在下方手动填写准确的模型名称。");
      if(names.length > 200)throw new Error("一次最多添加 200 个模型，请缩小本次选择范围。");
      const payload={name:data.name,base_url:data.base_url,api_key:data.api_key || "",enabled:quick ? true : data.enabled};
      const result=await api(data.id ? `/api/model-providers/${key(data.id)}` : "/api/model-providers",{method:data.id ? "PUT" : "POST",body:payload});
      const source=result.provider || result; const sourceId=data.id || source.id; if(!sourceId)throw new Error("供应商已保存，但没有返回有效编号，请刷新后添加模型。");
      selectedSource=sourceId;const idField=$('[name="id"]',form);if(idField)idField.value=sourceId;state.modalRecord=source;const secretField=$('[name="api_key"]',form);if(secretField)secretField.value="";
      let added=0,skipped=0,model;
      if(names.length){const result=await api("/api/model-catalog/models",{method:"POST",body:{provider_id:sourceId,model_type:"chat",models:names}});added=result.added?.length ?? names.length;skipped=result.skipped?.length || 0;if(quick)model=(result.models || result.added || []).find(item=>item.provider_id===sourceId && item.model===quickName);}
      if(quick){
        if(!model){await loadCatalog();model=catalog.models.find(item=>item.provider_id===sourceId && item.model===quickName);}
        if(!model?.id)throw new Error("供应商与模型已保存，但未找到聊天模型编号，请到模型供应商页选择它。");
        if(model.enabled===false)await api(`/api/model-catalog/models/${key(model.id)}`,{method:"PATCH",body:{enabled:true}});
        const tested=await api("/api/model/test",{method:"POST",body:{provider_id:routeId(model.id)}});
        if(tested.ok !== true)throw new Error("模型测试未通过，默认聊天模型保持原值。请修改连接信息后重试。");
        await api("/api/model-catalog/routing",{method:"PUT",body:{chat:routeId(model.id)}});
      }
      closeModal();quickModelComplete=null;quickModelMode=false;toast(quick ? `已选择 ${quickName}，可以开始聊天。` : names.length ? `供应商已保存，添加 ${added} 个模型${skipped ? `，${skipped} 个已存在` : ""}。` : "供应商已保存，可以随时获取并添加模型。");await bootstrap();
      if(quick && complete)await complete();
    } else if(kind === "catalog-model"){const payload={name:data.name,temperature:Number(data.temperature),input_price:Number(data.input_price),output_price:Number(data.output_price),vision:data.vision,tools:data.tools,custom_params:typeof parseCustomParams === "function" ? parseCustomParams(data.custom_params) : {}};if(data.top_p!==undefined)payload.top_p=Number(data.top_p);if(data.max_tokens!==undefined)payload.max_tokens=Number(data.max_tokens);if(data.reasoning_effort!==undefined)payload.reasoning_effort=data.reasoning_effort || "";if(data.timeout_seconds!==undefined)payload.timeout_seconds=Number(data.timeout_seconds);for(const key of ["first_token_timeout_seconds","total_timeout_seconds"])if(data[key]!==undefined)payload[key]=Number(data[key]);if(data.stream_mode!==undefined)payload.stream_mode=data.stream_mode;if(data.timeout_fallback!==undefined)payload.timeout_fallback=!!data.timeout_fallback;await api(`/api/model-catalog/models/${key(data.id)}`,{method:"PATCH",body:payload});closeModal();toast("模型参数已保存。");await bootstrap();}
    else if(kind === "catalog-routing"){await api("/api/model-catalog/routing",{method:"PUT",body:Object.fromEntries(Object.keys(routeNames).filter(purpose=>purpose!=="embedding").map(purpose=>[purpose,routeId(data[purpose] || "")]))});toast("任务分工已保存。");await bootstrap();}
    else return false;return true;
  }
  async function handleChange(target) {
    if(target.id === "chat-model-id"){if(state.sending)return true;await api(`/api/scopes/${key(state.scope)}`,{method:"PUT",body:{model_id:target.value}});toast(target.value ? "本会话已切换模型。" : "本会话继承全局聊天模型。");await bootstrap();return true;}
    if(target.id === "runtime-log-level"){logLevel=target.value;await renderPage();return true;}return false;
  }
  function handleInput(target) {
    if(target.id === "catalog-model-query" || target.id === "catalog-source-query"){const value=target.value,position=target.selectionStart;clearTimeout(discoverTimer);discoverTimer=setTimeout(async()=>{if(target.id === "catalog-model-query")modelQuery=value;else sourceQuery=value;await renderPage();const input=$("#"+target.id);input?.focus();input?.setSelectionRange(position,position);},250);}
    if(target.id === "runtime-log-query"){const value=target.value;clearTimeout(logQueryTimer);logQueryTimer=setTimeout(async()=>{logQuery=value;await renderPage();const input=$("#runtime-log-query");input?.focus();},350);}
    const form=target.closest?.('form[data-form="model-source"]');
    if(form && target.name === "manual_model")automaticManual="";
    if(form && ["base_url","api_key"].includes(target.name)){
      clearTimeout(discoverTimer);discoverSequence++;discoverController?.abort();discovered=[];
      if(target.name === "base_url" && form.dataset?.preset){
        const preset=sourcePresets[form.dataset.preset];
        if(target.value?.replace(/\/+$/,"")!==preset?.url){
          const manual=$('[name="manual_model"]',form);if(automaticManual && manual?.value===automaticManual)manual.value="";
          automaticManual="";form.dataset.preset="custom";
        }
      }
      const area=$("#source-discovery",form);if(area)area.innerHTML='<p class="help-caption">连接信息已改变，正在等待重新获取模型。也可手动填写准确型号。</p>';
      const data=getForm(form),saved=state.modalRecord?.api_key_set;const local=/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?=[:/]|$)/i.test(data.base_url || "");if(data.base_url && (data.api_key || saved || local))discoverTimer=setTimeout(()=>discoverModels(form),700);}
  }
  function afterRender() { if(state.page === "logs"){const list=$("#runtime-log-feed");if(list && !logPaused)list.scrollTop=list.scrollHeight;scheduleLogs();}else{stopLogs();logGeneration++;} }
  function close(){stopLogs();logGeneration++;discoverSequence++;discoverController?.abort();clearTimeout(discoverTimer);clearTimeout(logQueryTimer);}
  return {renderModels,renderLogs,chatModelControl,qqIngress,handleAction,handleSubmit,handleChange,handleInput,afterRender,close,loadCatalog,startQuickModel,recommendChatModel};
})();
