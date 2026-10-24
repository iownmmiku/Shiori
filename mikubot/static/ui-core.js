"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
function syncToastLayer() {
  const region=$("#toast-region"),dialog=$("#modal");
  if (!region || !dialog) return;
  // A native modal's top layer sits above every document z-index. Move the
  // live region into that layer; increasing z-index cannot fix this case.
  const host=dialog.open ? dialog : document.body;
  if (region.parentNode !== host) {
    if (dialog.open) host.prepend(region); else host.append(region);
  }
}

function toast(message,error = false) {
  syncToastLayer();
  const item = document.createElement("div"); item.className = `toast${error ? " error" : ""}`;
  item.innerHTML = icon(error ? "info" : "check") + `<span>${e(message)}</span>`;
  $("#toast-region").append(item); setTimeout(() => item.remove(),error ? 8000 : 4200);
}

function heading(eyebrow,title,description,actions = "") { return `<div class="page-heading"><div><div class="eyebrow">${e(eyebrow)}</div><h1>${e(title)}</h1><p>${e(description)}</p></div><div class="heading-actions">${actions}</div></div>`; }

function button(text,action,style = "",symbol = "",attrs = "") { return `<button type="button" class="btn ${style}" data-action="${e(action)}" ${attrs}>${symbol ? icon(symbol) : ""}${e(text)}</button>`; }

function empty(symbol,title,description,action = "",actionText = "") { return `<div class="empty-state"><div class="empty-symbol">${icon(symbol)}</div><h3>${e(title)}</h3><p>${e(description)}</p>${action ? button(actionText,action,"primary","plus") : ""}</div>`; }

function tag(text,type = "") { return `<span class="tag ${type}">${e(text)}</span>`; }

function notice(text,type = "",action = "",actionText = "") { return `<div class="notice ${type}">${icon("info")}<p>${e(text)}</p>${action ? `<button data-action="${e(action)}">${e(actionText)}</button>` : ""}</div>`; }

function field(name,title,value = "",type = "text",hint = "",extra = "") { const id = `field-${++fieldId}`; return `<div class="field ${extra.includes("data-full") ? "full" : ""}"><label for="${id}">${e(title)}</label><input id="${id}" name="${e(name)}" type="${e(type)}" value="${type === "password" ? "" : e(value)}" ${hint ? `aria-describedby="${id}-help"` : ""} ${extra}>${hint ? `<small id="${id}-help">${e(hint)}</small>` : ""}</div>`; }

function textarea(name,title,value = "",hint = "",extra = "") { const id = `field-${++fieldId}`; return `<div class="field full"><label for="${id}">${e(title)}</label><textarea id="${id}" name="${e(name)}" ${hint ? `aria-describedby="${id}-help"` : ""} ${extra}>${e(value)}</textarea>${hint ? `<small id="${id}-help">${e(hint)}</small>` : ""}</div>`; }

function select(name,title,options,value,extra = "") { const id = `field-${++fieldId}`; return `<div class="field ${extra.includes("data-full") ? "full" : ""}"><label for="${id}">${e(title)}</label><select id="${id}" name="${e(name)}" ${extra}>${options.map(item => { const [key,text] = Array.isArray(item) ? item : [item,label(item)]; return `<option value="${e(key)}" ${String(value) === String(key) ? "selected" : ""}>${e(text)}</option>`; }).join("")}</select></div>`; }

function toggle(name,title,hint,checked) { return `<div class="toggle-field"><div class="toggle-copy"><strong>${e(title)}</strong><small>${e(hint)}</small></div><label class="switch"><input type="checkbox" name="${e(name)}" aria-label="${e(title)}" ${checked ? "checked" : ""}><span></span></label></div>`; }

function scopesField(value = state.scope) { return select("scope_id","生效会话",state.scopes.map(item => [item.id,item.name]),value,"data-full"); }

function getForm(form) { const values = Object.fromEntries(new FormData(form)); $$('input[type="checkbox"]',form).forEach(item => { values[item.name] = item.checked; }); return values; }

function listToolbar(placeholder,options) { return `<div class="toolbar"><label class="search-field">${icon("search")}<input type="search" id="record-search" value="${e(state.filters.q || "")}" placeholder="${e(placeholder)}" aria-label="搜索"></label><div class="filter-tabs">${options.map(([key,text]) => `<button class="filter-tab ${ (state.filters.status || "") === key ? "active" : ""}" data-action="filter" data-status="${e(key)}">${e(text)}</button>`).join("")}</div></div>`; }

function openModal(title,description,body,kind = "",record = null,submitLabel = "保存") {
  const dialog = $("#modal"); dialog.classList.remove("modal-wide");
  state.modalRecord = record; state.modalKind = kind;
  $("#modal-content").innerHTML = `<div class="modal-head"><div><h2>${e(title)}</h2>${description ? `<p>${e(description)}</p>` : ""}</div><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></div>${kind ? `<form data-form="${e(kind)}">` : ""}<div class="modal-body">${body}<div id="modal-error" hidden class="modal-error" aria-live="polite"></div></div><div class="modal-footer">${button(kind ? "取消" : "关闭","close-modal","ghost")}${kind ? `<button class="btn primary" type="submit">${e(submitLabel)}</button>` : ""}</div>${kind ? "</form>" : ""}`;
  if (!dialog.open) dialog.showModal();
  syncToastLayer();
  setTimeout(() => $("input:not([type=checkbox]),textarea,select",dialog)?.focus(),30);
}

function modalError(message) { const area = $("#modal-error"); if (area) { area.textContent = message; area.hidden = false; } else toast(message,true); }

function closeModal() { $("#modal").close(); syncToastLayer(); state.modalRecord = null; state.modalKind = null; }

async function confirmAction(title,description,perform,labelText = "确认") {
  openModal(title,"",`<p class="record-text">${e(description)}</p>`,"confirm",null,labelText);
  state.confirm = perform;
}

// Advanced model parameters use a range+number pair. Ranges carry no form name,
// only data-sync-target pointing at the number input; numbers carry
// data-sync-source pointing back at the range. The delegated "input" listener in
// app.js calls this, so dragging a slider also updates the submitted value and
// typing a number moves the slider. No inline handlers and no CSP relaxation.
function syncRangePair(target) {
  if (!target || !target.dataset) return;
  const targetId = target.dataset.syncTarget;
  if (targetId) {
    const number = document.getElementById(targetId);
    if (number && number.value !== target.value) number.value = target.value;
    return;
  }
  const sourceId = target.dataset.syncSource;
  if (!sourceId) return;
  const range = document.getElementById(sourceId);
  if (range && range.value !== target.value) range.value = target.value;
}

// One validation rule for the shared custom-params textarea: empty means "no
// extra parameters"; anything that is not a plain JSON object is rejected with
// the same Chinese message in both the settings form and the catalog modal.
function parseCustomParams(text) {
  const raw = String(text ?? "").trim();
  if (!raw) return {};
  let value;
  try {
    value = JSON.parse(raw);
  } catch {
    throw new Error("自定义参数必须是有效的 JSON 对象。");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("自定义参数必须是有效的 JSON 对象。");
  if (Object.values(value).some(item => typeof item === "number" && !Number.isFinite(item))) throw new Error("自定义参数中的数字必须是有限数字。");
  return value;
}
