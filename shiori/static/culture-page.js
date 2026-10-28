"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
async function renderCultureBase() {
  const key=JSON.stringify([state.learningAll,state.scope,state.filters.q,state.filters.status]);
  if(state.culturePage?.key!==key)state.culturePage={key,offset:0};
  const page=await api(`/api/jargon?${query({scope_id:state.learningAll?undefined:state.scope,q:state.filters.q,status:state.filters.status,limit:100,offset:state.culturePage.offset})}`);
  state.records=rows(page);state.cultureMeta=page;
  return heading("OUR SHARED LANGUAGE","你们懂的，它也懂。","收集黑话、别名与群梗，让它慢慢融入你们的聊天。",button("教一个新词","new-jargon","primary","plus")) + listToolbar("搜索词语、含义或话题",[["","全部"],["approved","已确认"],["pending","待确认"],["disabled","已停用"],["rejected","已拒绝"]]) + notice("同一个词可以有多个意思。只有已确认的词条参与理解；是否主动使用可以单独设置。") + (state.records.length ? `<div class="record-grid">${state.records.map(item => `<article class="record-card"><div class="record-card-head"><h3>${e(item.term)}</h3>${tag(label(item.status),item.status === "pending" ? "gold" : item.status === "approved" ? "" : "gray")}</div><p class="record-text">${e(item.meaning)}</p>${item.example ? `<div class="persona-preview">${e(item.example)}</div>` : ""}<div class="record-meta">${item.topic ? `<span>话题 · ${e(item.topic)}</span>` : ""}<span>${item.use_allowed ? "允许主动使用" : "只用于理解"}</span><span>v${e(item.version || 1)}</span></div><div class="record-actions">${item.status === "pending" ? button("确认词义","approve-jargon","primary small","check",`data-id="${e(item.id)}"`) + button("拒绝","reject-jargon","ghost small","",`data-id="${e(item.id)}"`) : ""}${button("编辑","edit-jargon","small","edit",`data-id="${e(item.id)}"`)}${item.status === "approved" || item.status === "disabled" ? button(item.status === "disabled" ? "启用" : "停用","toggle-jargon","ghost small","",`data-id="${e(item.id)}"`) : ""}<button class="icon-button push-right" data-action="delete-jargon" data-id="${e(item.id)}" aria-label="删除词条">${icon("trash")}</button></div></article>`).join("")}</div>` : empty("culture","每个群都有自己的语言","从一个缩写、昵称或梗开始。填入词义与例句，让它在正确的场合听懂你们。","new-jargon","教会它第一个词"));
}

function jargonModal(item = null) {
  openModal(item ? "调整一个群词" : "教它一个新词","可以听懂，不必每次都说。词义与主动使用分别控制。",`<div class="form-grid">${scopesField(item?.scope_id)}${field("term","词语 / 黑话",item?.term || "","text","支持缩写、别名、短语与群梗。","required maxlength=\"100\"")}${field("topic","适用话题",item?.topic || "","text","例如游戏、聚餐、工作。")}${textarea("meaning","它在这里的意思",item?.meaning || "","同一个词可以添加多个解释。","required maxlength=\"4000\"")}${textarea("example","使用例句（可选）",item?.example || "","用一条真实语境，帮助它分清含义。")}${select("status","词义状态",["approved","pending","disabled","rejected"],item?.status || "approved","data-full")}</div>${toggle("use_allowed","允许主动使用","还需在会话设置中开启“主动使用群词”。",item?.use_allowed)}`,"jargon",item);
}

async function renderCultureTerms() {
  let html = await renderCultureBase();
  const page=state.cultureMeta;
  if(page?.total)html+=`<div class="record-actions"><span>共 ${e(page.total)} 条 · 第 ${Math.floor(page.offset/100)+1} 页</span>${button("上一页","culture-page","small","",`data-offset="${Math.max(0,page.offset-100)}" ${page.offset?"":"disabled"}`)}${button("下一页","culture-page","small","",`data-offset="${page.next_offset}" ${page.has_more?"":"disabled"}`)}</div>`;
  html = html.replace(button("教一个新词","new-jargon","primary","plus"),button("分析最近聊天","analyze-jargon","","spark") + button("教一个新词","new-jargon","primary","plus"));
  const current = scope();
  const switches = `<section class="culture-controls"><div><div class="eyebrow">LEARN · UNDERSTAND · EXPRESS</div><h3>听懂与说出来，由你分别决定。</h3><p>有明确释义和可靠证据的内容可自动确认，其余待审核。主动用梗还需要词条允许。</p></div><form data-form="culture-switches" class="culture-switch-form">${toggle("dictionary_learn","自动发现新词","分析重复出现的陌生说法。",current.dictionary_learn ?? false)}${toggle("dictionary_understand","听懂已确认词义","根据隔离规则召回已确认词义。",current.dictionary_understand ?? true)}${toggle("dictionary_use","主动使用群词","在合适的场合适量接梗。",current.dictionary_use ?? false)}<button type="submit" class="btn small">保存这组规则</button></form></section>`;
  const marker = '<div class="toolbar">'; html = html.replace(marker,switches + marker);
  for (const record of state.records) {
    const evidence = record.evidence;
    if (!evidence && !(record.source_ids || []).length) continue;
    const editButton = button("编辑","edit-jargon","small","edit",`data-id="${e(record.id)}"`);
    html = html.replace(editButton,button("学习证据","jargon-evidence","ghost small","",`data-id="${e(record.id)}"`) + editButton);
  }
  return html;
}

