"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
async function renderKnowledgeBase() {
  const results = state.knowledgeResults, searched = results?.scope_id === state.scope;
  return heading("FIND THE RIGHT PIECE","需要的时候，想得起来。","导入本地资料，检验记忆召回，看看它为何想起这些内容。",button("人物画像","show-profile","","persona") + button("导入知识文件","upload-note","primary","upload")) + `<div class="knowledge-intro"><div>${icon("file")}<div><h3>资料留在当前会话</h3><p>支持 TXT、Markdown、CSV、JSON、PDF 与 DOCX。单文件最多 8 MB、30 万字；PDF / DOCX 需要解析依赖。不支持扫描件 OCR。</p></div></div><div>${icon("shield")}<div><h3>基础检索，无需额外模型</h3><p>默认使用词语、人物和时间等线索。语义检索可在设置中选配 Embedding，并单独核算费用。</p></div></div></div><section class="panel settings-section"><div class="section-title"><span>${icon("search")}</span><div><h2>召回试验台</h2><p>检查当前会话中，指定用户能召回哪些有效记忆。</p></div>${tag(state.settings.memory?.semantic_enabled ? "语义增强已启用" : "基础混合检索",state.settings.memory?.semantic_enabled ? "" : "gray")}</div><form data-form="knowledge-search"><div class="knowledge-search-row">${field("q","想找什么",state.knowledgeQuery || "","text","","required maxlength=\"1000\" placeholder=\"例如：我上次提到的咖啡偏好\"")}${field("user_id","以谁的身份检索",state.knowledgeUser || "owner","text","本地聊天使用 owner。","required")}<button type="submit" class="btn primary">${icon("search")}检验召回</button></div></form><p class="help-caption">此试验读取已有记忆，不生成聊天回复。开启语义增强后，查询可能产生 Embedding 调用费用。</p></section><div id="knowledge-results" class="knowledge-results">${searched ? `<div class="search-summary"><h3>${rows(results).length ? `找到 ${rows(results).length} 条相关记忆` : "没有找到相关记忆"}</h3><div class="inline-stack">${tag(label(results.mode) || "基础检索","gray")}${results.elapsed_ms !== undefined ? tag(`${results.elapsed_ms} ms`,"gray") : ""}</div></div>${results.message ? notice(results.message) : ""}${rows(results).length ? `<div class="record-grid">${rows(results).map(searchResultCard).join("")}</div>` : empty("search","这次还没想起来","试试人物、事件或原话中的关键词。也请检查记忆是否属于当前会话、是否有效、可见范围是否允许。","upload-note","导入一份资料")}` : empty("search","从一个问题，找到一段记忆","输入真正想找的问题，查看命中的原文、分数与召回原因。没有预置的演示结果。")}</div>`;
}

async function renderKnowledgeV02() {
  let html = await renderKnowledgeBase();
  const buttonRow = `<div class="knowledge-analysis"><div><h3>把近期聊天，整理成可审核的记忆。</h3><p>需要已配置的文本模型并计入用量。按上方用户整理；至少需要 4 条近期原始用户消息，且当前会话已启用自动记忆。</p></div>${button("整理当前用户记忆候选","analyze-memory","small","spark")}</div><div id="memory-analysis-result" aria-live="polite"></div>`;
  html = html.replace('</form><p class="help-caption">此试验读取已有记忆',`</form>${buttonRow}<p class="help-caption">此试验读取已有记忆`);
  const results = state.knowledgeResults;
  if (results?.scope_id === state.scope) {
    const semantics = results.semantic || {};
    const diagnostic = (semantics.error ? notice(`语义增强暂时不可用，已使用基础检索：${semantics.error}`,"warning") : semantics.enabled && !semantics.used && semantics.reason ? notice(semantics.reason) : "") + `<details class="details retrieval-diagnostics"><summary>查看本次召回诊断</summary><dl class="detail-table"><dt>检索模式</dt><dd>${e(label(results.mode))}</dd><dt>排序方法</dt><dd>${e(results.algorithm || "未返回")}</dd><dt>文本切分</dt><dd>${e(results.tokenizer || "未返回")}</dd><dt>语义增强</dt><dd>${semantics.enabled ? semantics.used ? "本次已使用" : "本次未使用" : "未启用"}</dd><dt>向量缓存命中</dt><dd>${e(semantics.cache_hits ?? 0)}</dd><dt>本次嵌入片段</dt><dd>${e(semantics.embedded_chunks ?? 0)}</dd>${semantics.dimension ? `<dt>向量维度</dt><dd>${e(semantics.dimension)}</dd>` : ""}</dl><p class="help-caption">召回分数用于对本次结果排序，不是回答正确率或置信概率。</p></details>`;
    html = html.replace('<div id="knowledge-results" class="knowledge-results">',`<div id="knowledge-results" class="knowledge-results">${diagnostic}`);
  }
  return html;
}

