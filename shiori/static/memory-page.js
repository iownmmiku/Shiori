"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
async function renderMemoryBase() {
  state.records = rows(await api(`/api/memories?${query({scope_id:state.scope,q:state.filters.q,status:state.filters.status})}`));
  return heading("LITTLE THINGS, WELL REMEMBERED","记忆档案","事实、偏好与共同经历，都有出处，也能被纠正。",button("导入笔记","upload-note","","upload") + button("记住一件事","new-memory","primary","plus")) + listToolbar("搜索内容、人物或关键词",[["","全部"],["active","有效"],["archived","归档"],["superseded","已替代"]]) + notice("当前会话的记忆只在这个空间召回。纠正内容会更新版本；归档后不再参与回答。") + (state.records.length ? `<div class="record-grid">${state.records.map(item => `<article class="record-card"><div class="record-card-head"><div class="inline-stack">${tag(label(item.kind))}${item.pinned ? tag("长期保留","gold") : ""}</div>${tag(label(item.status),item.status === "active" ? "" : "gray")}</div>${item.subject ? `<h3 style="font-size:12px;margin-bottom:8px">${e(item.subject)}</h3>` : ""}<p class="record-text">${e(item.content)}</p><div class="record-meta"><span>${iconInline("persona")}${e(item.user_id || "owner")}</span><span>${e(label(item.visibility))}</span><span>v${e(item.version || 1)} · ${e(fmt(item.updated_at || item.created_at))}</span></div><div class="record-actions">${button("详情 / 来源","memory-detail","ghost small","",`data-id="${e(item.id)}"`)}${button("纠正","edit-memory","small","edit",`data-id="${e(item.id)}"`)}${button(item.status === "archived" ? "恢复" : "归档","archive-memory","ghost small","",`data-id="${e(item.id)}"`)}<button class="icon-button push-right" data-action="delete-memory" data-id="${e(item.id)}" aria-label="删除记忆">${icon("trash")}</button></div></article>`).join("")}</div>` : empty("memory","值得记住的事，还在路上","告诉它你的偏好，或在这里手动添加一段记忆。也可以导入 TXT / Markdown 笔记。","new-memory","添加第一条记忆"));
}

function memoryModalBase(item = null) {
  const eventMode=!item||!!memoryEpisode(item),kind=item?.kind||"event";
  openModal(item ? "纠正一段记忆" : "记住一件事","写下发生过什么，保留原始依据；没有提到的细节可以留空。",`<div class="form-grid">${scopesField(item?.scope_id)}${select("kind","记忆类型",["event","fact","preference","note"],kind,"data-full")}</div><div id="memory-event-editor" ${kind==="event"&&eventMode?"":"hidden"}>${memoryEpisodeFields(item,kind==="event"&&eventMode)}</div><div id="memory-plain-editor" ${kind==="event"&&eventMode?"hidden":""}>${textarea("content","记忆原文",item?.content || "","旧记忆会保留原文；修改类型不会自动编造事件。",`${kind==="event"&&eventMode?"disabled":"required"} maxlength="${item?.kind==="note"?300000:6000}" rows="6"`)}${kind==="event"&&!eventMode?`<p class="memory-legacy-note">这是尚未结构化的旧事件。${button("整理为结构化事件","memory-structure-event","ghost small","edit")}</p>`:""}</div><input type="hidden" name="memory_event_mode" value="${eventMode?"structured":"legacy"}"><div class="form-grid">${field("subject","关于谁 / 什么",item?.subject || "","text","可填写人物称呼、项目或事件。")}${field("user_id","所属用户",item?.user_id || "owner","text","本地聊天用户固定为 owner。")}${select("visibility","可见范围",["private","group"],item?.visibility || "private")}${field("importance","重要程度（0—1）",item?.importance ?? .6,"number","","min=\"0\" max=\"1\" step=\"0.1\"")}${field("valid_to","有效至（可选）",toLocal(item?.valid_to),"datetime-local","留空表示持续有效，不代表事件发生时间。")}${field("property","事实属性（可选）",item?.property || "","text","事实与偏好可用于识别同一属性的变化。","data-full maxlength=\"120\"")}</div>${toggle("pinned","长期保留","保留重要约定，避免被普通整理策略归档。",item?.pinned)}${toggle("shareable","允许在指定共享空间中使用","仅对群可见记忆生效；来源与目标会话必须加入同一共享空间。",item?.shareable ?? false)}`,"memory",item);
}

