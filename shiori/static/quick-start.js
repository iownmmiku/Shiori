"use strict";

window.ShioriQuickStart = (() => {
  const steps = ["认识机器人", "连接模型", "接入 QQ"];
  let current = 0;
  let drafts = {};
  let targetModel = "default";
  const cleanIds = value => [...new Set(String(value || "").split(/[\s,，;；]+/).filter(Boolean))];
  const cleanNames = value => [...new Set(String(value || "").split(/[\n\r,，;；]+/).map(item => item.trim()).filter(Boolean))];

  function modelSelection(settings) {
    const id = settings.routing?.chat;
    const selected = id && (settings.providers || []).find(item => item.id === id);
    const source = selected?.source_id && (settings.provider_sources || []).find(item => item.id === selected.source_id);
    return selected ? {...selected,...(source ? {base_url:source.base_url,source_name:source.name,source_enabled:source.enabled} : {}),provider_id:id} : {...(settings.model || {}),provider_id:"default"};
  }

  function modelOptions(settings) {
    const defaults = settings.model || {};
    const models = [{...defaults,id:"default",name:defaults.model || "原默认模型"},...(settings.providers || [])];
    return models.filter(item => {
      const source = item.source_id && (settings.provider_sources || []).find(value => value.id === item.source_id);
      return item.model_type !== "embedding" && item.model && item.enabled !== false && (item.source_id ? source && source.enabled !== false && source.base_url : item.base_url);
    }).map(item => [item.id,`${item.source_id ? (settings.provider_sources || []).find(source => source.id === item.source_id)?.name + " · " : ""}${item.name || item.model}`]);
  }

  function buildPatch(step, data, settings, providerId = "default") {
    if (step === 0) return {identity:{name:String(data.name || "").trim(),aliases:cleanNames(data.aliases)}};
    if (step === 1) {
      if (data.chat_model_id !== undefined) {
        if (!modelOptions(settings).some(([id]) => id === data.chat_model_id)) throw new Error("请先添加供应商并选择一个已启用的聊天模型。");
        return {routing:{chat:data.chat_model_id === "default" ? "" : data.chat_model_id}};
      }
      const model = {base_url:String(data.base_url || "").trim(),model:String(data.model || "").trim(),vision:!!data.vision};
      if (data.api_key) model.api_key = data.api_key;
      if (providerId === "default") return {model};
      const providers = (settings.providers || []).map(item => {
        const clean = {...item}; delete clean.api_key_set; delete clean.api_key;
        return item.id === providerId ? {...clean,...model,enabled:!!data.model_enabled} : clean;
      });
      if (!providers.some(item => item.id === providerId)) throw new Error("原模型配置已删除，请重新打开快速入门。");
      return {providers};
    }
    const adapter = ["napcat","snowluma","official"].includes(data.adapter) ? data.adapter : "napcat";
    if (adapter === "official") {
      const official = {chat_policy:data.chat_policy || settings.qq_official?.chat_policy || "blacklist",enabled:!!data.qq_enabled,app_id:String(data.app_id || "").trim(),allowed_groups:cleanIds(data.official_groups),allowed_users:cleanIds(data.official_users)};
      if (data.app_secret) official.app_secret = data.app_secret;
      return {onebot:{adapter,enabled:false},qq_official:official};
    }
    const onebot = {chat_policy:data.chat_policy || settings.onebot?.chat_policy || "blacklist",adapter,enabled:!!data.qq_enabled,ws_url:String(data.ws_url || "").trim(),allowed_users:cleanIds(data.owner_ids),allowed_groups:cleanIds(data.allowed_groups)};
    if (data.access_token) onebot.access_token = data.access_token;
    return {onebot,qq_official:{enabled:false}};
  }

  function recommendations(diagnostics) {
    const tips = [];
    if (diagnostics.paused) tips.push({title:"自动回复已暂停",text:"在设置的“预算与运行”中关闭暂停，机器人才能自动回复。",action:"go-settings",label:"打开运行设置"});
    if (!diagnostics.model?.configured) tips.push({title:"还没有可用模型",text:"连接自己的模型服务后才能自然聊天；记事、待办和资料管理可先在本地使用。",action:"quick-model",label:"连接模型"});
    if (diagnostics.budget?.exhausted) tips.push({title:"今日用量达到上限",text:"等待每日额度恢复，或核对设置中的预算。调整上限前请确认服务商余额。",action:"go-settings",label:"查看预算"});
    const qq = diagnostics.qq || {};
    if (!qq.enabled) tips.push({title:"QQ 尚未启用",text:"如需 QQ 聊天，选择接入方式并连接；新配置默认接收未被屏蔽的聊天。本地功能可以继续使用。",action:"quick-qq",label:"接入 QQ"});
    else if (!qq.status?.connected) tips.push({title:"QQ 当前未连接",text:qq.adapter === "official" ? "核对 AppID、AppSecret 和开放平台权限；凭据验证通过仍需等待实际连接。" : "确认 QQ 连接器已安装、运行并登录，再检查连接地址和双方的连接口令是否一致。",action:"qq-settings",label:"检查 QQ 设置"});
    if (qq.enabled && qq.chat_policy !== "blacklist" && !qq.allowed_groups && !qq.allowed_users) tips.push({title:"还没有允许回复的聊天",text:"连接成功也不会处理白名单外的消息。填写群号或用户标识，再到“群与会话”检查回应规则。",action:"qq-settings",label:"填写允许列表"});
    const received=qq.status?.message_events_received, last=qq.status?.last_message_result || {};
    if(qq.enabled && qq.status?.connected && received === 0) tips.push({title:"QQ 在线，等待第一条聊天消息",text:"请用允许名单中的账号私聊机器人，或在允许的群中 @ 它。只有心跳而收不到消息时，检查连接器是否开启消息上报。",action:"qq-settings",label:"检查消息上报"});
    if(qq.enabled && ["private_not_allowed","group_not_allowed"].includes(last.code)) tips.push({title:"最近一条消息被允许名单拦下",text:`${last.user_id ? "发送人 " + last.user_id + "。" : ""}${last.reason || "请把对应账号或群加入允许名单后，再发送一条新消息。"}`,action:"qq-settings",label:"核对允许名单"});
    return tips;
  }

  function diagnosticTips(diagnostics) {
    const tips = recommendations(diagnostics);
    return `<div class="quick-diagnostic-tips">${tips.map(tip => `<article class="quick-tip"><div><strong>${e(tip.title)}</strong><p>${e(tip.text)}</p></div>${button(tip.label,tip.action,"small")}</article>`).join("") || '<p class="help-caption">本地检查没有发现配置阻碍。模型是否可用需单独测试，QQ 收发效果请在实际聊天中确认。</p>'}</div>`;
  }

  function diagnosticsPanel(diagnostics) {
    const checks = diagnostics.checks || [], warnings = checks.filter(item => ["error","warning"].includes(item.status)).length;
    return `<section class="panel overview-diagnostics" id="quick-diagnostics"><div class="panel-head"><div><div class="panel-kicker">LOCAL CHECK</div><h2>一键检查，知道下一步做什么</h2><p>读取当前配置和运行状态，不发送消息、不调用模型。</p></div>${button("重新检查","quick-diagnose","small","shield")}</div><div class="panel-body"><div class="diagnostic-summary"><strong>${warnings ? `${warnings} 项需要留意` : "本地检查完成"}</strong><span>检查时间 · ${e(fmt(diagnostics.generated_at,true))}</span></div>${diagnosticTips(diagnostics)}${window.ShioriPages?.qqIngress?.(diagnostics.qq?.status) || ""}<details class="details"><summary>查看全部 ${checks.length} 项本地检查</summary><div class="diagnostic-list">${checks.map(diagnosticCard).join("")}</div><div class="section-actions">${button("完整诊断与运行记录","go-diagnostics","ghost small","arrow")}</div></details></div></section>`;
  }

  function capture() {
    const form = $('[data-form="quick-start"]');
    if (form) drafts[current] = {...(drafts[current] || {}),...getForm(form)};
  }

  function open(step = 0) {
    current = Math.max(0,Math.min(2,Number(step) || 0)); drafts = {};
    targetModel = modelSelection(state.settings).provider_id;
    render();
  }

  function render() {
    state.modalKind = "wizard"; state.modalRecord = null;
    const identity = {...(state.settings.identity || {}),...(drafts[0] || {})};
    let body, footer;
    if (current === 0) {
      body = `<h3>怎么称呼它？</h3><p class="quick-intro">默认人格是初音未来。聊天时的名字和别名可以单独修改，其他触发规则会沿用现有设置。</p><div class="form-grid">${field("name","机器人名称",identity.name || "初音未来","text","提及名称或别名可以触发回应；是否回复仍遵守会话规则。","required data-full maxlength=\"40\"")}${field("aliases","别名",typeof identity.aliases === "string" ? identity.aliases : (identity.aliases || []).join("，"),"text","多个别名用逗号隔开，例如 初音、Miku。","data-full maxlength=\"1000\"")}</div>`;
      footer = button("沿用当前称呼","quick-skip","ghost") + '<button type="submit" class="btn primary">保存，下一步 →</button>';
    } else if (current === 1) {
      const selected = modelSelection(state.settings), model = {...selected,...(drafts[1] || {})};
      const options = modelOptions(state.settings);
      body = `<h3>连接你自己的模型</h3><p class="quick-intro">只需供应商地址与密钥，自动获取模型后选一个聊天模型。无需先填写复杂的任务分工。</p><div class="quick-connector-guide"><strong>第一次连接</strong><p>添加供应商 → 获取模型 → 选择一个并保存。模型列表读取不发送聊天；正式聊天按服务商规则计费。</p>${button("添加供应商并选择模型","quick-model-source","primary","plus")}</div>${options.length ? `<div class="form-grid">${select("chat_model_id","或沿用已配置的模型",options,model.chat_model_id || targetModel,"data-full")}</div><p class="help-caption">这里只调整聊天使用的模型，已有密钥、模型参数和其他任务分工会保留。</p>` : '<p class="help-caption">还没有可用模型。点击上方按钮添加，也可以暂时跳过。</p>'}<details class="details"><summary>不连接模型，可以先做什么？</summary><p class="help-caption">可以试用“记住：我喜欢抹茶”“添加待办：周末买咖啡豆”，管理记忆、导入资料和使用基础检索。自然对话、语义判断和自动学习需要可用模型。shiori 不附赠模型账户或密钥。</p></details>`;
      footer = button("暂不连接，下一步","quick-skip","ghost") + (options.length ? '<button type="submit" class="btn primary">使用所选模型，下一步 →</button>' : "");
    } else {
      const onebot = state.settings.onebot || {}, official = state.settings.qq_official || {};
      const data = {adapter:onebot.adapter || "napcat",ws_url:onebot.ws_url || "",owner_ids:(onebot.allowed_users || onebot.owner_ids || []).join("\n"),allowed_groups:(onebot.allowed_groups || []).join("\n"),app_id:official.app_id || "",official_groups:(official.allowed_groups || []).join("\n"),official_users:(official.allowed_users || []).join("\n"),...(drafts[2] || {})};
      const adapter = data.adapter, isOfficial = adapter === "official", name = isOfficial ? "QQ 官方机器人" : adapter === "snowluma" ? "SnowLuma" : "NapCat";
      const policy = data.chat_policy || (isOfficial ? official : onebot).chat_policy || "blacklist";
      const enabled = data.qq_enabled ?? (isOfficial ? official.enabled : onebot.enabled);
      const doc = isOfficial ? "https://q.qq.com/" : adapter === "snowluma" ? "https://snowluma.github.io/" : "https://napneko.github.io/";
      body = `<h3>选择 QQ 接入方式</h3><p class="quick-intro">可以跳过，先在本地聊天。接入后依次确认“已连接、收到消息、回复送达”。</p>${select("adapter","接入方式",[["napcat","NapCat"],["snowluma","SnowLuma"],["official","QQ 官方机器人"]],adapter,"data-full")}
        <div class="quick-connector-guide"><strong>${isOfficial ? e(name) : "先登录连接器，再自动配对"}</strong><p>${isOfficial ? "先在 QQ 开放平台创建机器人并取得 AppID 和 AppSecret。可用场景、权限与审核要求以平台为准。" : `请先独立安装 ${name}、启动并完成 QQ 登录。与 shiori 在同一台电脑时，优先使用下方自动配对。`}</p><a class="subtle-link" href="${doc}" target="_blank" rel="noopener noreferrer">打开 ${e(name)} 官方文档 ↗</a></div>
        ${select("chat_policy","接收哪些聊天",[["blacklist","黑名单：接收未被屏蔽的聊天（推荐）"],["whitelist","白名单：只接收指定聊天"]],policy,"data-full")}${toggle("qq_enabled","保存后启用 QQ 连接","黑名单模式下不用填账号；收到聊天会自动收集，未单独设置的聊天使用默认配置。",enabled)}<div class="form-grid">${isOfficial ? `${field("app_id","AppID",data.app_id,"text","QQ 开放平台的应用 ID。","data-full")}${field("app_secret","AppSecret","","password",official.app_secret_set ? "已保存，留空保持原值。" : "只保存在本机，不回显。","data-full autocomplete=\"new-password\"")}${textarea("official_groups","允许回复的群 OpenID",data.official_groups,"每行一个。官方群 OpenID 与普通 QQ 群号不同。")}${textarea("official_users","允许回复的用户 OpenID",data.official_users,"每行一个。使用官方事件中的用户 OpenID。")}` : `${textarea("owner_ids","允许私聊的 QQ 号",data.owner_ids,"填写用来和机器人聊天的账号，不是机器人自身的 QQ 号。每行一个。")}${textarea("allowed_groups","允许回复的 QQ 群号",data.allowed_groups,"每行一个；只使用私聊时可留空。名单中的群默认由 @、名字等回应规则决定是否回复。")}`}</div>
        ${!isOfficial ? `<div class="quick-connector-guide"><strong>本机接入 · 推荐</strong><p>保存接收规则，再检测已登录的 ${e(name)}。输入它的管理口令后，自动创建反向 WebSocket；无需自己填写连接地址。</p>${button("保存并自动配对","quick-local-qq","primary small","link")}<small class="help-caption">管理口令属于连接器管理台，与模型密钥、shiori 登录密码不同。</small></div><details class="details"><summary>连接器在另一台电脑，或需要手动连接</summary><p class="help-caption">在 ${e(name)} 中添加 OneBot v11 反向 WebSocket，填入地址和相同的连接口令，再点击下方保存。地址使用当前打开的主机；连接器需能访问这台电脑。</p><div class="code-block">${e(reverseAddress())}</div>${button("复制反向连接地址","quick-copy-address","small","link")}<div class="form-grid">${field("access_token","连接口令（Access Token）","","password",onebot.access_token_set ? "已保存，留空保持原值；连接器中使用同一口令。" : "自行设置后，在连接器中填写相同值。","data-full autocomplete=\"new-password\"")}${field("ws_url","已有正向 WebSocket 地址（可选）",data.ws_url,"text","通常留空使用反向连接；仅已有正向服务时填写。","data-full placeholder=\"ws://127.0.0.1:3001\"")}</div></details>` : ""}
        <div id="quick-qq-result" aria-live="polite"></div><div class="quick-connector-guide"><strong>最后用一条真实消息验收</strong><ol class="guide-list"><li>用自己的账号私聊机器人，或在未被屏蔽的群里 @ 机器人；白名单模式需先加入名单。</li><li>保存后在首页检查“收到消息”计数是否增加；只有心跳时检查连接器的消息上报。</li><li>看到“平台已确认回复送达”才算收发完成。收到消息却不回复时，查看黑名单/白名单、暂停开关和默认回应规则。</li></ol></div>`;
      footer = button("先在本地使用","quick-finish","ghost") + (isOfficial ? button("验证官方凭据","quick-test-official","","shield") : "") + '<button type="submit" class="btn primary">保存并检查连接 →</button>';
    }
    $("#modal").classList.remove("modal-wide");
    $("#modal-content").innerHTML = `<div class="wizard-banner"><button class="icon-button quick-close" data-action="quick-finish" aria-label="关闭快速入门">${icon("close")}</button><div class="eyebrow">SHIORI · QUICK START</div><h2>先聊起来，其他随时再设置。</h2><p>每一步单独保存；跳过时保留已有配置。</p><ol class="wizard-steps quick-steps">${steps.map((name,index) => `<li class="${index === current ? "current" : ""}" ${index === current ? 'aria-current="step"' : ""}>${index+1} ${e(name)}</li>`).join("")}</ol></div><form data-form="quick-start"><div class="wizard-body">${body}<div id="modal-error" hidden class="modal-error"></div></div><div class="modal-footer">${current > 0 ? button("上一步","quick-back","ghost") : ""}${footer}</div></form>`;
    if (!$("#modal").open) $("#modal").showModal();
    if (typeof syncToastLayer === "function") syncToastLayer();
    $("#modal").scrollTop = 0;
    $("#modal-content input:not([type=checkbox]), #modal-content select")?.focus({preventScroll:true});
  }

  function reverseAddress() { return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/onebot/v11/ws`; }

  async function finish() {
    drafts = {}; sessionStorage.setItem("shiori-wizard-skipped","1"); closeModal();
    await navigate("chat");
    toast(state.boot?.setup_required ? "可以先用本地记事与待办，之后随时继续快速入门。" : "已打开本地聊天。QQ 实际状态可在首页自检中查看。");
  }

  async function submit(form) {
    capture();
    const fresh = await api("/api/settings");
    const payload = buildPatch(current,getForm(form),fresh,targetModel);
    await api("/api/settings",{method:"PUT",body:payload});
    if (current < 2) { await bootstrap(); current += 1; render(); toast("本步设置已保存"); }
    else {
      drafts = {}; sessionStorage.setItem("shiori-wizard-skipped","1"); closeModal();
      await bootstrap(); await navigate("overview");
      $("#quick-diagnostics")?.scrollIntoView({behavior:"smooth",block:"start"});
      toast("QQ 配置已保存；连接状态以刚读取的本地自检为准。");
    }
  }

  async function testModelConnection(target) {
    const form = target.closest("form"), data = getForm(form), result = $("#model-result",form);
    if (!String(data.base_url || "").trim() || !String(data.model || "").trim()) throw new Error("请先填写服务地址和模型名称。");
    target.disabled = true; result.innerHTML = '<p class="help-caption">正在请求你的模型服务…</p>';
    try {
      const response = await api("/api/model/test",{method:"POST",body:{...modelPayload(form),provider_id:targetModel}});
      result.innerHTML = notice(response.ok ? `模型已返回结果 · ${response.latency_ms ?? "—"} ms；配置还需点击保存。` : "本次模型测试没有成功。",response.ok ? "" : "warning");
    } catch (error) { result.innerHTML = `<div class="modal-error">${e(error.message)}</div>`; }
    finally { target.disabled = false; }
  }

  async function handleAction(action,target) {
    if (action === "quick-model" || action === "quick-qq") open(action === "quick-model" ? 1 : 2);
    else if (action === "quick-model-source") {
      capture();
      await window.ShioriPages.startQuickModel(async () => { open(2); toast("聊天模型已保存，下一步可接入 QQ，也可以先在本地聊天。"); });
    } else if (action === "quick-local-qq") {
      const form = target.closest("form"), data = getForm(form);
      if ((data.chat_policy || state.settings.onebot?.chat_policy || "blacklist") === "whitelist" && !cleanIds(data.owner_ids).length && !cleanIds(data.allowed_groups).length) throw new Error("先填写至少一个允许私聊的 QQ 号或群号，避免连接成功后仍无法回复。");
      target.disabled = true;
      try {
        const fresh = await api("/api/settings");
        const patch=buildPatch(2,data,fresh);
        // Pairing generates its receiver token and enables the connector only
        // after the remote configuration succeeds. Do not require a token here
        // or interrupt an existing forward connection while opening the form.
        delete patch.onebot.enabled;delete patch.onebot.ws_url;
        await api("/api/settings",{method:"PUT",body:patch});
        drafts = {}; await bootstrap();
        await window.ShioriExperience.handleAction("local-qq",target);
      } finally { target.disabled = false; }
    }
    else if (action === "quick-skip") { capture(); current += 1; render(); }
    else if (action === "quick-back") { capture(); current -= 1; render(); }
    else if (action === "quick-finish") await finish();
    else if (action === "quick-test-model") await testModelConnection(target);
    else if (action === "quick-copy-address") {
      try { await navigator.clipboard.writeText(reverseAddress()); toast("已复制连接地址；连接口令请在连接器中单独填写。"); }
      catch { toast("浏览器未允许复制，请选中上方地址手动复制。",true); }
    } else if (action === "quick-test-official") {
      const form = target.closest("form"), data = getForm(form); target.disabled = true;
      try {
        const result = await api("/api/qq/official/test",{method:"POST",body:{app_id:data.app_id,app_secret:data.app_secret || undefined}});
        $("#quick-qq-result",form).innerHTML = notice(result.ok ? "官方凭据验证通过；保存并启用后仍需等待实际连接。" : result.message || "凭据验证未通过。",result.ok ? "" : "warning");
      } finally { target.disabled = false; }
    } else if (action === "quick-diagnose") {
      target.disabled = true;
      try {
        await bootstrap();
        $("#quick-diagnostics")?.scrollIntoView({behavior:"smooth",block:"start"});
        toast("本地检查已刷新，未调用外部模型或发送 QQ 消息。");
      } finally { target.disabled = false; }
    } else return false;
    return true;
  }

  function adapterChanged(selectElement) {
    if (!selectElement.closest('[data-form="quick-start"]')) return false;
    capture();
    delete drafts[2].qq_enabled;
    render(); return true;
  }

  return {open,submit,handleAction,adapterChanged,diagnosticsPanel,diagnosticTips,buildPatch,modelSelection,modelOptions,recommendations};
})();
