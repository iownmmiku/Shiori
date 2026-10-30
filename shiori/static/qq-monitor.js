"use strict";

// Read-only QQ workspace. All requests here are GETs; transport retries and
// message composition deliberately belong to their existing dedicated flows.
window.ShioriQQMonitor = (() => {
  const PAGE = "qqChats", INTERVAL = 5000, LIMIT = 80, MAX_MESSAGES = 500;
  const arr = value => Array.isArray(value) ? value : [];
  const isQQ = id => /^(qq|qqofficial):(group|private):[^:]+$/.test(String(id || ""));
  let chats = [], selected = "", messages = [], receipts = [];
  let search = "", kind = "all", auto = true, active = false;
  let sequence = 0, viewSequence = 0, controller = null, timer = null, busy = false;
  let hasOlder = false, historyView = false, before = "", lastRefresh = "", listError = "", feedError = "", receiptError = "";
  const here = () => active && state.page === PAGE && state.authenticated !== false;
  const current = () => chats.find(item => item.id === selected);
  const visibleChats = () => chats.filter(item => (kind === "all" || item.kind === kind) &&
    `${item.name || ""} ${item.id}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const stamp = value => value ? fmt(value, true) : "时间未记录";
  const count = value => Number.isInteger(value) && value >= 0 ? `${value.toLocaleString("zh-CN")} 条` : "条数未记录";
  const kindName = item => item.kind === "group" ? "群聊" : "私聊";
  const platformName = item => item.id.startsWith("qqofficial:") ? "官方 QQ" : "QQ";
  const preview = item => item.last_message ? String(item.last_message.content || (Number(item.last_message.metadata?.image_count) > 0 || arr(item.last_message.metadata?.images).length ? "[图片]" : "[无文本消息]")) : "暂无消息";
  const validMessages = (items, scopeId) => arr(items).filter(item => item && item.id && item.scope_id === scopeId);
  const errorText = error => String(error?.message || "读取失败，请重试。");
  function invalidate() {
    sequence++; controller?.abort(); controller = null; busy = false;
    clearTimeout(timer); timer = null;
  }
  function close() {
    active = false; viewSequence++; invalidate();
    if (state.messages === messages) state.messages = [];
    messages = []; receipts = []; before = ""; hasOlder = historyView = false;
  }
  function choose(scopeId) {
    if (scopeId === selected) return;
    selected = scopeId; messages = []; receipts = []; before = ""; hasOlder = historyView = false;
    feedError = receiptError = "";
    state.messages = messages;
    if (selected) {
      state.scope = selected;
      // Shared source dialogs resolve messages from this array, while the
      // monitor keeps its own history independently from local Chat.
      state.messages = messages;
    }
  }
  function syncShell() {
    if (selected) {
      state.scope = selected; state.messages = messages;
      const snapshot = current(), index = state.scopes.findIndex(item => item.id === selected);
      if (snapshot) {
        if (index < 0) state.scopes.push(snapshot); else state.scopes[index] = snapshot;
      }
      const picker = $("#global-scope"); if (picker) picker.value = selected;
    }
  }
  function schedule() {
    clearTimeout(timer); timer = null;
    if (here() && auto && !document.hidden && !busy) timer = setTimeout(() => refresh(), INTERVAL);
  }
  function statusMarkup() {
    const text = document.hidden ? "页面隐藏，刷新已暂停" : busy ? "正在读取记录…" :
      listError || feedError ? "刷新失败，可手动重试" : auto ? "自动刷新中 · 每 5 秒" : "自动刷新已暂停";
    return `<span class="qq-refresh-dot ${auto && !document.hidden && !listError && !feedError ? "live" : ""}" aria-hidden="true"></span><span>${e(text)}${lastRefresh ? `<small>上次读取 ${e(stamp(lastRefresh))}</small>` : ""}</span>`;
  }
  function listMarkup() {
    const visible = visibleChats();
    return `${listError ? `<div class="qq-inline-error" role="alert">${e(listError)}</div>` : ""}<p class="qq-list-count">${visible.length} / ${chats.length} 个 QQ 会话</p>${visible.length ? visible.map(item =>
      `<button type="button" class="qq-conversation ${item.id === selected ? "selected" : ""}" data-action="qq-monitor-select" data-scope="${e(item.id)}" aria-pressed="${item.id === selected}"><span class="qq-conversation-head"><strong>${e(item.name || item.id)}</strong><span class="qq-kind">${e(kindName(item))}</span></span><span class="qq-conversation-id">${e(platformName(item))} · ${e(item.id)}</span><span class="qq-conversation-preview">${e(preview(item))}</span><span class="qq-conversation-foot"><time>${e(item.last_at ? stamp(item.last_at) : "暂无消息时间")}</time><span>${e(count(item.message_count))}</span></span></button>`).join("") :
      `<div class="qq-empty"><strong>${chats.length ? "没有匹配的会话" : "尚无 QQ 会话"}</strong><p>${chats.length ? "试试其他名称、号码或会话类型。" : "QQ 收到消息后，会话记录会出现在这里。"}</p></div>`}`;
  }
  function headerMarkup() {
    const item = current();
    return `<div class="qq-conversation-title"><span class="eyebrow">CONVERSATION / READ ONLY</span><h2>${e(item?.name || "选择一个 QQ 会话")}</h2><p>${item ? `${e(platformName(item))} · ${e(kindName(item))} · ${e(item.id)} · ${e(count(item.message_count))}` : "查看收发记录与实际送达状态"}</p></div><div class="qq-header-actions">${historyView ? button("回到最新", "qq-monitor-latest", "small", "refresh", busy ? "disabled" : "") : ""}${button("运行日志", "qq-monitor-logs", "ghost small", "logs")}${item ? button("会话配置", "qq-monitor-settings", "ghost small", "settings", `data-scope="${e(item.id)}"`) : ""}</div>`;
  }
  const deliveryNames = {confirmed:"已确认送达", delivered:"已确认送达", failed:"发送失败", unknown:"送达未知", sending:"等待送达确认", pending:"尚未发送", not_sent:"未发送", blocked:"发送已阻止"};
  function deliveryState(status) {
    const value = String(status || "unknown");
    return `<span class="qq-delivery-state ${["confirmed", "delivered"].includes(value) ? "confirmed" : value === "failed" ? "failed" : "uncertain"}">${e(deliveryNames[value] || "状态未识别")}</span>`;
  }
  function deliveryMarkup(item) {
    if (item.role !== "assistant") return "";
    const records = receipts.filter(row => row.assistant_message_id === item.id && row.scope_id === selected);
    const metadata = item.metadata || {}, part = metadata.output_part;
    const hasPart = Number.isInteger(part?.index) && part.index > 0 && Number.isInteger(part?.total) && part.total >= part.index;
    const explicitStatus = typeof metadata.delivery_status === "string" ? metadata.delivery_status : metadata.delivery_blocked === true ? "blocked" : "";
    return `<div class="qq-delivery" aria-label="送达记录">${hasPart ? `<span>分段 ${e(part.index)} / ${e(part.total)}</span>` : ""}${records.length ? records.map(record => `<details><summary>分段送达记录 · ${arr(record.parts).length} 段</summary><ol>${arr(record.parts).map(entry => `<li>${deliveryState(entry.status)}<span class="qq-part-text">${e(entry.text || "")}</span></li>`).join("")}</ol></details>`).join("") : explicitStatus ? deliveryState(explicitStatus) : '<span class="qq-delivery-missing">无送达回执</span>'}</div>`;
  }
  function monitorMessage(item) {
    const html = renderMessage(item);
    if (item.role !== "user") return html;
    const nickname = typeof item.metadata?.nickname === "string" ? item.metadata.nickname.trim() : "";
    const fallback = String(item.scope_id || selected).includes(":group:") ? "群友" : "用户";
    const avatar = nickname ? Array.from(nickname)[0] : fallback;
    // Replace only the shared renderer's avatar element. Message content and
    // its escaping remain untouched, including any literal “你” in the text.
    return html.replace('<div class="message-avatar">你</div>', `<div class="message-avatar" role="img" aria-label="${e("QQ 发送者：" + (nickname || item.user_id || fallback))}">${e(avatar)}</div>`);
  }
  function feedMarkup() {
    if (!selected) return '<div class="qq-empty"><strong>QQ 聊天监控</strong><p>从会话列表中选择群聊或私聊。</p></div>';
    return `${feedError ? `<div class="qq-inline-error" role="alert">${e(feedError)}</div>` : ""}${receiptError ? `<p class="qq-inline-error">分段回执读取失败：${e(receiptError)}</p>` : ""}<div class="qq-history-control">${hasOlder ? button("加载更早记录", "qq-monitor-older", "ghost small", "clock", busy ? "disabled" : "") : messages.length ? '<span>已到最早记录</span>' : ""}${historyView ? button("回到最新", "qq-monitor-latest", "small", "refresh", busy ? "disabled" : "") : ""}<small>${historyView ? "正在浏览历史；刷新只更新会话列表与送达回执。" : ""}当前窗口最多显示 ${MAX_MESSAGES} 条，完整历史可继续翻页。</small></div>${messages.length ? messages.map(item => `<div class="qq-message-record" data-qq-message="${e(item.id)}">${monitorMessage(item)}${deliveryMarkup(item)}</div>`).join("") : `<div class="qq-empty"><strong>${busy ? "正在读取消息…" : feedError ? "暂时无法读取消息" : "这个会话还没有消息"}</strong><p>这里只展示已保存的真实聊天记录。</p></div>`}`;
  }
  function markup() {
    return heading("QQ / OBSERVATION", "QQ 聊天", "独立查看 QQ 群聊与私聊的收发记录。", '<span class="qq-readonly-badge">只读监控</span>') +
      `<section class="qq-monitor" id="qq-monitor"><aside class="qq-sidebar" aria-label="QQ 会话列表"><div class="qq-list-toolbar"><label class="qq-search" for="qq-monitor-search">${icon("search")}<input id="qq-monitor-search" type="search" value="${e(search)}" placeholder="搜索名称或号码" aria-label="搜索 QQ 会话"></label><div class="qq-kind-tabs" role="group" aria-label="筛选 QQ 会话类型">${[["all","全部"],["group","群聊"],["private","私聊"]].map(([value,title]) => `<button type="button" class="${kind === value ? "active" : ""}" data-action="qq-monitor-kind" data-kind="${value}" aria-pressed="${kind === value}">${title}</button>`).join("")}</div></div><div class="qq-conversations" id="qq-monitor-list">${listMarkup()}</div></aside><section class="qq-detail" aria-label="QQ 聊天记录"><header class="qq-detail-header" id="qq-monitor-header">${headerMarkup()}</header><div class="qq-refresh-bar"><div id="qq-monitor-status" class="qq-refresh-status" role="status">${statusMarkup()}</div><div class="qq-refresh-actions"><label class="qq-auto-toggle"><input id="qq-monitor-auto" type="checkbox" ${auto ? "checked" : ""}>自动刷新</label>${button("刷新", "qq-monitor-refresh", "ghost small", "refresh")}</div></div><div class="qq-feed" id="qq-monitor-feed" tabindex="0" aria-label="只读消息历史">${feedMarkup()}</div><footer class="qq-readonly-note">${icon("shield")}只读记录 · 此页不会向 QQ 发送消息。无回执不代表已送达。</footer></section></section>`;
  }
  function paint({feed = true, scroll = "keep"} = {}) {
    if (!here() || !$("#qq-monitor")) return;
    const list = $("#qq-monitor-list"), header = $("#qq-monitor-header"), status = $("#qq-monitor-status"), area = $("#qq-monitor-feed");
    if (list) {const next = listMarkup(); if (list.innerHTML !== next) list.innerHTML = next;}
    if (header) {const next = headerMarkup(); if (header.innerHTML !== next) header.innerHTML = next;}
    if (status) status.innerHTML = statusMarkup();
    if (feed && area) {
      const top = area.scrollTop, height = area.scrollHeight, atBottom = height - top - area.clientHeight < 70;
      const next = feedMarkup();
      // Leave unchanged message DOM intact so automatic refresh does not
      // discard text selection or keyboard focus while someone is reading.
      if (area.innerHTML !== next) {
        const frame = area.getBoundingClientRect?.();
        const anchor = frame && [...area.querySelectorAll("[data-qq-message]")].find(node => {
          const rect = node.getBoundingClientRect(); return rect.bottom > frame.top && rect.top < frame.bottom;
        });
        const anchorOffset = anchor ? anchor.getBoundingClientRect().top - frame.top : 0;
        const expanded = new Set([...area.querySelectorAll("[data-qq-message] details[open]")].map(node => node.closest("[data-qq-message]")?.dataset.qqMessage));
        area.innerHTML = next;
        for (const node of area.querySelectorAll("[data-qq-message]")) if (expanded.has(node.dataset.qqMessage)) for (const details of node.querySelectorAll("details")) details.open = true;
        const restored = anchor && [...area.querySelectorAll("[data-qq-message]")].find(node => node.dataset.qqMessage === anchor.dataset.qqMessage);
        area.scrollTop = scroll === "bottom" || scroll === "follow" && atBottom ? area.scrollHeight : restored ?
          area.scrollTop + restored.getBoundingClientRect().top - frame.top - anchorOffset : scroll === "prepend" ? top + area.scrollHeight - height : top;
      }
    }
    syncShell();
  }
  function merge(incoming, older = false) {
    const combined = older ? [...incoming, ...messages] : [...messages, ...incoming];
    const indexes = new Map(), result = [];
    for (const item of combined) {
      if (indexes.has(item.id)) result[indexes.get(item.id)] = item;
      else { indexes.set(item.id, result.length); result.push(item); }
    }
    return result;
  }
  async function refresh({initial = false, older = false} = {}) {
    if (!here() || document.hidden || busy) return;
    clearTimeout(timer); timer = null; busy = true;
    const ticket = ++sequence, abort = new AbortController(); controller = abort;
    const live = () => ticket === sequence && here() && !document.hidden;
    paint({feed:false});
    try {
      if (!older) {
        try {
          let result = await api("/api/qq/chats?limit=500", {signal:abort.signal});
          const items = [...arr(result.items)];
          while (result.has_more && arr(result.items).length) {
            if (!live()) return;
            result = await api(`/api/qq/chats?${query({limit:500,offset:items.length})}`, {signal:abort.signal});
            items.push(...arr(result.items));
          }
          if (!live()) return;
          chats = items.filter(item => isQQ(item.id)); listError = "";
          if (!chats.some(item => item.id === selected)) choose(chats[0]?.id || "");
        } catch (error) { if (!live()) return; listError = errorText(error); }
      }
      const scopeId = selected;
      if (!scopeId) { if (live()) lastRefresh = listError ? lastRefresh : new Date().toISOString(); return; }
      const check = () => live() && selected === scopeId;
      const cursor = older ? before : initial || !messages.length ? "" : messages.at(-1).id;
      const readingHistory = historyView && !older && !initial;
      const path = `/api/qq/chats/${encodeURIComponent(scopeId)}/messages?${query({limit:LIMIT, ...(cursor ? {[older ? "before" : "after"]:cursor} : {})})}`;
      let [batch, delivery] = await Promise.allSettled([
        readingHistory ? Promise.resolve(null) : api(path, {signal:abort.signal}), api(`/api/delivery?${query({scope_id:scopeId})}`, {signal:abort.signal})
      ]);
      if (!check()) return;
      let resetHistory = false;
      if (cursor && batch.status === "rejected" && errorText(batch.reason) === "消息游标不存在于当前会话") {
        // Another tab may have cleared this conversation. Only a confirmed
        // missing cursor resets history; network failures retain readable data.
        try {
          const latest = await api(`/api/qq/chats/${encodeURIComponent(scopeId)}/messages?${query({limit:LIMIT})}`, {signal:abort.signal});
          if (!check()) return;
          batch = {status:"fulfilled",value:latest}; resetHistory = true;
        } catch (error) { if (!check()) return; batch = {status:"rejected",reason:error}; }
      }
      if (batch.status === "fulfilled" && batch.value) {
        let result = batch.value, incoming = validMessages(result.items, scopeId);
        // Catch up in bounded batches without dropping messages that arrived
        // while the tab was hidden. A remaining backlog resumes next refresh.
        if (!older && cursor && !resetHistory) for (let page = 0; result.has_more && incoming.length && page < 4; page++) {
          const after = result.next_after || incoming.at(-1).id;
          result = await api(`/api/qq/chats/${encodeURIComponent(scopeId)}/messages?${query({after,limit:LIMIT})}`, {signal:abort.signal});
          if (!check()) return;
          const next = validMessages(result.items, scopeId); if (!next.length || next.at(-1).id === after) break;
          incoming.push(...next);
        }
        if (!cursor || older || resetHistory) {
          hasOlder = Boolean(result.has_more);
          before = result.next_before || incoming[0]?.id || before;
        }
        messages = initial || resetHistory ? incoming : merge(incoming, older); feedError = "";
        if (initial || resetHistory) historyView = false;
        else if (older) historyView = true;
        if (messages.length > MAX_MESSAGES) {
          messages = older && !resetHistory ? messages.slice(0, MAX_MESSAGES) : messages.slice(-MAX_MESSAGES);
          if (!older || resetHistory) hasOlder = true;
        }
        before = messages[0]?.id || "";
        lastRefresh = new Date().toISOString();
      } else if (batch.status === "rejected") feedError = errorText(batch.reason);
      else if (!listError) lastRefresh = new Date().toISOString();
      if (delivery.status === "fulfilled") { receipts = arr(delivery.value).filter(row => row.scope_id === scopeId); receiptError = ""; }
      else { receipts = []; receiptError = errorText(delivery.reason); }
      syncShell();
    } catch (error) { if (live()) feedError = errorText(error); }
    finally {
      if (live()) { busy = false; controller = null; paint({scroll:older ? "prepend" : initial ? "bottom" : "follow"}); schedule(); }
    }
  }
  async function render() {
    invalidate(); active = true;
    const view = ++viewSequence;
    const desired = isQQ(state.scope) ? state.scope : selected;
    choose(desired);
    await refresh({initial:true});
    // Hiding the tab invalidates network work, not this page shell. Returning
    // an empty shell here would leave nothing for visibility-resume to update.
    if (!here() || view !== viewSequence) return "";
    return markup();
  }
  function afterRender() {
    if (state.page !== PAGE || state.authenticated === false) { close(); return; }
    active = true; syncShell();
    const feed = $("#qq-monitor-feed"); if (feed) feed.scrollTop = feed.scrollHeight;
    schedule();
  }
  async function handleAction(action, target) {
    if (!action.startsWith("qq-monitor-")) return false;
    if (!here()) return true;
    if (action === "qq-monitor-select") {
      const id = target.dataset.scope;
      if (isQQ(id) && chats.some(item => item.id === id) && selected !== id) {
        invalidate(); choose(id); paint(); await refresh({initial:true});
      }
    } else if (action === "qq-monitor-kind") {
      if (["all","group","private"].includes(target.dataset.kind)) kind = target.dataset.kind;
      for (const tab of $$('#qq-monitor [data-action="qq-monitor-kind"]')) {
        tab.classList.toggle("active",tab.dataset.kind === kind); tab.setAttribute("aria-pressed",String(tab.dataset.kind === kind));
      }
      paint({feed:false});
    } else if (action === "qq-monitor-refresh") await refresh();
    else if (action === "qq-monitor-older" && hasOlder && before) await refresh({older:true});
    else if (action === "qq-monitor-latest") await refresh({initial:true});
    else if (action === "qq-monitor-logs") { syncShell(); await navigate("logs"); }
    else if (action === "qq-monitor-settings") {
      const item = current(); if (item && target.dataset.scope === selected) scopeModal(item);
    }
    return true;
  }
  function handleInput(target) {
    if (target.id !== "qq-monitor-search" || !here()) return false;
    search = target.value; paint({feed:false}); return true;
  }
  async function handleChange(target) {
    if (target.id !== "qq-monitor-auto" || !here()) return false;
    auto = Boolean(target.checked);
    if (!auto) { invalidate(); paint({feed:false}); }
    else await refresh();
    schedule(); return true;
  }
  document.addEventListener("visibilitychange", () => {
    if (!here()) return;
    if (document.hidden) { invalidate(); paint({feed:false}); }
    else { paint({feed:false}); if (auto) void refresh(); }
  });
  return {render, afterRender, close, handleAction, handleInput, handleChange};
})();