function memoryCard(item,candidate = false) {
  const confidence = Number(item.confidence);
  const sourceScope = state.memoryAll !== false ? `<span>来源会话 · ${e(scopeName(item.scope_id))}</span>` : "";
  const episode=memoryEpisode(item);
  return `<article class="record-card ${episode?"memory-event-card":""}"><div class="record-card-head"><div class="inline-stack">${tag(label(item.kind))}${item.kind==="event"&&!episode?tag("旧版原文","gray"):""}${item.pinned ? tag("长期保留","gold") : ""}</div>${tag(candidate ? "待你确认" : label(item.status),candidate ? "gold" : item.status === "active" ? "" : "gray")}</div>${episode?`<p class="memory-event-time">${icon("clock")}${e(memoryEventTime(item))}</p><h3 class="memory-event-title">${e(episode.title||item.subject||"未命名事件")}</h3><p class="record-text memory-event-summary">${e(episode.summary||"尚未填写概述，可打开详情查看经过。")}</p>${memoryEventContext(episode)}`:`${item.subject ? `<h3 style="font-size:12px;margin-bottom:8px">${e(item.subject)}</h3>` : ""}<p class="record-text">${e(item.content)}</p>${item.kind==="event"?`<p class="memory-event-time">${e(memoryEventTime(item))}</p>`:""}`}${candidate && (item.review_reason || item.reason) ? `<div class="review-reason">${icon("info")}<span>${e(item.review_reason || item.reason)}</span></div>` : ""}<div class="record-meta">${sourceScope}<span>${e(item.user_id || "owner")}</span><span>${e(label(item.visibility))}</span>${Number.isFinite(confidence) ? `<span>提取置信度 ${Math.round(confidence * 100)}%</span>` : ""}<span>${(item.source_ids || []).length} 条来源 · v${e(item.version || 1)}</span></div><div class="record-actions">${candidate ? button("审核与冲突","review-memory","primary small","check",`data-id="${e(item.id)}"`) + button("不记住","reject-memory","ghost small","",`data-id="${e(item.id)}"`) : button(episode?"经过与来源":"详情 / 来源","memory-detail","ghost small","",`data-id="${e(item.id)}"`)}${button("纠正","edit-memory","small","edit",`data-id="${e(item.id)}"`)}${!candidate && !["superseded","rejected","deleted"].includes(item.status) ? button(item.status === "archived" ? "恢复" : "归档","archive-memory","ghost small","",`data-id="${e(item.id)}"`) : ""}<button class="icon-button push-right" data-action="delete-memory" data-id="${e(item.id)}" aria-label="删除记忆">${icon("trash")}</button></div></article>`;
}

