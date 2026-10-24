"use strict";

// The module shares the existing authenticated API and escape helpers. Remote
// content is always rendered as text; generated personas never execute scripts.
window.MikubotExperience = (() => {
  let liveSource = null, watchedJob = "", renderedPage = "", pairTimer = null;
  let attachments = [], attachmentScope = "", distillDraft = null, currentJob = null, pair = null;
  const jobNames = {queued:"排队中",running:"进行中",awaiting_approval:"等待确认",completed:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
  const inactive = job => ["completed","failed","cancelled","interrupted"].includes(job?.status);
  const unwrapJob = value => value?.job || value;
  const jobId = value => String(unwrapJob(value)?.id || "");
  const safeLink = value => /^(https?:\/\/|\/(?!\/))/i.test(String(value || "")) ? String(value) : "";
  const safeId = id => encodeURIComponent(String(id || ""));
  const _pageRuntime={get liveSource(){return liveSource},set liveSource(value){liveSource=value},get watchedJob(){return watchedJob},set watchedJob(value){watchedJob=value},get renderedPage(){return renderedPage},set renderedPage(value){renderedPage=value},get pairTimer(){return pairTimer},set pairTimer(value){pairTimer=value},get attachments(){return attachments},set attachments(value){attachments=value},get attachmentScope(){return attachmentScope},set attachmentScope(value){attachmentScope=value},get distillDraft(){return distillDraft},set distillDraft(value){distillDraft=value},get currentJob(){return currentJob},set currentJob(value){currentJob=value},get pair(){return pair},set pair(value){pair=value}};
  const {renderChat,sessionSidebar,attachmentChips,sendChat,releaseAttachments,flushCollected,cancelCollected,close:closeChat}=window.MikubotExperienceChat.create(_pageRuntime,{inactive,jobDetail:(...args)=>jobDetail(...args)});
  const {jobDetail,renderJobs}=window.MikubotExperienceJobs.create(_pageRuntime,{jobNames,inactive,safeId,safeLink});
  const {renderSettings,localConnectorPanel,schedulerPanel,pollPairing}=window.MikubotExperienceConfig.create(_pageRuntime,{safeId});
  const {distillChooser,characterForm,generateForm,profilePanel,profileForm,profileBindForm,corpusForm,showDistillPreview}=window.MikubotExperiencePersona.create(_pageRuntime,{safeLink});
  const {markdown,renderHelp,renderOverview}=window.MikubotExperienceHelp.create(_pageRuntime,{safeLink});

  function costLabel(record = {}) {
    const configured = configuredModel();
    const known = record.cost_known === true || record.price_configured === true || Number(record.cost) > 0 || Number(configured.input_price) > 0 || Number(configured.output_price) > 0;
    return record.cost_known === false || !known ? "费用未知" : `¥ ${Number(record.cost || 0).toFixed(4)}`;
  }

  async function attachmentManager(offset=0,scopeId=state.scope) {
    const data=await api(`/api/chat/attachments?${query({scope_id:scopeId,offset,limit:50})}`);
    const purposes={context:"临时参考",memory:"长期资料记录",legacy_unknown:"旧版资料 · 用途未知"};
    const items=rows(data).map(item=>`<article class="record-card"><h3>${e(item.name || "资料")}</h3><p>${e(purposes[item.purpose] || "用途未知")} · ${e(item.characters || 0)} 字符 · ${e(fmt(item.created_at))}</p><p>${item.in_use ? "聊天或后台任务正在使用，自动清理会跳过。" : "当前没有进行中的引用。"}${item.memory_id ? " 正文保存在长期资料中，删除此记录不会删除知识正文。" : ""}</p><div class="record-actions">${button("预览与管理","attachment-preview","small","file",`data-id="${e(item.id)}" data-scope="${e(scopeId)}"`)}${item.purpose==="context"&&!item.in_use ? button("用于下一条消息","attachment-reuse","ghost small","",`data-id="${e(item.id)}" data-name="${e(item.name)}" data-scope="${e(scopeId)}"`) : ""}</div></article>`).join("");
    openModal("资料管理","刷新或重启后仍可在这里管理。临时参考24小时后可自动回收；旧版用途未知资料仅在你预览确认后删除。",`<p>临时参考 ${e(data.temporary_count || 0)} / ${e(data.temporary_limit || 200)}（全部会话共用）；长期资料不占此配额。本会话共 ${e(data.total || 0)} 份记录。</p>${button("预览安全清理","attachment-cleanup","ghost small","trash",`data-scope="${e(scopeId)}"`)}<div class="record-grid">${items || '<p>暂无资料记录。</p>'}</div><div class="record-actions">${offset ? button("上一页","attachment-manager","ghost small","",`data-offset="${Math.max(0,offset-50)}" data-scope="${e(scopeId)}"`) : ""}${offset+50<(data.total || 0) ? button("下一页","attachment-manager","ghost small","",`data-offset="${offset+50}" data-scope="${e(scopeId)}"`) : ""}</div>`);
  }


  function watch(job) {
    if (!job || inactive(job)) { closeStream(); return; }
    if (watchedJob === job.id && liveSource) return;
    closeStream(); watchedJob = job.id;
    liveSource = new EventSource(`/api/jobs/${safeId(job.id)}/stream`);
    const update = event => {
      let data; try { data = JSON.parse(event.data); } catch { return; }
      const received = data.job || data;
      if (received.id === watchedJob && received.status) applyLiveJob(received);
      else api(`/api/jobs/${safeId(watchedJob)}`).then(value => applyLiveJob(unwrapJob(value))).catch(() => {});
    };
    liveSource.onmessage = update;
    for (const name of ["job","progress","update","completed","approval","state","snapshot"]) liveSource.addEventListener(name,update);
    liveSource.onerror = () => { closeStream(); if ($("#live-job-detail")) $("#live-job-detail").insertAdjacentHTML("beforeend",'<p class="help-caption">实时连接已中断，任务仍保存在后台。点击刷新查看最新状态。</p>'); };
  }

  function closeStream() { if (liveSource) liveSource.close(); liveSource = null; watchedJob = ""; }
  function applyLiveJob(job) {
    const previous = (state.liveJobs || []).findIndex(item => item.id === job.id);
    if (previous >= 0) state.liveJobs[previous] = job;
    currentJob = job;
    if (state.page === "chat" && job.scope_id === state.scope && $("#live-job-detail")) $("#live-job-detail").innerHTML = jobDetail(job,true);
    if ($('#modal [data-job-id]')?.dataset.jobId === job.id && !$('#modal form')) { const body=$('#modal .modal-body'); if(body)body.innerHTML=jobDetail(job); }
    if (state.page === "jobs") { const existing = $$('[data-job-id]').find(item => item.dataset.jobId === job.id); if (existing) existing.outerHTML = jobDetail(job); }
    if (inactive(job)) { closeStream(); if (state.page === "chat") refreshMessages().catch(() => {}); }
  }
  async function refreshMessages() { state.messages = rows(await api(`/api/messages?${query({scope_id:state.scope})}`)); const feed = $("#chat-feed"); if (feed) { const detail = $("#live-job-detail")?.outerHTML || '<div id="live-job-detail"></div>'; feed.innerHTML = state.messages.map(renderMessage).join("") + detail; feed.scrollTop = feed.scrollHeight; } }


  // Small Markdown reader: raw HTML, remote images and script URLs remain text.


  async function showJob(id) { currentJob = unwrapJob(await api(`/api/jobs/${safeId(id)}`)); openModal("任务详情","关闭窗口后，任务仍在后台保存。",jobDetail(currentJob)); $("#modal").classList.add("modal-wide"); watch(currentJob); }

  function modelListId(form) { return $('[name="model"]',form)?.getAttribute("list") || "model-list"; }
  async function listModels(target) {
    const form = target.closest("form"); if (!form) throw new Error("请在模型配置表单中读取列表。");
    target.disabled = true;
    try { const data = modelPayload(form); if (getForm(form).id) data.provider_id = getForm(form).id; else if(form.dataset?.form === "quick-start") data.provider_id=window.MikubotQuickStart.modelSelection(state.settings).provider_id; const result = await api("/api/models",{method:"POST",body:data}), models = Array.isArray(result) ? result : result.models || result.data || [], list = document.getElementById(modelListId(form)); if (list) list.innerHTML = models.map(item => `<option value="${e(typeof item === "string" ? item : item.id)}"></option>`).join(""); toast(`已读取 ${models.length} 个模型，请在模型名称框中选择。`); }
    finally { target.disabled = false; }
  }

  async function localQQ() {
    openModal("自动配对本机 QQ","连接器需已安装、运行并完成 QQ 登录。管理口令仅用于本次配置，不保存在表单或日志中。",`<div class="form-grid">${select("adapter","本机连接器",[["napcat","NapCat"],["snowluma","SnowLuma"]],state.settings.onebot?.adapter === "snowluma" ? "snowluma" : "napcat","data-full")}${field("webui_url","管理地址","","url","检测后点选管理地址；仅访问本机地址。","required data-full placeholder=\"http://127.0.0.1:6099\"")}${field("password","管理口令 / Token","","password","这是连接器管理台的口令，不是模型密钥。","data-full autocomplete=\"off\"")}${field("totp","动态验证码（如已开启）","","text","未开启两步验证时留空。","data-full autocomplete=\"one-time-code\"")}${field("account_id","QQ 账号（可选）","","text","连接器有多个账号时，选择需要绑定的账号。","data-full")}</div><div class="section-actions">${button("检测连接器","discover-local-qq","","search")}</div><div id="local-qq-result" aria-live="polite"></div>`,"local-qq",null,"创建反向连接并验证");
  }

  function connectionResult(result) {
    const steps = result.steps || result.checks || [];
    return `<div class="local-connector-result"><strong>${e(result.message || (result.connected ? "已检测到实际连接" : "已读取连接结果"))}</strong>${steps.map(step => `<div class="connector-step"><span>${step.ok || step.status === "ok" || step.status === "connected" ? "✓" : "·"}</span><div>${e(step.title || step.step || step.name || "检查")} · ${e(step.message || step.detail || step.status || "")}</div></div>`).join("")}${result.candidates?.length ? `<div class="corpus-files">${result.candidates.map(item => `<div>${e(item.adapter || item.connector || item.name || "连接器")} · ${e(item.webui_url || item.url || "")} ${e(item.message || item.status || "")} ${item.reachable && item.webui_url ? button("使用此地址","select-local-qq","small","",`data-url="${e(item.webui_url)}" data-adapter="${e(item.adapter)}"`) : ""}</div>`).join("")}</div>` : ""}${result.connected === false ? '<p class="help-caption">配置成功与实际连接分别判断。请确认连接器仍在运行，再发送一条测试消息。</p>' : ""}</div>`;
  }

  async function handleAction(action,target) {
    if(action === "flush-chat-debounce"){await flushCollected();return true;}
    if(action === "cancel-chat-debounce"){await cancelCollected();toast("已取消收集，内容已放回输入框。");return true;}
    if(window.MikubotUpdates && await window.MikubotUpdates.handleAction(action,target))return true;
    const id = target.dataset.id;
    if (action === "new-chat") { if (state.sending || state.debouncePending) throw new Error("请先完成或取消当前回复与消息收集。"); await releaseAttachments(); const result = await api("/api/scopes",{method:"POST",body:{name:"新对话",kind:"web",private_reply_policy:"always"}}), item = result.scope || result; state.scope = item.id; state.draft="";state.images=[]; attachments=[]; localStorage.setItem("qunyu-scope",state.scope); await bootstrap(); await navigate("chat"); }
    else if (action === "start-distill") distillChooser();
    else if (action === "distill-character") characterForm();
    else if (action === "persona-generate") generateForm();
    else if (action === "https-guide") {
      const result=await api('/api/remote/https-guide',{method:'POST',body:{public_url:$('#auth-public-url')?.value || ''}});
      openModal('HTTPS部署配置',result.message,textarea('caddyfile','保存为Caddyfile',result.caddyfile,'','readonly rows="5"')+`<ol>${result.steps.map(step=>`<li>${e(step)}</li>`).join('')}</ol>`);
    }
    else if (action === "qq-self-test") {
      openModal("QQ 连通性自检","只读检查，不自动向 QQ 发送消息。",'<p class="help-caption">正在检查本机端口和实际连接…</p>');
      const result=await api('/api/qq/self-test',{method:'POST',body:{}});
      openModal("QQ 连通性自检",result.message,`<div class="diagnostic-list">${result.checks.map(c=>`<article class="record-card"><h3>${c.status==='passed'?'🟢':c.status==='failed'?'🔴':'⚪'} ${e(c.title)}</h3><p>${e(c.message)}</p></article>`).join('')}</div>`);
    }
    else if (action === "profile-new") profileForm();
    else if (action === "profile-edit") profileForm(id);
    else if (action === "profile-bind") profileBindForm(id);
    else if (action === "profile-delete") {
      const config=state.botProfiles;
      if(config.scopes.some(s=>s.profile_id===id))throw new Error("请先重新绑定使用这份配置的聊天。");
      await confirmAction("删除这份配置？","人格库和记忆会保留。",async()=>{await api('/api/bot-profiles',{method:'PUT',body:{default_id:config.default_id,items:config.items.filter(p=>p.id!==id)}});},"删除配置");
    }
    else if (action === "use-persona") {
      const config=await api('/api/bot-profiles'), profile=config.items.find(p=>p.id===config.default_id);
      await confirmAction("更换默认配置的人格？","跟随默认配置且没有人格覆盖的聊天会使用这个人格，记忆隔离不变。",async()=>{profile.values.persona_id=id;await api('/api/bot-profiles',{method:'PUT',body:{default_id:config.default_id,items:config.items}});},"设为默认人格");
    }
    else if (action === "distill-corpus") corpusForm();
    else if (action === "choose-corpus") $("#corpus-input").click();
    else if (action === "view-job") await showJob(id);
    else if (action === "job-cancel") { await api(`/api/jobs/${safeId(id)}/cancel`,{method:"POST",body:{}}); toast("已请求停止任务"); closeModal(); await bootstrap(); }
    else if (action === "job-approve") { const approvalId = target.dataset.approval, job = unwrapJob(await api(`/api/jobs/${safeId(id)}`)); if (job.approval?.id !== approvalId || job.status !== "awaiting_approval") throw new Error("这次审批已变化，请刷新任务。"); openModal("批准这一次动作",`${job.title || "任务"} · ${id}`,`<input type="hidden" name="job_id" value="${e(id)}"><input type="hidden" name="approval_id" value="${e(approvalId)}"><p class="record-text">${e(job.approval.summary)}</p><pre class="tool-review-json">${e(toolJSON(job.approval.details,12000))}</pre><p class="help-caption">批准只对本任务的本次动作生效；执行后自动继续任务。</p>`,"job-approval",null,"批准并继续"); }
    else if (action === "apply-distill") {
      const job=unwrapJob(await api(`/api/jobs/${safeId(id)}`)), result=job.result || {};
      openModal("编辑并保存人格","保存到人格库后，可在机器人配置中选择；不会自动替换正在使用的人格。",`<input type="hidden" name="job_id" value="${e(id)}">${field("name","人格名称",result.name || "","text","","required data-full maxlength=\"100\"")}${textarea("prompt","人格正文",result.prompt || result.personality || "","可以在保存前调整。","required rows=\"12\" maxlength=\"6000\"")}`,"apply-distill",null,"保存人格");
    }
    else if (action === "trial-persona") openModal("试聊人格草稿","试聊会调用模型并计入用量，不替换当前人格，也不进入聊天记忆。",`<input type="hidden" name="job_id" value="${e(id)}">${textarea("text","对它说一句话","你好，介绍一下你自己吧","","required maxlength=\"4000\"")}<div id="trial-response" aria-live="polite"></div>`,"trial-persona",null,"发送试聊");
    else if (action === "local-qq") await localQQ();
    else if (action === "select-local-qq") { const form=target.closest("form");$('[name="webui_url"]',form).value=target.dataset.url;$('[name="adapter"]',form).value=target.dataset.adapter; }
    else if (action === "discover-local-qq") { const form=target.closest("form");target.disabled=true;try { const result=await api("/api/qq/local/discover",{method:"POST",body:{}});$("#local-qq-result").innerHTML=connectionResult(result); } finally { target.disabled=false; } }
    else if (action === "computer-settings") { await navigate("settings"); $("#computer-section")?.scrollIntoView({block:"start"}); }
    else if (action === "computer-chat") { if (!state.scope.startsWith("web:")) state.scope=state.scopes.find(item=>item.id.startsWith("web:"))?.id || "web:default";state.chatMode="computer";await navigate("chat"); }
    else if (action === "new-admin") openModal("添加管理员账号","填写连接器提供的真实发送者 ID。官方用户 OpenID 与普通 QQ 号不同。",`<div class="form-grid">${select("platform","账号来源",[["onebot","NapCat / SnowLuma（QQ 号）"],["qqofficial","QQ 官方机器人（OpenID）"]],"onebot","data-full")}${field("account_id","账号 ID","","text","不要填写昵称。","required data-full")}${field("label","显示备注","","text","例如：我的 QQ、小号。","data-full")}${field("instance_id","连接实例（可选）","","text","留空时由服务端按当前连接处理。","data-full")}</div>`,"new-admin",null,"添加管理员");
    else if (action === "remove-admin") await confirmAction("移除这个管理员？","后续电脑操作和任务继续时会重新校验权限。已发生的操作不会撤销。",async()=>{await api(`/api/admins/${safeId(id)}`,{method:"DELETE"});},"移除账号");
    else if (action === "pair-admin" || action === "pair-admin-auto") { pair=await api("/api/admins/pairing",{method:"POST",body:{automatic:action === "pair-admin-auto"}});openModal("绑定我的 QQ","使用目标 QQ 私聊机器人发送下方配对码。60秒快速配对将自动绑定，手动模式需回到此处核对账号。",`<div class="pairing-code">${e("/绑定 " + (pair.code || ""))}</div><p class="help-caption">有效至 ${e(fmt(typeof pair.expires_at === "number" ? pair.expires_at * 1000 : pair.expires_at,true))}。配对码只用于本次绑定。</p>${button("检查绑定消息","check-admin-pairing","primary","refresh")}<div id="pairing-result"></div>`); if(pair.automatic){$("#pairing-result").textContent="配对模式已开启60秒，持码账号私聊发送后将自动成为管理员。请只向自己的账号提供配对码。";pairTimer=setTimeout(pollPairing,1000);} }
    else if (action === "job-resume") {await api(`/api/jobs/${safeId(id)}/resume`,{method:"POST",body:{}});toast("已创建续作任务；已完成的蒸馏分析将按检查点复用。");await bootstrap();}
    else if (action === "runtime-check") {const result=await api("/api/computer/runtimes");openModal("执行环境检查","不会安装组件，也不会运行任务。",notice(result.docker.message,result.docker.available?"":"warning"));}
    else if (action === "check-admin-pairing") { if(!pair?.id)throw new Error("请重新生成配对码。");const result=await api(`/api/admins/pairing/${safeId(pair.id)}`);pair={...pair,...result};const account=result.identity || result.candidate || result.account || result.user;$("#pairing-result").innerHTML=result.status === "bound" ? notice("管理员已绑定，配对码已失效。", "success") : account ? `<div class="local-connector-result"><strong>${e(account.label || account.nickname || "收到配对消息")}</strong><p>${e(account.platform || "qq")} · ${e(account.user_id || account.account_id)}</p>${button("确认添加此管理员","confirm-admin-pairing","primary small","shield")}</div>` : notice(result.message || "尚未收到配对消息，请检查 QQ 连接后重试。", "warning"); }
    else if (action === "confirm-admin-pairing") { if(!pair?.id)throw new Error("请重新生成配对码。");await api(`/api/admins/pairing/${safeId(pair.id)}/confirm`,{method:"POST",body:{}});pair=null;closeModal();toast("QQ 管理员已绑定");await bootstrap(); }
    else if (action === "attachment-options" || action === "upload-note") openModal("上传资料，选择用途","文件解析保存在本机；作为模型参考的选中文本会发送给你配置的模型服务。",`<div class="distill-options"><button class="distill-option" data-action="choose-attachment" data-purpose="context">${icon("file")}<strong>仅本次对话参考</strong><small>下一条消息参考；合计最多8万字符，未使用资料24小时后自动回收。</small></button><button class="distill-option" data-action="choose-attachment" data-purpose="memory">${icon("memory")}<strong>保存为长期资料</strong><small>最多30万字符，放入当前会话的可检索资料，不占临时附件配额。</small></button><button class="distill-option" data-action="distill-corpus">${icon("persona")}<strong>蒸馏人格</strong><small>上传 ZIP，独立分析目标人物，不写入当前记忆。</small></button></div>${button("管理已上传的资料","attachment-manager","ghost","file")}`);
    else if (action === "attachment-manager") await attachmentManager(Number(target.dataset.offset)||0,target.dataset.scope || state.scope);
    else if (action === "attachment-preview") {
      const scopeId=target.dataset.scope || state.scope,item=await api(`/api/chat/attachments/${safeId(id)}?${query({scope_id:scopeId})}`);
      openModal("资料预览",item.in_use ? "此资料正在被使用。主动删除会撤回来源，后续模型发送或电脑操作将停止。" : "删除不可撤销；仅删除本机参考或导入记录，不删除已有长期知识正文。",`<h3>${e(item.name || "资料")}</h3>${item.purpose==="legacy_unknown" ? notice("旧版记录没有用途信息，请核对后再决定删除。","warning") : ""}<pre class="record-text">${e(item.preview || "正文保存在长期资料中，请到记忆档案查看。")}</pre><p class="help-caption">预览最多显示2000字符。</p>${button("返回资料管理","attachment-manager","ghost","",`data-scope="${e(scopeId)}"`)}${button("删除并撤回参考","attachment-delete","ghost","trash",`data-id="${e(id)}" data-scope="${e(scopeId)}"`)}`);
    }
    else if (action === "attachment-delete") {
      const scopeId=target.dataset.scope || state.scope;
      await confirmAction("删除这份参考记录？","此操作会撤回资料来源；正在使用它的模型请求或电脑任务将停止后续操作。长期知识正文保持不变。",async()=>{await api(`/api/chat/attachments/${safeId(id)}?${query({scope_id:scopeId})}`,{method:"DELETE"});attachments=attachments.filter(item=>item.id!==id);},"删除并撤回");
    }
    else if (action === "attachment-reuse") {
      if(state.sending || (target.dataset.scope && target.dataset.scope!==state.scope))throw new Error("请回到资料所属聊天，并等待本轮回复结束。");
      if(attachments.length>=5)throw new Error("一次最多引用5个附件。");
      const scopeId=state.scope,revision=_pageRuntime.attachmentRevision || 0;
      const item=await api(`/api/chat/attachments/${safeId(id)}?${query({scope_id:scopeId})}`);
      if(state.sending || scopeId!==state.scope || revision!==(_pageRuntime.attachmentRevision || 0))throw new Error("聊天或参考资料已变化，请重新选择资料，避免使用已释放的附件。");
      if(attachments.length>=5 && !attachments.some(ref=>ref.id===id))throw new Error("一次最多引用5个附件。");
      if(!attachments.some(ref=>ref.id===id))attachments.push({id,name:item.name});attachmentScope=state.scope;closeModal();await renderPage();
    }
    else if (action === "attachment-cleanup") {
      const scopeId=target.dataset.scope || state.scope,preview=await api('/api/chat/attachments/cleanup',{method:'POST',body:{scope_id:scopeId,preview:true}});
      await confirmAction("清理已释放或过期的资料？",`${preview.eligible_ids?.length || 0}份可安全回收。在用任务和用途未知的旧版资料会保留。`,async()=>{await api('/api/chat/attachments/cleanup',{method:'POST',body:{scope_id:scopeId,preview:false}});},"安全清理");
    }
    else if (action === "choose-attachment") { state.attachmentPurpose=target.dataset.purpose;$("#attachment-input").click(); }
    else if (action === "remove-attachment") { if(state.sending)throw new Error("请先等待当前回复结束。");const index=Number(target.dataset.index),item=attachments[index];if(item){await releaseAttachments([item]);attachments.splice(index,1);}$("#attachment-chips").innerHTML=attachmentChips(); }
    else if (action === "list-models") await listModels(target);
    else return false;
    return true;
  }

  async function handleSubmit(kind,form) {
    const data=getForm(form);
    if(data.selected_account) data.account_id=data.selected_account;
    if(kind === "persona-generate") {await api('/api/persona/distill/start',{method:'POST',body:{mode:'generate',name:data.name || undefined,description:data.description}});closeModal();await navigate('jobs');toast('人格生成任务已创建');}
    else if(kind === "profile-bind") {const ids=$$('input[name="scope_ids"]:checked',form).map(x=>x.value);if(!ids.length)throw new Error('请选择要绑定的聊天');await api('/api/bot-profiles/bind',{method:'POST',body:{profile_id:data.profile_id,scope_ids:ids}});closeModal();await bootstrap();toast('已绑定，聊天记录与记忆保留');}
    else if(kind === "bot-profile") {
      const config=state.botProfiles, id=data.id || 'profile_'+Date.now().toString(36);
      const values={...(config.items.find(p=>p.id===id)?.values || {}),...Object.fromEntries(['persona_id','model_id','activity','trigger_at','trigger_reply','trigger_name','plan_enabled','proactive_reply','always_reply','allow_tools','allow_mcp','use_persona_knowledge','private_always_reply','auto_memory','dictionary_learn','dictionary_use'].map(k=>[k,data[k]]))};
      for(const key of ["trigger_at","trigger_reply","trigger_name","plan_enabled","proactive_reply"])values[key]=data[key] === "" ? null : data[key] === "true";
      if(data.debounce_enabled !== undefined)values.debounce_enabled=data.debounce_enabled === "" ? null : data.debounce_enabled === "true";
      for(const key of ["debounce_seconds","debounce_max_wait_seconds"])if(data[key] !== undefined)values[key]=data[key] === "" ? null : Number(data[key]);
      const item={id,name:data.name,values}, items=config.items.some(p=>p.id===id) ? config.items.map(p=>p.id===id?item:p) : [...config.items,item];
      await api('/api/bot-profiles',{method:'PUT',body:{default_id:data.is_default?id:config.default_id,items}});closeModal();await bootstrap();toast('机器人配置已保存');
    }
    else if(kind === "distill-search") {
      const sources=String(data.source_urls || "").split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
      if(sources.length>6)throw new Error("最多提供6个资料链接，请每行填写一个。");
      if(sources.length || (!data.query && data.character)) {
        if(!data.work)throw new Error("使用自备资料时，请同时填写作品名称。");
        const body={work:data.work,character:data.character || data.query};if(sources.length)body.source_urls=[...new Set(sources)];
        const result=await api("/api/persona/distill/character/search",{method:"POST",body});
        distillDraft={...result,mode:"character",work:data.work,character:data.character || data.query};
      } else {
        const result=await api("/api/persona/distill/character/lookup",{method:"POST",body:{query:data.query,work:data.work || ""}});
        distillDraft={...result,mode:"character"};
      }
      showDistillPreview();
    }
    else if(kind === "distill-start") {
      if(!distillDraft?.lookup_id && !data.confirmed)throw new Error("请先确认角色或语料范围。");
      if(!distillDraft)throw new Error("资料预览已过期，请重新开始。");
      const selected=name=>$$(`input[name="${name}"]:checked`,form).map(item=>item.value);
      const payload={mode:distillDraft.mode,confirmed:true,name:data.name || undefined};
      if(payload.mode === "character") {
        payload.version=data.version || "";
        if(distillDraft.lookup_id) {
          const character=distillDraft.characters.find(item=>item.id===data.character_id);
          if(!character)throw new Error("请先选择一个角色。");
          payload.lookup_id=distillDraft.lookup_id;payload.character_id=character.id;payload.name=data.name || character.name;
        } else {
          payload.discovery_id=distillDraft.discovery_id;payload.candidate_ids=selected("candidate_ids");
          if(!payload.candidate_ids.length)throw new Error("至少选择一个已核对的候选。");
        }
      } else {
        payload.upload_id=distillDraft.upload_id;payload.target_speaker=data.target_speaker;payload.selected_files=selected("selected_files");
        if(!payload.selected_files.length)throw new Error("至少选择一个可读取文件。");
      }
      await api("/api/persona/distill/start",{method:"POST",body:payload});closeModal();distillDraft=null;await navigate("jobs");toast("蒸馏任务已创建，可以在后台任务中查看进度。");
    }
    else if(kind === "apply-distill") { await api(`/api/persona/distill/${safeId(data.job_id)}/apply`,{method:"POST",body:{name:data.name || undefined,prompt:data.prompt}});closeModal();toast("已保存为人格副本，原人格保留。");await bootstrap();await navigate("persona"); }
    else if(kind === "trial-persona") { const result=await api(`/api/persona/distill/${safeId(data.job_id)}/trial`,{method:"POST",body:{text:data.text}});$("#trial-response").innerHTML=`<div class="trial-response">${e(result.reply || result.message || "未返回试聊内容。")}</div>`; }
    else if(kind === "new-admin") { await api("/api/admins",{method:"POST",body:data});closeModal();toast("管理员账号已添加");await bootstrap(); }
    else if(kind === "computer-settings") { await api("/api/settings",{method:"PUT",body:{computer:{enabled:data.enabled,allow_external_tools:!!data.allow_external_tools,runtime:data.runtime || "docker",workspace:data.workspace,approval_mode:data.approval_mode || "automatic",max_steps:Number(data.max_steps),timeout_seconds:Number(data.timeout_seconds),max_output_chars:Number(data.max_output_chars)}}});toast("电脑任务设置已保存");await bootstrap(); }
    else if(kind === "time-awareness") {await api('/api/settings',{method:'PUT',body:{time:{awareness_enabled:!!data.awareness_enabled,timezone:data.timezone}}});toast('时间设置已保存');await bootstrap();}
    else if(kind === "timezone-settings") {await api('/api/settings',{method:'PUT',body:{time:{timezone:data.timezone}}});toast('时区已保存');await bootstrap();}
    else if(kind === "queue-settings") {await api('/api/settings',{method:'PUT',body:{scheduler:Object.fromEntries(['model_concurrency','model_queue_limit','background_queue_limit','scope_queue_limit'].map(k=>[k,Number(data[k])]))}});toast('调度设置已保存');await bootstrap();}
    else if(kind === "job-approval") { await api(`/api/jobs/${safeId(data.job_id)}/approve`,{method:"POST",body:{approval_id:data.approval_id}});closeModal();toast("已批准本次动作，任务继续处理。");await bootstrap(); }
    else if(kind === "local-qq") {
      const result=await api("/api/qq/local/connect",{method:"POST",body:data});
      if(result.requires_account){$("#local-qq-result").innerHTML=notice("连接器中有多个 QQ 账号，请选择一个后再次创建连接。","warning")+select("selected_account","选择 QQ 账号",(result.accounts || []).map(item=>[item.account_id,`${item.label || "QQ"} · ${item.account_id}`]),"","data-full");return true;}
      const status=await api("/api/qq/status");
      $("#local-qq-result").innerHTML=connectionResult({...result,connected:status.connected===true});
      $('[name="password"]',form).value="";
      toast(status.connected ? "QQ 已实际连接" : result.message || "连接配置已写入，请等待实际连接并发送测试消息。");
    }
    else if(kind === "model") { const payload=modelPayload(form);await api("/api/settings",{method:"PUT",body:{model:payload}});const result=$("#model-result",form);if(result)result.innerHTML='<p class="help-caption">配置已保存，正在测试实际连接…</p>';try{const tested=await api("/api/model/test",{method:"POST",body:{}});if(result)result.innerHTML=notice(tested.ok ? `保存并测试成功 · ${tested.latency_ms ?? "—"} ms` : tested.message || "配置已保存，本次连接测试失败。",tested.ok ? "" : "warning");toast(tested.ok ? "已保存并测试模型" : "配置已保存，连接尚未通过",!tested.ok);}catch(error){if(result)result.innerHTML=notice(`配置已保存，测试失败：${error.message}`,"warning");throw error;}state.settings=await api("/api/settings"); }
    else return false;
    return true;
  }

  async function handleChange(target) {
    if(window.MikubotUpdates && await window.MikubotUpdates.handleChange(target))return true;
    if(target.name === "selected_account"){const form=target.closest("form");$('[name="account_id"]',form).value=target.value;return true;}
    if(target.id === "chat-mode"){state.chatMode=target.value;if(target.value === "computer" && !state.settings.computer?.enabled)toast("电脑任务尚未开启，请在机器人设置中开启并指定工作目录。",true);state.draft=$("#chat-text")?.value || state.draft;await renderPage();return true;}
    if(target.id === "corpus-input" && target.files[0]){const file=target.files[0];target.value="";if(file.size>24*1024*1024)throw new Error("ZIP 文件不能超过 24 MB。请减少语料后重试。");const data=new FormData();data.append("file",file);toast("正在识别 ZIP 中的可读文件与说话者…");const result=await api("/api/persona/distill/corpus",{method:"POST",body:data});distillDraft={...result,mode:"corpus"};showDistillPreview();return true;}
    if(target.id === "attachment-input" && target.files[0]){
      const file=target.files[0],scopeId=state.scope;target.value="";
      if(file.size>8*1024*1024)throw new Error("单份资料不能超过 8 MB。");
      const purpose=state.attachmentPurpose || "context",data=new FormData();
      if(purpose==="context" && attachments.length>=5)throw new Error("一次最多引用5个附件。");
      data.append("file",file);data.append("scope_id",scopeId);data.append("purpose",purpose);
      const result=await api("/api/chat/attachments",{method:"POST",body:data});
      if(purpose === "context"){
        const item=result.attachment || result;if(!item.id)throw new Error("附件未返回有效标识，请重新上传。");
        if(state.scope!==scopeId){await releaseAttachments([item],scopeId);toast("聊天已切换，未使用的参考资料已释放。");return true;}
        attachments.push({...item,name:file.name});attachmentScope=scopeId;
      }
      closeModal();toast(result.message || (purpose === "memory" ? "已保存为当前会话的长期资料。" : "资料已附加，发送下一条消息时参考。"));await bootstrap();return true;
    }
    return false;
  }

  function handleInput(target) { const form=target.closest?.('form[data-form="model"],form[data-form="quick-start"],form[data-form="provider"]');if(form && ["base_url","model","api_key","vision"].includes(target.name)){const result=$("#model-result",form);if(result && result.textContent.trim())result.innerHTML='<p class="help-caption">配置已改变，请保存后重新测试。</p>';}}
  function afterRender() { renderedPage=state.page;const job=(state.liveJobs || []).find(item=>!inactive(item) && (state.page !== "chat" || item.scope_id === state.scope));if(["chat","jobs"].includes(state.page))watch(job);else closeStream();if(state.page === "settings"){$$('[data-form="model"] button[type="submit"]').forEach(item=>item.textContent="保存并测试");} }

  // Capture aliases before exported methods are installed; recursion is avoided.
  const originalPersonas = () => globalThis.renderPersonas();
  return {profilePanel,renderOverview,renderChat,sendChat,renderJobs,renderSettings,renderHelp,renderPersonas:async()=>{
    const html=await originalPersonas();return html.replace(button("创造人格","new-persona","primary","plus"),button("直接生成","persona-generate","primary","spark")+button("蒸馏人格","start-distill","","spark")+button("手动创建","new-persona","","plus"));
  },baseSettings:()=>globalThis.renderSettings(),afterRender,handleAction,handleSubmit,handleChange,handleInput,markdown,jobDetail,connectionResult,costLabel,close:()=>{closeChat();clearTimeout(pairTimer);closeStream();}};
})();
