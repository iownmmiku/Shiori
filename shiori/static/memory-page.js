"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
async function renderMemoryBase() {
  state.records = rows(await api(`/api/memories?${query({scope_id:state.scope,q:state.filters.q,status:state.filters.status})}`));
  return heading("LITTLE THINGS, WELL REMEMBERED","记忆档案","事实、偏好与共同经历，都有出处，也能被纠正。",button("导入笔记","upload-note","","upload") + button("记住一件事","new-memory","primary","plus")) + listToolbar("搜索内容、人物或关键词",[["","全部"],["active","有效"],["archived","归档"],["superseded","已替代"]]) + notice("当前会话的记忆只在这个空间召回。纠正内容会更新版本；归档后不再参与回答。") + (state.records.length ? `<div class="record-grid">${state.records.map(item => `<article class="record-card"><div class="record-card-head"><div class="inline-stack">${tag(label(item.kind))}${item.pinned ? tag("长期保留","gold") : ""}</div>${tag(label(item.status),item.status === "active" ? "" : "gray")}</div>${item.subject ? `<h3 style="font-size:12px;margin-bottom:8px">${e(item.subject)}</h3>` : ""}<p class="record-text">${e(item.content)}</p><div class="record-meta"><span>${iconInline("persona")}${e(item.user_id || "owner")}</span><span>${e(label(item.visibility))}</span><span>v${e(item.version || 1)} · ${e(fmt(item.updated_at || item.created_at))}</span></div><div class="record-actions">${button("详情 / 来源","memory-detail","ghost small","",`data-id="${e(item.id)}"`)}${button("纠正","edit-memory","small","edit",`data-id="${e(item.id)}"`)}${button(item.status === "archived" ? "恢复" : "归档","archive-memory","ghost small","",`data-id="${e(item.id)}"`)}<button class="icon-button push-right" data-action="delete-memory" data-id="${e(item.id)}" aria-label="删除记忆">${icon("trash")}</button></div></article>`).join("")}</div>` : empty("memory","值得记住的事，还在路上","告诉它你的偏好，或在这里手动添加一段记忆。也可以导入 TXT / Markdown 笔记。","new-memory","添加第一条记忆"));
}

function memoryModalBase(item = null) {
  openModal(item ? "纠正一段记忆" : "记住一件事","保留来源与版本，让记忆准确，也让你始终能修改它。",`<div class="form-grid">${scopesField(item?.scope_id)}${textarea("content","记忆内容",item?.content || "","写清人物、事实与时间；每条记忆聚焦一件事。","required maxlength=\"6000\"")}${field("subject","关于谁 / 什么",item?.subject || "","text","可填写人物称呼、项目或事件。")}${field("user_id","所属用户",item?.user_id || "owner","text","本地聊天用户固定为 owner。")}${select("kind","记忆类型",["fact","preference","event","note"],item?.kind || "fact")}${select("visibility","可见范围",["private","group"],item?.visibility || "private")}${field("importance","重要程度（0—1）",item?.importance ?? .6,"number","","min=\"0\" max=\"1\" step=\"0.1\"")}${field("valid_to","有效至（可选）",toLocal(item?.valid_to),"datetime-local","留空表示持续有效。")}</div>${toggle("pinned","长期保留","保留重要约定，避免被普通整理策略归档。",item?.pinned)}`,"memory",item);
}

function memoryCard(item,candidate = false) {
  const confidence = Number(item.confidence);
  const sourceScope = state.memoryAll !== false ? `<span>来源会话 · ${e(scopeName(item.scope_id))}</span>` : "";
  return `<article class="record-card"><div class="record-card-head"><div class="inline-stack">${tag(label(item.kind))}${item.pinned ? tag("长期保留","gold") : ""}</div>${tag(candidate ? "待你确认" : label(item.status),candidate ? "gold" : item.status === "active" ? "" : "gray")}</div>${item.subject ? `<h3 style="font-size:12px;margin-bottom:8px">${e(item.subject)}</h3>` : ""}<p class="record-text">${e(item.content)}</p>${candidate && (item.review_reason || item.reason) ? `<div class="review-reason">${icon("info")}<span>${e(item.review_reason || item.reason)}</span></div>` : ""}<div class="record-meta">${sourceScope}<span>${e(item.user_id || "owner")}</span><span>${e(label(item.visibility))}</span>${Number.isFinite(confidence) ? `<span>提取置信度 ${Math.round(confidence * 100)}%</span>` : ""}<span>${(item.source_ids || []).length} 条来源 · v${e(item.version || 1)}</span></div><div class="record-actions">${candidate ? button("审核与冲突","review-memory","primary small","check",`data-id="${e(item.id)}"`) + button("不记住","reject-memory","ghost small","",`data-id="${e(item.id)}"`) : button("详情 / 来源","memory-detail","ghost small","",`data-id="${e(item.id)}"`)}${button("纠正","edit-memory","small","edit",`data-id="${e(item.id)}"`)}${!candidate && !["superseded","rejected","deleted"].includes(item.status) ? button(item.status === "archived" ? "恢复" : "归档","archive-memory","ghost small","",`data-id="${e(item.id)}"`) : ""}<button class="icon-button push-right" data-action="delete-memory" data-id="${e(item.id)}" aria-label="删除记忆">${icon("trash")}</button></div></article>`;
}