async function renderMemoryRecords() {
  const request=window.ShioriMemoryEvents.beginList();
  const candidate = state.filters.status === "pending";
  const all = state.memoryAll !== false;
  const key=JSON.stringify([all,state.scope,state.filters.status || "",state.filters.q || "",state.filters.kind||""]);
  if(state.memoryPage?.key !== key)state.memoryPage={key,offset:0};
  const params={scope_id:all?undefined:state.scope,q:state.filters.q,status:candidate?undefined:state.filters.status,kind:state.filters.kind,limit:50,offset:state.memoryPage.offset};
  const page=await api(`${candidate?"/api/memory/candidates":"/api/memories"}?${query(params)}`);
  if(!window.ShioriMemoryEvents.listCurrent(request))return "";
  state.records=rows(page);state.memoryMeta=page;
  if(page.total && state.memoryPage.offset>=page.total){state.memoryPage.offset=Math.floor((page.total-1)/50)*50;return renderMemoryRecords();}
  const pager=page.total>0?`<div class="memory-pagination record-actions" aria-label="记忆分页"><span>共 ${e(page.total)} 条 · 第 ${Math.floor(page.offset/50)+1} 页</span>${button("上一页","memory-page","ghost small","",`data-offset="${Math.max(0,page.offset-50)}" ${page.offset===0?"disabled":""}`)}${button("下一页","memory-page","ghost small","",`data-offset="${page.next_offset}" ${page.has_more?"":"disabled"}`)}</div>`:"";
  const range = `<section class="record-card" aria-label="记忆查看范围"><div class="record-actions"><h3>查看范围：${all ? "全部会话" : `当前聊天 · ${e(scopeName(state.scope))}`}</h3>${button(all ? "只看当前聊天" : "查看全部会话","learning-all","small","",'data-kind="memory"')}</div><p class="help-caption">${all ? "汇总展示各个聊天保存的记忆；机器人回复时仍按各会话的隔离规则召回。" : "这里只显示当前聊天的记忆；切换到全部会话可查看其他聊天的记录。"}</p></section>`;
  const filtered = Boolean(state.filters.q || state.filters.kind || (state.filters.status && !candidate));
  const emptyTitle = filtered ? "没有符合筛选条件的记忆" : candidate ? `${all ? "" : "当前聊天"}没有待审核的记忆` : all ? "还没有保存记忆" : "当前聊天还没有保存记忆";
  const emptyHint = filtered ? "可以修改搜索关键词或状态筛选后再试。" : candidate ? "自动提取且需要审核的事实与偏好会显示在这里。" : "可以在聊天中明确说“记住”，或在这里手动添加一段记忆。";
  const emptyState = empty(candidate ? "shield" : "memory",emptyTitle,emptyHint,all ? "new-memory" : "learning-all",all ? "记住一件事" : "查看全部会话");
  return heading("EVENTS, WELL REMEMBERED","记忆档案","记下发生过什么，也保留经过、结果和原始出处。",button("检索试验","go-knowledge","","search") + button("记住一件事","new-memory","primary","plus")) + range + listToolbar("搜索事件、人物或关键词",[["","全部"],["active","有效"],["pending","待确认"],["archived","归档"],["superseded","已替代"],["rejected","未接纳"]]) + `<div class="memory-kind-filters" role="group" aria-label="记忆类型筛选">${[["","所有类型"],["event","事件"],["fact","事实"],["preference","偏好"],["note","笔记"]].map(([kind,title])=>button(title,"memory-kind-filter",`small ${String(state.filters.kind||"")===kind?"primary":"ghost"}`,"",`data-kind="${kind}" aria-pressed="${String(state.filters.kind||"")===kind}"`)).join("")}</div>` + notice(candidate ? "自动提取的候选还不会用于回答。审核时可核对完整事件、原始来源与可能冲突。" : "记忆默认按会话与用户范围隔离；加入共享空间且显式允许的群可见条目可以互通。被替代、归档与未接纳的内容不参与正常召回。") + (state.records.length ? `<div class="record-grid">${state.records.map(item => memoryCard(item,candidate)).join("")}</div>` : emptyState) + pager;
}

async function renderMemory() { const [base,panel,dashboard,delivery] = await Promise.all([renderMemoryRecords(),window.ShioriFeatures.memoryToolsPanel(),window.ShioriLearning?.panel("memory"),window.ShioriLearning?.deliveryPanel()]); return base.replace('<div class="toolbar">',(dashboard||"")+(delivery||"")+panel + '<div class="toolbar">'); }

async function reviewMemoryBase(item) { return window.ShioriMemoryEvents.open(item,true); }
async function memoryDetailBase(item) { return window.ShioriMemoryEvents.open(item,false); }
async function reviewMemory(item) { return reviewMemoryBase(item); }
function memoryModal(item = null) { window.ShioriMemoryEvents.cancelDialog();memoryModalBase(item); }