function contextSummaryText(result) {
  const summary = result?.summary || result?.active_summary || result?.context || "";
  return typeof summary === "string" ? summary : summary.content || summary.summary || summary.text || "";
}

async function renderKnowledge() {
  const [base,context] = await Promise.all([renderKnowledgeV02(),api(`/api/context/status?${query({scope_id:state.scope,user_id:state.knowledgeUser || "owner"})}`)]);
  state.contextStatus = context;
  const summary = contextSummaryText(context), details = typeof context.summary === "object" ? context.summary || {} : context;
  const sourceIds = context.source_ids || details.source_ids || [], section = `<section class="panel context-preview"><div class="panel-head"><div><div class="panel-kicker">CONVERSATION WITH ROOTS</div><h2>长对话，留住来处。</h2><p>用户 ${e(state.knowledgeUser || "owner")} · 当前会话摘要与来源。</p></div>${button("整理当前上下文","compact-context","small","spark")}</div><div class="panel-body">${summary ? `<p class="context-summary-text">${e(summary)}</p><div class="record-meta"><span>${sourceIds.length} 条原始消息来源</span><span>覆盖 ${e(context.covered_count ?? details.covered_message_ids?.length ?? sourceIds.length)} 条消息</span><span>${e(fmt(context.last_compacted_at || details.updated_at || details.created_at))}</span>${tag("派生摘要","gray")}</div><div class="section-actions">${button("查看来源范围","context-sources","ghost small","memory")}</div>` : '<p class="help-caption" style="margin-top:0">还没有上下文摘要。对话积累后可以手动整理，或在设置与会话中开启自动整理。</p>'}<p class="help-caption">整理需要模型并计入用量。近期原文继续保留，摘要与事实记忆分别管理。</p><div id="context-result" aria-live="polite"></div></div></section>`;
  return base.replace('<div class="knowledge-intro">',section + '<div class="knowledge-intro">');
}

async function compactContext(target) {
  const userId = $('[data-form="knowledge-search"] [name="user_id"]')?.value.trim() || state.knowledgeUser || "owner", scopeId = state.scope, original = target.innerHTML;
  target.disabled = true; target.innerHTML = '<span class="spinner small"></span>整理中，计入模型用量…';
  try {
    const result = await api("/api/context/compact",{method:"POST",body:{scope_id:scopeId,user_id:userId}});
    state.knowledgeUser = userId;
    if (state.page === "knowledge" && state.scope === scopeId) { await renderPage(); if ($("#context-result")) $("#context-result").innerHTML = notice(result.message || (result.compressed ? "上下文已整理，原始消息来源已保留。" : "本次未压缩上下文，请检查消息数量与整理设置。"),result.compressed === false ? "warning" : ""); }
    toast(result.message || (result.compressed ? "上下文已整理" : "本次没有生成新的摘要"));
  } finally { target.disabled = false; target.innerHTML = original; }
}

async function contextSources() {
  const current = state.contextStatus || {}, summary = typeof current.summary === "object" ? current.summary || {} : current, ids = current.source_ids || summary.source_ids || [];
  const messages = rows(await api(`/api/messages?${query({scope_id:state.scope,limit:200})}`));
  openModal("摘要保留了哪些来处","摘要是派生内容，原始消息仍作为事实来源。",`<p class="help-caption" style="margin-top:0;margin-bottom:15px">关联 ${ids.length} 条来源。仅展示最近 200 条记录中仍可读取的原文。</p><div class="source-list">${ids.map(id => { const message = messages.find(value => value.id === id); return `<div class="source-item"><small>${e(message ? `${message.user_id} · ${fmt(message.created_at)}` : id)}</small><p>${e(message?.content || "原文已不在最近记录范围内，或已被删除。")}</p></div>`; }).join("") || '<p class="help-caption">没有返回可展示的来源编号。</p>'}</div>`);
}