async function renderMemoryRecords() {
  const candidate = state.filters.status === "pending";
  const all = state.memoryAll !== false;
  const key=JSON.stringify([all,state.scope,state.filters.status || "",state.filters.q || ""]);
  if(state.memoryPage?.key !== key)state.memoryPage={key,offset:0};
  const params={scope_id:all?undefined:state.scope,q:state.filters.q,status:candidate?undefined:state.filters.status,limit:50,offset:state.memoryPage.offset};
  const page=await api(`${candidate?"/api/memory/candidates":"/api/memories"}?${query(params)}`);
  state.records=rows(page);state.memoryMeta=page;
  if(page.total && state.memoryPage.offset>=page.total){state.memoryPage.offset=Math.floor((page.total-1)/50)*50;return renderMemoryRecords();}
  const pager=page.total>0?`<div class="memory-pagination record-actions" aria-label="记忆分页"><span>共 ${e(page.total)} 条 · 第 ${Math.floor(page.offset/50)+1} 页</span>${button("上一页","memory-page","ghost small","",`data-offset="${Math.max(0,page.offset-50)}" ${page.offset===0?"disabled":""}`)}${button("下一页","memory-page","ghost small","",`data-offset="${page.next_offset}" ${page.has_more?"":"disabled"}`)}</div>`:"";
  const range = `<section class="record-card" aria-label="记忆查看范围"><div class="record-actions"><h3>查看范围：${all ? "全部会话" : `当前聊天 · ${e(scopeName(state.scope))}`}</h3>${button(all ? "只看当前聊天" : "查看全部会话","learning-all","small","",'data-kind="memory"')}</div><p class="help-caption">${all ? "汇总展示各个聊天保存的记忆；机器人回复时仍按各会话的隔离规则召回。" : "这里只显示当前聊天的记忆；切换到全部会话可查看其他聊天的记录。"}</p></section>`;
  const filtered = Boolean(state.filters.q || (state.filters.status && !candidate));
  const emptyTitle = filtered ? "没有符合筛选条件的记忆" : candidate ? `${all ? "" : "当前聊天"}没有待审核的记忆` : all ? "还没有保存记忆" : "当前聊天还没有保存记忆";
  const emptyHint = filtered ? "可以修改搜索关键词或状态筛选后再试。" : candidate ? "自动提取且需要审核的事实与偏好会显示在这里。" : "可以在聊天中明确说“记住”，或在这里手动添加一段记忆。";
  const emptyState = empty(candidate ? "shield" : "memory",emptyTitle,emptyHint,all ? "new-memory" : "learning-all",all ? "记住一件事" : "查看全部会话");
  return heading("LITTLE THINGS, WELL REMEMBERED","记忆档案","记得住，也记得准。让每段记忆有出处、可审核、能纠正。",button("检索试验","go-knowledge","","search") + button("记住一件事","new-memory","primary","plus")) + range + listToolbar("搜索内容、人物或关键词",[["","全部"],["active","有效"],["pending","待确认"],["archived","归档"],["superseded","已替代"],["rejected","未接纳"]]) + notice(candidate ? "自动提取的候选还不会用于回答。审核时可查看来源与可能冲突，决定保留旧事实还是用新记忆替代。" : "记忆默认按会话与用户范围隔离；加入共享空间且显式允许的群可见条目可以互通。新事实和纠错可以分开处理；被替代、归档与未接纳的内容不参与正常召回。") + (state.records.length ? `<div class="record-grid">${state.records.map(item => memoryCard(item,candidate)).join("")}</div>` : emptyState) + pager;
}

async function renderMemory() { const [base,panel,dashboard,delivery] = await Promise.all([renderMemoryRecords(),window.ShioriFeatures.memoryToolsPanel(),window.ShioriLearning?.panel("memory"),window.ShioriLearning?.deliveryPanel()]); return base.replace('<div class="toolbar">',(dashboard||"")+(delivery||"")+panel + '<div class="toolbar">'); }