async function analyzeMemory(target) {
  if (!configuredModel("memory").base_url || !configuredModel("memory").model) { toast("请先配置记忆与学习模型（或默认模型），整理会计入模型用量。",true); await navigate("settings"); return; }
  if (state.settings.paused) { toast("当前已暂停。请在设置中恢复运行后整理候选。",true); return; }
  if (!scope().auto_memory) { toast("请先在当前会话设置中开启自动记忆。",true); return; }
  const userId = $('[data-form="knowledge-search"] [name="user_id"]')?.value.trim() || state.knowledgeUser || "owner", analyzedScope = state.scope;
  state.knowledgeUser = userId;
  const original = target.innerHTML; target.disabled = true; target.innerHTML = '<span class="spinner small"></span>正在整理，计入模型用量…';
  try {
    const result = await api("/api/memory/analyze",{method:"POST",body:{scope_id:analyzedScope,user_id:userId}}), count = (result.candidates || []).length;
    if (state.scope !== analyzedScope || state.page !== "knowledge") { toast(`用户 ${userId} 当前有 ${count} 条待确认记忆`); return; }
    $("#memory-analysis-result").innerHTML = `<div class="notice ${count ? "" : "warning"}">${icon(count ? "check" : "info")}<p>${e(result.message || result.reason || (count ? `用户 ${userId} 当前有 ${count} 条待确认记忆；确认前不会用于回答。` : "此次没有返回待确认候选。可以检查原始消息数量、自动记忆规则与运行记录。"))}</p>${count ? '<button data-action="open-memory-candidates">去审核</button>' : ""}</div>`;
    toast(count ? `当前有 ${count} 条候选等你确认` : "本次没有返回待确认候选");
  } finally { target.disabled = false; target.innerHTML = original; }
}

async function memoryDetail(item) { return memoryDetailBase(item); }

function memoryEpisode(item){return item?.kind==="event"&&item.episode&&typeof item.episode==="object"&&!Array.isArray(item.episode)?item.episode:null;}
function memoryEventTime(item){
  const start=item.event_time_start||item.occurred_at||item.event_at;
  if(item.event_time_known===false||!start)return "发生时间未知";
  return `${fmt(start,true)}${item.event_time_end&&item.event_time_end!==start?" — "+fmt(item.event_time_end,true):""}`;
}
function memoryEventContext(episode){
  const participants=Array.isArray(episode.participants)?episode.participants:[];
  return `<div class="memory-event-context">${participants.length?`<p><span>参与者</span>${e(participants.join("、"))}</p>`:""}${episode.location?`<p><span>地点</span>${e(episode.location)}</p>`:""}</div>`;
}
function memoryEpisodeFields(item,enabled){
  const episode=memoryEpisode(item)||{},disabled=enabled?"":" disabled";
  return `<div class="memory-event-form"><div class="form-grid">${field("episode_title","事件标题",episode.title||item?.subject||"","text","用一句短标题区分这件事。",`data-full maxlength="120"${disabled}`)}${textarea("episode_summary","事件概述",episode.summary||"","可选，概括这件事为何值得记住。",`maxlength="500" rows="3"${disabled}`)}${textarea("episode_narrative","详细经过",episode.narrative||"","必填，描述起因、经过与明确的事实；未知的信息不要补写。",`required maxlength="3600" rows="7"${disabled}`)}${textarea("episode_participants","参与者",Array.isArray(episode.participants)?episode.participants.join("\n"):"","每行一位，最多 8 位；只填写原文中明确出现的人或称呼。",`maxlength="650" rows="3"${disabled}`)}${field("episode_location","地点",episode.location||"","text","未知时留空。",`data-full maxlength="200"${disabled}`)}${textarea("episode_outcome","结果或后续",episode.outcome||"","可选，记录实际结果、决定或尚待完成的约定。",`maxlength="600" rows="3"${disabled}`)}</div><p class="memory-event-time">${e(item?memoryEventTime(item):"发生时间未知")} · 发生时间按原始来源记录，不能用保存时间代替。</p></div>`;
}