function cultureTabs() { return `<div class="culture-mode-tabs"><button class="${state.cultureMode !== "expressions" ? "active" : ""}" data-action="culture-mode" data-mode="terms">${icon("culture")}黑话与群梗<small>听懂你们的语言</small></button><button class="${state.cultureMode === "expressions" ? "active" : ""}" data-action="culture-mode" data-mode="expressions">${icon("chat")}表达与语气<small>学会在合适场合怎样说</small></button></div>`; }

async function renderCulture() {
  if (state.cultureMode !== "expressions") {
    const html = await renderCultureTerms(),dashboard=await window.ShioriLearning?.panel("language");
    return html.replace('<section class="culture-controls">',cultureTabs()+(dashboard||"") + '<section class="culture-controls">');
  }
  state.records = rows(await api(`/api/expressions?${query({scope_id:state.scope,status:state.filters.status})}`));
  if (state.filters.q) state.records = state.records.filter(item => `${item.term} ${item.meaning} ${item.topic || ""}`.toLowerCase().includes(state.filters.q.toLowerCase()));
  const current = scope();
  return heading("A WAY WITH WORDS","学会表达，也保留自己的性格。","黑话负责词义，表达学习负责场景、语气与句式。人格仍然决定怎么说。",button("分析表达样例","analyze-expressions","","spark") + button("教一种表达","new-expression","primary","plus")) + cultureTabs() + `<section class="culture-controls"><div><div class="eyebrow">LEARN · REVIEW · EXPRESS</div><h3>适合安慰时，别只会接梗。</h3><p>学习候选不会自动生效。先看来源和适用场景，确认后再允许采用。</p></div><form data-form="expression-switches" class="culture-switch-form">${toggle("expression_learn","自动学习表达","从近期真实群聊发现表达候选。",current.expression_learn ?? false)}${toggle("expression_use","采用已确认表达","结合人格与当前语境，适量使用。",current.expression_use ?? false)}<button type="submit" class="btn small">保存表达规则</button></form></section>` + listToolbar("搜索场景、句式或语气",[["","全部"],["pending","待确认"],["approved","已确认"],["disabled","已停用"],["rejected","未接纳"]]) + notice("表达分析使用已配置的学习模型并计入预算。它学习可复用的表达方式，不把他人的身份、关系或经历当作自己的人设。") + (state.records.length ? `<div class="record-grid">${state.records.map(item => `<article class="record-card"><div class="record-card-head"><h3>${e(item.term)}</h3>${tag(label(item.status),item.status === "pending" ? "gold" : item.status === "approved" ? "" : "gray")}</div><p class="record-text">${e(item.meaning)}</p>${item.topic ? `<div class="record-meta"><span>适用场景 · ${e(item.topic)}</span></div>` : ""}${item.example ? `<div class="persona-preview">${e(item.example)}</div>` : ""}<div class="record-meta"><span>${item.use_allowed ? "允许采用" : "暂不主动采用"}</span><span>${(item.source_ids || []).length} 条来源</span></div><div class="record-actions">${item.status === "pending" ? button("确认表达","approve-jargon","primary small","check",`data-id="${e(item.id)}"`) + button("不采用","reject-jargon","ghost small","",`data-id="${e(item.id)}"`) : ""}${button("来源样例","jargon-evidence","ghost small","",`data-id="${e(item.id)}"`)}${button("编辑","edit-expression","small","edit",`data-id="${e(item.id)}"`)}${["approved","disabled"].includes(item.status) ? button(item.status === "disabled" ? "启用" : "停用","toggle-jargon","ghost small","",`data-id="${e(item.id)}"`) : ""}<button class="icon-button push-right" data-action="delete-jargon" data-id="${e(item.id)}" aria-label="删除表达">${icon("trash")}</button></div></article>`).join("")}</div>` : empty("chat","表达的默契，从真实聊天里来","开启表达学习后分析近期聊天，或教它一种适用于认可、安慰、邀请等场合的表达。","new-expression","教一种表达"));
}

function expressionModal(item = null) {
  jargonModal(item);
  $("#modal .modal-head h2").textContent = item ? "打磨一种表达" : "教它一种表达";
  $("#modal .modal-head p").textContent = "记录适用场景与句式，确认后再决定是否主动采用。";
  for (const [name,title] of Object.entries({term:"表达短语 / 句式",meaning:"适用情境说明",topic:"关联话题",example:"原始示例（可选）"})) {
    const input = $(`[name="${name}"]`,$("#modal")); input.closest(".field").querySelector("label").textContent = title;
  }
  const hints = {term:"2—30 字的自然短语，例如“稍等我捋一捋”。",meaning:"说明什么场合适合说这句话，2—240 字。",topic:"例如安慰、认可、邀请、讨论整理。",example:"保留真实语境，避免模仿特定成员身份。"};
  for (const [name,hint] of Object.entries(hints)) { const helper = $(`[name="${name}"]`,$("#modal")).closest(".field").querySelector("small"); if (helper) helper.textContent = hint; }
  $('[name="status"]',$("#modal")).closest(".field").querySelector("label").textContent = "审核状态";
  const useToggle = $('[name="use_allowed"]',$("#modal")).closest(".toggle-field");
  $(".toggle-copy strong",useToggle).textContent = "允许采用这条表达";
  $(".toggle-copy small",useToggle).textContent = "还需在当前会话开启“采用已确认表达”。";
  $('[name="term"]',$("#modal")).minLength = 2;
  $('[name="term"]',$("#modal")).maxLength = 30;
  $('[name="meaning"]',$("#modal")).minLength = 2;
  $('[name="meaning"]',$("#modal")).maxLength = 240;
  $('[name="status"]',$("#modal")).value = item?.status || "pending";
  $("#modal form").insertAdjacentHTML("beforeend",'<input type="hidden" name="entry_type" value="expression">');
}