async function reviewMemoryBase(item) {
  const conflicts = rows(await api(`/api/memories/${encodeURIComponent(item.id)}/conflicts`));
  state.reviewing = item;
  openModal("这段记忆，值得留下吗？","审核后它才会参与回答。发生变化和纠正旧记忆，可以分别处理。",`<div class="review-candidate"><div class="inline-stack">${tag("新候选","gold")}${tag(label(item.kind))}</div><p class="record-text">${e(item.content)}</p></div><h3 style="margin-top:20px;margin-bottom:10px">${conflicts.length ? "可能冲突的现有记忆" : "没有发现直接冲突"}</h3>${conflicts.length ? `<div class="source-list">${conflicts.map(record => `<div class="source-item"><small>${e(record.subject || label(record.kind))} · ${e(fmt(record.updated_at))}</small><p>${e(record.content)}</p></div>`).join("")}</div>${toggle("replace_conflicts","用新记忆替代以上内容","旧记忆标记为已替代并退出召回，保留来源与历史。",false)}` : '<p class="help-caption">确认后将作为有效记忆保留。若它仍不准确，可先关闭窗口并纠正内容。</p>'}<p class="help-caption">来源消息：${(item.source_ids || []).length} 条。你可以先关闭，在详情中查看原始语境。</p>`,"memory-review",item,"确认并记住");
}

async function memoryDetailBase(item) {
  const messages = rows(await api(`/api/messages?${query({scope_id:item.scope_id})}`)), sources = (item.source_ids || []).map(id => messages.find(message => message.id === id));
  openModal("记忆的来处",`${scopeName(item.scope_id)} · 版本 ${item.version || 1}`,`<p class="record-text">${e(item.content)}</p><dl class="detail-table"><dt>类型</dt><dd>${e(label(item.kind))}</dd><dt>当前状态</dt><dd>${e(label(item.status))}</dd><dt>可见范围</dt><dd>${e(label(item.visibility))}</dd><dt>所属用户</dt><dd>${e(item.user_id || "owner")}</dd><dt>创建时间</dt><dd>${e(fmt(item.created_at,true))}</dd><dt>更新时间</dt><dd>${e(fmt(item.updated_at,true))}</dd><dt>有效至</dt><dd>${item.valid_to ? e(fmt(item.valid_to,true)) : "持续有效"}</dd><dt>重要程度</dt><dd>${e(item.importance ?? "—")}</dd></dl><h3 style="margin-bottom:12px">来源消息</h3><div class="source-list">${sources.length ? sources.map((source,index) => `<div class="source-item"><small>${e(source ? `${source.user_id} · ${fmt(source.created_at)}` : item.source_ids[index])}</small><p>${e(source?.content || "原始消息已不可用。")}</p></div>`).join("") : '<p class="help-caption">手动创建或导入的记忆，没有关联聊天来源。</p>'}</div>`);
}

async function reviewMemory(item) {
  await reviewMemoryBase(item);
  const messages = rows(await api(`/api/messages?${query({scope_id:item.scope_id,limit:200})}`));
  if (state.modalRecord?.id !== item.id || !$("#modal").open) return;
  const sources = (item.source_ids || []).map(id => messages.find(message => message.id === id));
  $("#modal .modal-body").insertAdjacentHTML("beforeend",`<div class="memory-conflict-section"><h3 style="margin-bottom:10px">原始语境</h3><div class="source-list">${sources.length ? sources.map((source,index) => `<div class="source-item"><small>${e(source ? `${source.user_id} · ${fmt(source.created_at)}` : item.source_ids[index])}</small><p>${e(source?.content || "该来源不在最近 200 条记录中，或已被删除。")}</p></div>`).join("") : '<p class="help-caption">没有关联聊天来源。请结合内容与其他记忆审核。</p>'}</div></div>`);
}

function memoryModal(item = null) {
  memoryModalBase(item);
  $("#modal .form-grid").insertAdjacentHTML("beforeend",field("property","事实属性（可选）",item?.property || "","text","用于识别同一事实的变化，例如 residence_city、饮食偏好。相同主体与属性的不同内容会提示冲突。","data-full maxlength=\"120\""));
  $("#modal .form-grid").insertAdjacentHTML("afterend",toggle("shareable","允许在指定共享空间中使用","仅对群可见记忆生效。来源与目标会话必须加入同一共享空间；私有内容始终隔离。",item?.shareable ?? false));
}

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

async function memoryDetail(item) {
  await memoryDetailBase(item);
  const conflicts = rows(await api(`/api/memories/${encodeURIComponent(item.id)}/conflicts`));
  if (!$("#modal").open) return;
  $("#modal .modal-body").insertAdjacentHTML("beforeend",`<div class="memory-conflict-section"><h3 style="margin-bottom:10px">${conflicts.length ? "可能冲突的内容" : "未发现直接冲突"}</h3>${conflicts.length ? `<div class="source-list">${conflicts.map(record => `<div class="source-item"><small>${e(label(record.status))} · ${e(record.subject || label(record.kind))}</small><p>${e(record.content)}</p></div>`).join("")}</div>` : '<p class="help-caption">自动冲突检测只提供线索，仍需结合原始语境判断。</p>'}</div>`);
}
