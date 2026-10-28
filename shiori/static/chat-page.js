"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
function messageImage(url) {
  return /^https?:\/\//i.test(url) ? `<a class="image-external-link" href="${e(url)}" target="_blank" rel="noopener noreferrer">${icon("image")}查看外部图片 ↗</a>` : `<img src="${e(url)}" alt="对话附图" loading="lazy" data-action="view-image">`;
}

function renderMessageBase(item) {
  const user = item.role === "user", metadata = item.metadata || {}, images = metadata.images || item.images || [];
  const storedParts = metadata.reply_parts;
  const normalize = value => value.replace(/\r\n?/g,"\n").replace(/\n[ \t]*\n(?:[ \t]*\n)*/g,"\n\n").trim();
  const replyParts = !user && Array.isArray(storedParts) && storedParts.length>0 && storedParts.length<=30 && storedParts.every(part=>typeof part==="string" && part.trim()) && normalize(storedParts.join("\n\n"))===normalize(item.content || "") ? storedParts : [item.content];
  const references = metadata.memories || metadata.memory_ids || metadata.references || Object.keys(metadata.memory_versions || {}), jargon = metadata.jargon || metadata.jargon_ids || Object.keys(metadata.jargon_versions || {});
  const original = metadata.original_reply, adjustment = metadata.output_adjustment || {};
  const changes = [adjustment.limited === true ? "已应用字数限制" : "", adjustment.removed_parentheses === true ? "已去除括号内容" : ""].filter(Boolean);
  const originalDetails = item.role === "assistant" && typeof original === "string" && original.trim() && original !== item.content
    ? `${changes.length ? `<p class="help-caption">${e(changes.join(" · "))}</p>` : ""}<details class="message-output-original"><summary>查看原文</summary><div class="message-content">${e(original)}</div></details>` : "";
  return `<article class="message ${user ? "user" : "assistant"}"><div class="message-avatar">${user ? "你" : "ミ"}</div><div class="message-body"><div class="message-label"><span>${user ? e((item.scope_id || state.scope).startsWith("web:") && item.user_id === "owner" ? "shiori" : metadata.nickname || item.user_id || "你") : e(personaName(scope().persona_id))}</span><span>${e(fmt(item.created_at))}</span></div>${item.content ? replyParts.map(part=>`<div class="message-content">${e(part)}</div>`).join("") : ""}${originalDetails}${images.length ? `<div class="message-images">${images.map(imageURL).filter(Boolean).map(url => messageImage(url)).join("")}</div>` : ""}${references.length || jargon.length ? `<div class="message-references">${references.length ? `<button class="reference-button" data-action="message-sources" data-id="${e(item.id)}">参考 ${references.length} 条记忆 ↗</button>` : ""}${jargon.length ? `<button class="reference-button" data-action="message-sources" data-id="${e(item.id)}">理解 ${jargon.length} 个群词 ↗</button>` : ""}</div>` : ""}</div></article>`;
}

async function renderChatBase() {
  const isWeb = state.scope.startsWith("web:");
  state.messages = rows(await api(`/api/messages?${query({scope_id:state.scope})}`));
  const configNotice = state.boot?.setup_required ? notice("还没有连接语言模型。你可以先试试“记住：我喜欢抹茶”“添加待办：周末买咖啡豆”等本地命令。","warning","setup","连接模型") : "";
  return heading("A PLACE TO TALK","从一句话开始。","陪你闲聊，也认真接住每一件小事。",button("导入笔记","upload-note","","upload")) + configNotice + `<div class="chat-layout"><div class="chat-top"><div class="chat-identity"><div class="chat-avatar">✳</div><div><h3>${e(personaName(scope().persona_id))}</h3><p>${e(scope().name)} · ${isWeb ? "本地对话空间" : "QQ 历史记录"}</p></div></div><div class="inline-stack">${button("清空记录","clear-messages","ghost small","trash")}${button("人格","go-persona","small","persona")}</div></div>${!isWeb ? '<div class="chat-scope-note scope-warning">这里可以查看 QQ 会话记录。发送消息请切换到「本地聊天」，避免意外向群里发信。</div>' : ""}<div class="chat-feed" id="chat-feed">${state.messages.length ? state.messages.map(renderMessage).join("") : `<div class="empty-state"><div class="chat-welcome-glyph">✳</div><h3>见到你很开心。</h3><p>说说今天的事，或把想记住、想完成的事情交给我。</p>${isWeb ? '<div class="chat-suggestions"><button data-action="chat-suggestion" data-text="你好，介绍一下你自己吧">认识一下你</button><button data-action="chat-suggestion" data-text="记住：我喜欢喝抹茶">记住我的偏好</button><button data-action="chat-suggestion" data-text="添加待办：周末买咖啡豆">添加一件待办</button></div>' : ""}</div>`}${state.sending ? '<div class="pending-message"><span class="spinner small"></span>正在认真听，也在想…</div>' : ""}</div><form class="chat-composer" data-form="chat"><div class="image-previews" id="image-previews"></div><textarea id="chat-text" name="text" placeholder="${isWeb ? "把想说的话，放在这里…" : "请切换到本地聊天后发送消息"}" aria-label="发送消息" rows="2" ${!isWeb || state.sending ? "disabled" : ""}>${e(state.draft)}</textarea><div class="composer-bottom"><div class="composer-tools"><button class="icon-button" type="button" data-action="attach-image" title="添加图片" aria-label="添加图片" ${!isWeb || state.sending ? "disabled" : ""}>${icon("image")}</button><button class="icon-button" type="button" data-action="upload-note" title="导入文本笔记" aria-label="导入文本笔记">${icon("file")}</button><span class="composer-hint">Enter 发送 · Shift + Enter 换行</span></div>${state.sending ? button("停止等待","cancel-chat","small","stop") : `<button class="btn primary" type="submit" ${!isWeb ? "disabled" : ""}>发送 ${icon("send")}</button>`}</div></form></div><p class="help-caption">${e(state.boot?.capabilities?.vision ? "当前模型已启用图片理解。" : "发送图片前，请在设置中启用模型的视觉能力。")} 记忆与群词典按会话隔离，可随时纠正或删除。</p>`;
}