window.ShioriMemoryEvents=(()=>{
  let listSequence=0,dialogSequence=0,sourceSequence=0,dialog=null;
  const pendingReviews=new Set();
  const snapshot=()=>({auth:state.authGeneration||0,page:state.page,scope:state.scope,render:state.renderId});
  const here=context=>context.auth===(state.authGeneration||0)&&state.authenticated!==false&&context.page===state.page&&context.scope===state.scope&&context.render===state.renderId;
  const identity=()=>typeof $==="function"?$("#modal-content")?.firstElementChild:null;
  const captureModal=()=>({...snapshot(),node:identity(),record:state.modalRecord});
  const modalCurrent=context=>here(context)&&identity()===context.node&&state.modalRecord===context.record&&!!$("#modal")?.open;
  const limited=(value,max,title)=>{const text=String(value??"").trim();if(text.length>max)throw new Error(`${title}不能超过 ${max} 字。`);return text;};
  function payload(data,item){
    const result={...data,importance:Number(data.importance),valid_to:data.valid_to?new Date(data.valid_to).toISOString():null};
    for(const key of Object.keys(result))if(key.startsWith("episode_")||key==="memory_event_mode")delete result[key];
    if(data.kind!=="event"){result.episode=null;return result;}
    if(data.memory_event_mode==="legacy"&&!memoryEpisode(item))return result;
    const participants=String(data.episode_participants||"").split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
    if(participants.length>8||participants.some(value=>value.length>80))throw new Error("参与者最多 8 位，每位不超过 80 字。");
    const narrative=limited(data.episode_narrative,3600,"详细经过");if(!narrative)throw new Error("请填写事件的详细经过。");
    result.episode={title:limited(data.episode_title,120,"事件标题"),summary:limited(data.episode_summary,500,"事件概述"),narrative,
      participants:[...new Set(participants)],location:limited(data.episode_location,200,"地点"),outcome:limited(data.episode_outcome,600,"结果或后续")};
    result.content=narrative;return result;
  }
  function eventBody(item){
    const episode=memoryEpisode(item);
    if(!episode)return `${item.kind==="event"?`<p class="memory-legacy-note">旧版事件原文 · ${e(memoryEventTime(item))}</p>`:""}<p class="record-text memory-detail-text">${e(item.content)}</p>`;
    return `<section class="memory-event-detail"><p class="memory-event-time">${icon("clock")}${e(memoryEventTime(item))}</p><h3>${e(episode.title||item.subject||"未命名事件")}</h3>${episode.summary?`<p class="memory-event-summary">${e(episode.summary)}</p>`:""}${memoryEventContext(episode)}<h4>详细经过</h4><p class="memory-event-narrative">${e(episode.narrative||"")}</p>${episode.outcome?`<h4>结果或后续</h4><p class="memory-event-narrative">${e(episode.outcome)}</p>`:""}</section>`;
  }
  function sourcesMarkup(item,page){
    const entries=Array.isArray(page?.items)?page.items:[],offset=Number(page?.offset)||0;
    const reasons=new Map([
      ["source_fingerprint_unavailable","旧记录缺少校验信息，无法核验原始来源。"],
      ["source_changed","原始来源已发生改动，无法核验当时的内容。"],
      ["source_revoked","原始来源已撤回，不再展示。"],
      ["source_missing_or_outside_scope","原始来源已不存在，或不在本条记忆所属的会话中。"],
      ["source_identity_mismatch","原始来源的身份信息与本条记忆不符，无法核验。"],
      ["invalid_source_id","来源编号无效，无法读取原始来源。"]
    ]);
    return `<h3>原始来源</h3><p class="help-caption">按原始来源编号读取。已删除、改动或不属于本条记忆的来源不显示旧摘录。</p><div class="source-list">${entries.length?entries.map(entry=>{
      const message=entry.message,available=entry.available===true&&message&&message.scope_id===item.scope_id&&message.id===entry.id;
      return `<article class="source-item ${available?"":"memory-source-unavailable"}"><small>${e(available?`${message.user_id||"未记录发送者"} · ${fmt(message.created_at,true)}`:entry.id||"来源")}</small><p>${e(available?message.content||"[无文字内容]":reasons.get(entry.reason)||"来源已不可用，可能已删除、改动或超出本条记忆的来源范围。")}</p></article>`;
    }).join(""):'<p class="help-caption">这条记忆没有关联的聊天来源，可能由手动创建或资料导入。</p>'}</div>${Number(page?.total)>0?`<div class="memory-source-pagination"><span>共 ${e(page.total)} 条来源 · 第 ${Math.floor(offset/20)+1} 页</span>${button("上一页来源","memory-source-page","ghost small","",`data-id="${e(item.id)}" data-offset="${Math.max(0,offset-20)}" ${offset===0?"disabled":""}`)}${button("下一页来源","memory-source-page","ghost small","",`data-id="${e(item.id)}" data-offset="${Number(page.next_offset)||0}" ${page.has_more?"":"disabled"}`)}</div>`:""}`;
  }
  function conflictsMarkup(conflicts,review){
    return `<section class="memory-conflict-section"><h3>${conflicts.length?"可能冲突的现有记忆":"未发现直接冲突"}</h3>${conflicts.length?`<div class="source-list">${conflicts.map(item=>{const episode=memoryEpisode(item);return `<article class="source-item"><small>${e(label(item.status))} · ${e(episode?.title||item.subject||label(item.kind))}</small><p>${e(episode?.summary||String(item.content||"").slice(0,600))}</p></article>`;}).join("")}</div>${review?toggle("replace_conflicts","用新记忆替代以上内容","旧记忆标记为已替代并退出召回，仍保留来源与历史。",false):""}`:'<p class="help-caption">冲突检测提供线索，仍需结合原始语境核对。</p>'}</section>`;
  }
  async function open(item,review=false){
    const ticket=++dialogSequence;sourceSequence++;dialog=null;
    openModal(review?"核对这段记忆":"记忆的来处","正在读取完整记忆与原始来源。",'<p class="help-caption" role="status">正在读取…</p>',"",item);
    const context=captureModal();
    try{
      const id=encodeURIComponent(item.id),[record,sources,conflicts]=await Promise.all([api(`/api/memories/${id}`),api(`/api/memories/${id}/sources?${query({offset:0,limit:20})}`),api(`/api/memories/${id}/conflicts`)]);
      if(ticket!==dialogSequence||!modalCurrent(context))return;
      const reviewing=review&&record.status==="pending";
      const body=`${reviewing?notice("确认后才会参与回答。请核对完整内容、来源与可能冲突。"):""}${eventBody(record)}<dl class="detail-table"><dt>类型</dt><dd>${e(label(record.kind))}</dd><dt>当前状态</dt><dd>${e(label(record.status))}</dd><dt>可见范围</dt><dd>${e(label(record.visibility))}</dd><dt>所属用户</dt><dd>${e(record.user_id||"owner")}</dd><dt>保存时间</dt><dd>${e(fmt(record.created_at,true))}</dd><dt>更新时间</dt><dd>${e(fmt(record.updated_at,true))}</dd><dt>有效至</dt><dd>${record.valid_to?e(fmt(record.valid_to,true)):"持续有效"}</dd><dt>重要程度</dt><dd>${e(record.importance??"—")}</dd></dl><section id="memory-sources" class="memory-source-section">${sourcesMarkup(record,sources)}</section>${conflictsMarkup(rows(conflicts),reviewing)}<div class="record-actions">${button("纠正内容","memory-event-edit","small","edit",`data-id="${e(record.id)}"`)}</div>`;
      openModal(reviewing?"这段记忆，值得留下吗？":"记忆的来处",`${scopeName(record.scope_id)} · 版本 ${record.version||1}`,body,reviewing?"memory-review":"",record,"确认并记住");
      dialog={record,review:reviewing,context:captureModal()};
    }catch(error){if(ticket===dialogSequence&&modalCurrent(context)){
      const area=$("#modal .modal-body");if(area)area.innerHTML=`<p class="inline-error">${e(error.message)}</p>${button("重新读取","memory-dialog-retry","small","refresh",`data-id="${e(item.id)}"`)}`;
      dialog={record:item,review,context:captureModal()};
    }}
  }
  async function sourcePage(target){
    const currentDialog=dialog;if(!currentDialog||target.dataset.id!==currentDialog.record.id||!modalCurrent(currentDialog.context))return true;
    const sequence=++sourceSequence,offset=Math.max(0,Number(target.dataset.offset)||0);target.disabled=true;
    try{
      const page=await api(`/api/memories/${encodeURIComponent(currentDialog.record.id)}/sources?${query({offset,limit:20})}`);
      if(sequence!==sourceSequence||dialog!==currentDialog||!modalCurrent(currentDialog.context))return true;
      const node=$("#memory-sources");if(node)node.innerHTML=sourcesMarkup(currentDialog.record,page);
    }catch(error){if(sequence===sourceSequence&&dialog===currentDialog&&modalCurrent(currentDialog.context))toast(error.message,true);}
    finally{if(target.isConnected!==false)target.disabled=false;}return true;
  }
  function setEditor(form,structured){
    const event=$("#memory-event-editor",form),plain=$("#memory-plain-editor",form);
    if(event){event.hidden=!structured;event.querySelectorAll("input,textarea,select").forEach(node=>{node.disabled=!structured;});}
    if(plain){plain.hidden=structured;plain.querySelectorAll("input,textarea,select").forEach(node=>{node.disabled=structured;});}
    const mode=$('[name="memory_event_mode"]',form);if(mode)mode.value=structured?"structured":"legacy";
  }
  function handleChange(target){
    const form=target.closest?.('form[data-form="memory"]');if(!form||target.name!=="kind")return false;
    setEditor(form,target.value==="event");return true;
  }
  function handleAction(action,target){
    if(action==="memory-kind-filter"){
      if(state.page!=="memory")return false;
      state.filters.kind=["","fact","preference","event","note"].includes(target.dataset.kind)?target.dataset.kind:"";
      return renderPage().then(()=>true);
    }
    if(action==="memory-source-page")return sourcePage(target);
    if(action==="memory-dialog-retry"&&dialog&&modalCurrent(dialog.context))return open(dialog.record,dialog.review).then(()=>true);
    if(action==="memory-event-edit"&&dialog&&dialog.record.id===target.dataset.id&&modalCurrent(dialog.context)){memoryModal(dialog.record);return true;}
    if(action==="memory-structure-event"){
      const form=target.closest?.('form[data-form="memory"]');if(!form)return true;
      setEditor(form,true);const narrative=$('[name="episode_narrative"]',form),content=$('[name="content"]',form);
      if(narrative&&!narrative.value&&String(content?.value||"").length<=3600)narrative.value=content?.value||"";
      else if(narrative&&!narrative.value&&String(content?.value||"").length>3600)toast("原文超过 3600 字，请整理后填写详细经过；原文仍保留在原文输入框中。");
      return true;
    }
    return false;
  }
  function handleSubmit(kind,form){
    if(kind!=="memory-review")return false;
    const context=captureModal(),record=context.record,key=`${context.auth}:${record?.id}`;
    if(!record?.id||!modalCurrent(context))return Promise.resolve(true);
    if(pendingReviews.has(key))return Promise.resolve(true);
    pendingReviews.add(key);
    return (async()=>{
      try{
        await api(`/api/memories/${encodeURIComponent(record.id)}/approve`,{method:"POST",body:{replace_conflicts:!!getForm(form).replace_conflicts}});
        if(!modalCurrent(context))return true;
        closeModal();toast("这段记忆，已经认真记住了");await bootstrap();
      }catch(error){if(modalCurrent(context))throw error;if(context.auth===(state.authGeneration||0)&&state.authenticated!==false)toast(error.message,true);}
      finally{pendingReviews.delete(key);}return true;
    })();
  }
  return {payload,open,handleAction,handleChange,handleSubmit,captureSave:captureModal,saveCurrent:modalCurrent,
    beginList:()=>({...snapshot(),ticket:++listSequence}),listCurrent:request=>request.ticket===listSequence&&here(request),
    cancelDialog:()=>{dialogSequence++;sourceSequence++;dialog=null;}};
})();