function renderImagePreviews() {
  if (!$("#image-previews")) return;
  $("#image-previews").innerHTML = state.images.map((item,index) => `<div class="image-preview"><img src="${e(item.data)}" alt="${e(item.name)}"><button type="button" data-action="remove-image" data-index="${index}" aria-label="移除${e(item.name)}">×</button></div>`).join("");
}

async function sendChat(form) {
  if (state.sending || !state.scope.startsWith("web:")) return;
  const text = $("#chat-text").value.trim();
  if (!text && !state.images.length) return;
  const images = state.images.map(item => item.data);
  state.draft = text; state.sending = true; state.controller = new AbortController();
  const controller = state.controller;
  await renderPage();
  try {
    const response = await api("/api/chat",{method:"POST",body:{scope_id:state.scope,text,images},signal:controller.signal});
    state.draft = ""; state.images = [];
    if (response.pending_actions?.length) { state.pendingToolNotice = {scope_id:state.scope,count:response.pending_actions.length}; toast(`有 ${response.pending_actions.length} 项 MCP 调用等待你审批，尚未执行。`); }
    if (response.memories?.length) toast(`已参考 ${response.memories.length} 条记忆`);
  } catch (error) { toast(error.name === "AbortError" ? "已停止等待；后台可能仍在处理，请刷新记录确认。" : error.message,error.name !== "AbortError"); }
  finally { state.sending = false; state.controller = null; await bootstrap(); if (state.page === "chat") $("#chat-text")?.focus(); }
}

async function renderChat() {
  let html = await renderChatBase();const pending = state.pendingToolNotice;
  if(!state.scope.startsWith('web:') && window.ShioriLearning)html=(await window.ShioriLearning.deliveryPanel())+html;
  if (!pending || pending.scope_id !== state.scope) return html;
  return html.replace('<div class="chat-layout">',`${notice(`刚才提出了 ${pending.count} 项 MCP 请求，请前往审批页查看当前状态。批准前不会执行，批准结果仅在本地查看。`,"warning","go-tools","查看工具请求")}<div class="chat-layout">`);
}

async function showSources(message) {
  const metadata = message.metadata || {}, memories = metadata.memories || metadata.memory_ids || metadata.references || Object.keys(metadata.memory_versions || {}), jargon = metadata.jargon || metadata.jargon_ids || Object.keys(metadata.jargon_versions || {});
  const [memoryRecords,jargonRecords] = await Promise.all([api(`/api/memories?${query({scope_id:state.scope})}`),api(`/api/jargon?${query({scope_id:state.scope})}`)]);
  const resolve = (items,all) => items.map(item => typeof item === "object" ? item : rows(all).find(record => record.id === item) || {id:item,content:"这条来源已不可用。"});
  openModal("这次对话，想起了什么","只显示当前会话的可用来源。",`<div class="source-list">${resolve(memories,memoryRecords).map(item => `<div class="source-item"><small>记忆 · ${e(item.subject || label(item.kind))}</small><p>${e(item.content || item.id)}</p></div>`).join("")}${resolve(jargon,jargonRecords).map(item => `<div class="source-item"><small>群词 · ${e(item.term)}</small><p>${e(item.meaning || item.id)}</p></div>`).join("") || '<p class="help-caption">暂无可展示来源。</p>'}</div>`);
}

function renderMessage(item) {
  const html = renderMessageBase(item), expressionIds = item.metadata?.expression_ids || [];
  if (!expressionIds.length) return html;
  return html.replace('</div></article>',`<div class="message-references"><button class="reference-button" data-action="expression-sources" data-id="${e(item.id)}">参考 ${expressionIds.length} 条表达 ↗</button></div></div></article>`);
}

async function expressionSources(message) {
  const ids = message.metadata?.expression_ids || [], records = rows(await api(`/api/expressions?${query({scope_id:state.scope})}`));
  openModal("这次对话，参考了哪些表达","显示当前仍可读取的表达与适用情境。",`<div class="source-list">${ids.map(id => { const item = records.find(value => value.id === id); return `<div class="source-item"><small>${e(item ? `${label(item.status)} · ${item.topic || "表达习惯"}` : id)}</small><p>${e(item?.term || "表达已不可用。")}</p>${item ? `<p class="help-caption">适用情境：${e(item.meaning)}</p>` : ""}</div>`; }).join("")}</div>`);
}
