"use strict";

const {scopeModal,identitySection,timeAwarenessSection,outputSection,plannerSection,explorationSection,renderPersonaKnowledge,safeKnowledgeURL,explorePersona,renderGroups} = window.ShioriFeatures;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
const e = escapeHTML;
const paths = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  chat:'<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M8 10h8M8 14h5"/>',
  memory:'<path d="M12 7v14M3 4h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v15h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3V4Z"/>',
  culture:'<path d="m4 7 3-3 4 4-3 3-4-4Zm9 6 3-3 4 4-3 3-4-4ZM8 11l5 2M11 8l5 2M3 21l4-1-3-3-1 4ZM18 3l3 3M21 3l-3 3"/>',
  tasks:'<rect x="5" y="4" width="14" height="17" rx="2"/><rect x="9" y="2" width="6" height="4" rx="1"/><path d="m8 13 2 2 5-5"/>',
  tools:'<path d="M8 3v4M16 3v4M6 7h12v4a6 6 0 0 1-12 0V7ZM12 17v4M9 21h6"/>',
  persona:'<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2M9 8h.01M15 8h.01"/>',
  groups:'<circle cx="9" cy="7" r="3"/><path d="M2 21v-2a7 7 0 0 1 14 0v2M16 4a3 3 0 0 1 0 6M18 14a6 6 0 0 1 4 5v2"/>',
  settings:'<path d="m9 3-.5 3-2.5 1-2.5-1L2 9l2 2v3l-2 2 1.5 3L6 18l2.5 1L9 22h6l.5-3 2.5-1 2.5 1 1.5-3-2-2v-3l2-2-1.5-3L18 7l-2.5-1L15 3H9Z"/><circle cx="12" cy="12.5" r="3"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  send:'<path d="m22 2-7 20-4-9L2 9 22 2ZM22 2 11 13"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  download:'<path d="M12 3v13m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  edit:'<path d="m16 3 5 5-12 12-6 1 1-6L16 3ZM14 5l5 5"/>',
  spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-5"/>',
  link:'<path d="m10 13 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 8l2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(1 0) scale(.92)"/>',
  globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
  file:'<path d="M14 2H5v20h14V7l-5-5ZM14 2v6h5M8 12h8M8 16h6"/>',
  models:'<path d="M12 3 3 7l9 4 9-4-9-4ZM3 12l9 4 9-4M3 17l9 4 9-4"/>',
  logs:'<path d="M5 3h11l3 3v15H5V3ZM8 8h8M8 12h8M8 16h5"/>',
  statistics:'<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  plugins:'<path d="M9 3v4H5a2 2 0 0 0-2 2v4h4a2 2 0 1 1 0 4H3v4a2 2 0 0 0 2 2h4v-4a2 2 0 1 1 4 0v4h4a2 2 0 0 0 2-2v-4h-4a2 2 0 1 1 0-4h4V9a2 2 0 0 0-2-2h-4V3H9Z"/>',
  diagnostics:'<path d="M3 12h4l2-5 3 10 2-5h7"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.spark}</svg>`;
const pageNames = {overview:"运行概览",chat:"聊天",models:"模型供应商",memory:"记忆档案",culture:"群语言与表达",knowledge:"知识与检索",personaKnowledge:"人格知识",tasks:"任务与提醒",tools:"MCP",persona:"机器人配置",groups:"群与会话",logs:"运行日志",statistics:"数据统计",plugins:"插件",diagnostics:"自检与诊断",settings:"系统设置",jobs:"后台任务",help:"使用指南"};
// Keep old model links working without restoring a second configuration page.
const sidebarPageNames = Object.fromEntries(Object.entries(pageNames).filter(([page]) => page !== "models"));
// Retain browser preferences from previous product names on this origin.
for (const [storage, names] of [[localStorage, ["page", "scope"]], [sessionStorage, ["wizard-skipped"]]]) {
  for (const name of names) {
    const key = "shiori-" + name;
    if (storage.getItem(key) === null) {
      const previous = storage.getItem("mikubot-" + name) ?? storage.getItem("qunyu-" + name);
      if (previous !== null) storage.setItem(key, previous);
    }
  }
}
const state = {page:localStorage.getItem("shiori-page") || "chat",scope:localStorage.getItem("shiori-scope") || "web:default",boot:null,settings:{},scopes:[],personas:[],records:[],messages:[],filters:{},images:[],draft:"",sending:false,controller:null,renderId:0,modalRecord:null,modalKind:null,authenticated:false};
if (!pageNames[state.page]) state.page = "overview";
const labels = {fact:"事实",preference:"偏好",event:"共同经历",note:"笔记",active:"有效",archived:"已归档",superseded:"已替代",deleted:"已删除",pending:"待处理",approved:"已确认",rejected:"已拒绝",disabled:"已停用",running:"执行中",completed:"已完成",cancelled:"已取消",failed:"失败",private:"仅本人",group:"会话共享",todo:"待办",reminder:"提醒",none:"不重复",daily:"每天",weekly:"每周",balanced:"适时回应",quiet:"仅被提及",active_activity:"积极接话",delivered:"已送达",lexical:"关键词检索",hybrid:"混合检索",semantic:"语义检索",basic:"基础检索"};
const label = key => labels[key] || key || "—";
const fmt = (value, full = false) => { if (!value) return "未设置"; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN",full ? {year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); };
const number = value => Number(value || 0).toLocaleString("zh-CN");
const rows = value => Array.isArray(value) ? value : value?.items || value?.messages || value?.memories || value?.jargon || value?.tasks || value?.events || value?.usage || value?.jobs || value?.admins || value?.candidates || [];
const scope = () => state.scopes.find(item => item.id === state.scope) || {id:state.scope,name:"本地聊天"};
const scopeName = id => state.scopes.find(item => item.id === id)?.name || id || "本地聊天";
const personaName = id => state.personas.find(item => item.id === id)?.name || (id ? "人格已删除" : "未使用人格");
function configuredModel(route = "chat") {
  const id = state.settings.routing?.[route];
  if (!id) return state.settings.model || {};
  const model = (state.settings.providers || []).find(item => item.id === id);
  return model?.enabled !== false ? model || {} : {};
}
const query = params => new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== "" && value !== null)).toString();

async function api(path, options = {}) {
  const {method = "GET",body,signal,raw = false} = options;
  const headers = {};
  if (body && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path,{method,headers,credentials:"same-origin",signal,body:body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    let result; try { result = await response.json(); } catch { result = {}; }
    let detail = result.detail || result.error || `请求失败（${response.status}）`;
    if (typeof detail !== "string") detail = JSON.stringify(detail);
    if (response.status === 401) { state.authenticated = false; showAuth(); }
    const error = new Error(detail);
    if (typeof result.code === "string" && /^[a-z_]{1,64}$/.test(result.code)) error.code = result.code;
    throw error;
  }
  if (raw) return response;
  if (response.status === 204) return {};
  return response.json();
}








let fieldId = 0;








function renderNavigation() {
  const layout = window.ShioriSidebar;
  // The sidebar lives in its own script. If that script ever fails to load, fall
  // back to a flat list instead of throwing and leaving the shell blank.
  $("#navigation").innerHTML = layout?.render
    ? layout.render({ pageNames: sidebarPageNames, current: state.page, icon, esc: e })
    : `<div class="primary-navigation">${Object.keys(sidebarPageNames).filter(key => key !== "help").map(key => `<button type="button" class="nav-item ${key === state.page ? "active" : ""}" data-page="${e(key)}">${icon(key)}<span>${e(sidebarPageNames[key])}</span></button>`).join("")}</div>`;
  document.body.classList.toggle("chat-page",state.page === "chat");
  $("#page-breadcrumb").textContent = pageNames[state.page];
  $("#scope-icon").innerHTML = icon("groups");
  $("#refresh-button").innerHTML = icon("refresh");
  $("#global-scope").innerHTML = state.scopes.map(item => `<option value="${e(item.id)}" ${item.id === state.scope ? "selected" : ""}>${e(item.name)}</option>`).join("");
  const modelReady = !state.boot?.setup_required;
  $("#sidebar-status-dot").className = `status-dot ${state.settings.paused ? "warning" : modelReady ? "online" : ""}`;
  $("#sidebar-status-text").textContent = state.settings.paused ? "已暂停自动回复" : modelReady ? "本地服务已就绪" : "等待配置模型";
  $("#sidebar-status-sub").textContent = modelReady ? (configuredModel().model || "你的数据，留在你的电脑") : "连接模型即可开始聊天";
  $("#version-label").textContent = state.boot?.version || "0.1";
  $("#workspace-version").textContent = state.boot?.version || "0.2.0";
  if ($("#header-version")) $("#header-version").textContent = "v" + (state.boot?.version || "0.2.0");
  const portWarning = $("#port-warning"), listener = state.boot?.listener;
  if (portWarning) {
    portWarning.hidden = !listener?.port_changed;
    portWarning.textContent = listener?.port_changed ? listener.port_warning : "";
  }
}
// Structural edits replace only the management panel, keeping its form, footer
// and dialog stable. Page-group ids change on a move, so use the page identity.
function sidebarFocusKey(element) {
  if (!element || !element.dataset) return "";
  const { sidebarField = "", targetPage = "", id = "", action = "", view = "", icon = "" } = element.dataset;
  if (!sidebarField && !targetPage && !action) return "";
  return [sidebarField, targetPage, targetPage ? "" : id, action, view, icon, element.tagName].join("|");
}
function findSidebarControl(key) {
  if (!key) return null;
  for (const element of document.querySelectorAll('#modal [data-sidebar-field],#modal [data-target-page],#modal [data-action^="sidebar-"]')) {
    if (sidebarFocusKey(element) === key) return element;
  }
  return null;
}
function saveSidebarTitles(form) {
  for (const input of $$('[data-sidebar-field="title"]',form)) {
    window.ShioriSidebar?.handleChange?.(input,sidebarContext());
  }
}
function sidebarContext() {
  return { pageNames: sidebarPageNames, icon, esc: e, notify: toast, rerender: () => { renderNavigation(); leaveHiddenPage(); },
           rerenderManage: () => {
             const dialog = $("#modal"), panel = $("#modal .sidebar-manage");
             if (state.modalKind !== "sidebar-manage" || !dialog?.open || !panel) return;
             const active = document.activeElement, key = sidebarFocusKey(active);
             const selection = [active?.selectionStart,active?.selectionEnd,active?.selectionDirection];
             const inPanel = panel.contains(active);
             const scroll = ["#modal","#modal .modal-body","#modal .sidebar-manage-pages","#modal .sidebar-manage-list"]
               .map(selector => [selector,$(selector)?.scrollTop]);
             panel.outerHTML = window.ShioriSidebar.manageBody({pageNames:sidebarPageNames,icon,esc:e});
             const next = findSidebarControl(key);
             // Choosing an icon closes the replacement <details>. Its hidden
             // palette buttons cannot take focus, so return to its summary.
             const picker = next?.closest?.(".sidebar-icon-picker");
             const visible = picker && !picker.open ? picker.querySelector("summary") : next;
             const focus = visible && !visible.disabled ? visible : inPanel ? $("#modal .manage-tab.active") : null;
             focus?.focus({preventScroll:true});
             if (focus === next && Number.isInteger(selection[0])) {
               try { next.setSelectionRange?.(...selection); } catch {}
             }
             for (const [selector,top] of scroll) if (top !== undefined && $(selector)) $(selector).scrollTop = top;
           },
           openManage: openSidebarManage };
}

// Hiding the page you are currently on (or its whole group) would leave no
// active sidebar entry, so fall back to chat and tell the user how to return.
function leaveHiddenPage() {
  window.ShioriSidebar?.prepare?.(sidebarPageNames);
  if (state.page !== "chat" && window.ShioriSidebar && !window.ShioriSidebar.isPageVisible(state.page)) {
    state.page = "chat"; localStorage.setItem("shiori-page","chat");
    renderNavigation();
    toast("该页面已隐藏，已回到聊天；可在「管理侧边栏」中恢复。");
    renderPage();
  }
}

// The sidebar is the only entry point to every page, so the management dialog
// itself can never be hidden or removed.
function openSidebarManage() {
  openModal("管理侧边栏","调整分类、顺序与显示范围。隐藏的页面不会出现在侧边栏，但功能与数据都保留，可随时恢复。",
            window.ShioriSidebar.manageBody({ pageNames:sidebarPageNames, icon, esc: e }),"sidebar-manage",null,"完成");
}

async function bootstrap(showWizard = false) {
  const boot = await api("/api/bootstrap");
  state.boot = boot; state.settings = boot.settings || {}; state.scopes = boot.scopes || []; state.personas = boot.personas || [];
  if (!state.scopes.find(item => item.id === state.scope)) state.scope = state.scopes[0]?.id || "web:default";
  if(state.page === "models"){state.page="persona";state.botTab="models";localStorage.setItem("shiori-page","persona");}
  // The remembered page may have been hidden since the last visit; never strand
  // the user on a page with no sidebar entry.
  window.ShioriSidebar?.prepare?.(sidebarPageNames);
  if (window.ShioriSidebar && !window.ShioriSidebar.isPageVisible(state.page)) { state.page = "chat"; localStorage.setItem("shiori-page","chat"); }  state.authenticated = true; renderNavigation(); await renderPage();
  if (showWizard && boot.setup_required && !sessionStorage.getItem("shiori-wizard-skipped")) showWizardModal();
}
async function navigate(page) {
  if (!pageNames[page]) return;
  if(page === "models"){page="persona";state.botTab="models";}
  // A hidden page has no sidebar entry; keep navigation truthful by refusing it
  // and telling the user how to bring it back.
  window.ShioriSidebar?.prepare?.(sidebarPageNames);
  if (window.ShioriSidebar && !window.ShioriSidebar.isPageVisible(page)) { toast("该页面已在侧边栏隐藏，可在「管理侧边栏 → 管理页面」中恢复。",true); return; }
  if (state.page === "chat") state.draft = $("#chat-text")?.value || state.draft;
  state.page = page; state.filters = {}; localStorage.setItem("shiori-page",page);
  $("#sidebar").classList.remove("open"); $("#sidebar-scrim").classList.remove("open");
  renderNavigation(); await renderPage(); window.scrollTo(0,0);
}
async function renderPage() {
  const id = ++state.renderId;
  const main = $("#content");
  main.innerHTML = '<div class="loading-page"><span class="spinner"></span><p>正在读取本地数据…</p></div>';
  try {
    const html = await ({overview:window.ShioriExperience.renderOverview,chat:window.ShioriExperience.renderChat,models:window.ShioriPages.renderModels,logs:window.ShioriPages.renderLogs,statistics:window.ShioriStatistics?.render,plugins:window.ShioriPlugins?.render,memory:renderMemory,culture:renderCulture,knowledge:renderKnowledge,personaKnowledge:renderPersonaKnowledge,tasks:renderTasks,tools:renderTools,persona:window.ShioriExperience.renderPersonas,groups:renderGroups,diagnostics:renderDiagnostics,settings:window.ShioriExperience.renderSettings,jobs:window.ShioriExperience.renderJobs,help:window.ShioriExperience.renderHelp}[state.page])();
    if (id !== state.renderId) return;
    main.innerHTML = `<div class="animate-in">${html}</div>`;
    if (state.page === "chat") { const feed = $("#chat-feed"); feed.scrollTop = feed.scrollHeight; renderImagePreviews(); }
    if (state.page === "settings" || state.page === "persona" && state.botTab === "qq") updateAdapterFields();
    window.ShioriExperience.afterRender();
    window.ShioriPages.afterRender();
    window.ShioriStatistics?.afterRender();
    window.ShioriUpdates?.afterRender();
    window.ShioriFeatures.afterRender?.();
  } catch (error) { if (id === state.renderId) main.innerHTML = `${heading("LOCAL WORKSPACE",pageNames[state.page],"数据暂时没有加载成功。请确认本地服务仍在运行。") }<div class="inline-error">${e(error.message)}</div><div class="section-actions">${button("重新加载","refresh","primary","refresh")}</div>`; }
}



function imageURL(value) {
  const url = typeof value === "string" ? value : value?.url || value?.image_url?.url || "";
  return /^(data:image\/(?:png|jpeg|webp|gif);base64,|https?:\/\/|\/(?!\/))/i.test(url) ? url : "";
}









function iconInline(name) { return `<span class="inline-icon" style="display:inline-block;vertical-align:middle;margin-right:4px;width:11px;height:11px">${icon(name)}</span>`; }














function scopeModalBase(item = null) {
  openModal(item ? "调整会话设置" : "添加一个会话空间","默认隔离记忆与词典；加入同名共享空间后，仅共享明确允许的群可见记忆。QQ 会话仍需白名单。",`<div class="form-grid">${!item ? `<input type="hidden" name="kind" value="web"><details class="details field full"><summary>高级：手动填写 QQ 会话 ID</summary>${field("id","会话 ID（可选）","","text","本地对话无需填写。QQ 会话通常从实际收到的消息自动创建。","data-full placeholder=\"qq:group:123456789\"")}</details>` : ""}${field("name","会话名称",item?.name || "","text","","required data-full maxlength=\"100\"")}${select("persona_id","使用人格",state.personas.map(p => [p.id,p.name]),item?.persona_id || "default")}${select("activity","回应方式",[["quiet","仅被提及"],["balanced","适时回应"],["active","积极接话"]],item?.activity || "balanced")}${field("quiet_start","安静时段开始",item?.quiet_start || "","time")}${field("quiet_end","安静时段结束",item?.quiet_end || "","time")}</div>${toggle("enabled","启用这个会话","关闭后停止自动回复。",item?.enabled ?? true)}${toggle("auto_memory","自动记住重要信息","从对话中提取事实、偏好与经历。",item?.auto_memory ?? true)}${toggle("dictionary_understand","理解群词典","在合适语境召回已确认词义。",item?.dictionary_understand ?? true)}${toggle("dictionary_use","主动使用群词","只使用同时允许主动使用的已确认词条。",item?.dictionary_use ?? false)}${toggle("allow_tools","允许使用工具","包括记事、提醒、计算和已启用的检索工具。",item?.allow_tools ?? true)}`,"scope",item);
}
function showWizardModal() {
  window.ShioriQuickStart.open(1);
}
function showAuth() {
  window.ShioriPages?.close();
  window.ShioriExperience?.close();
  window.ShioriAuth.requireLogin();
}


// 0.2: reviewable memories, scoped knowledge, culture learning and group turn-taking.




function searchResultCard(item) {
  const retrieval = item.retrieval || {}, reasons = retrieval.reasons || [], excerpt = retrieval.excerpt || item.content;
  const chunkIndex = item.chunk_index ?? retrieval.citation?.chunk_index ?? retrieval.chunk_index;
  return `<article class="record-card search-result"><div class="record-card-head"><div class="inline-stack">${tag(label(item.kind))}${item.subject ? tag(item.subject,"gray") : ""}</div><span class="search-score">${Number.isFinite(Number(retrieval.score)) ? Number(retrieval.score).toFixed(3) : "—"}<small>召回分数</small></span></div><p class="record-text">${e(excerpt)}</p><div class="retrieval-reasons">${reasons.map(reason => `<span>${icon("check")}${e(reason)}</span>`).join("")}</div><div class="record-meta">${item.filename || item.source_file ? `<span>${e(item.filename || item.source_file)}</span>` : ""}${chunkIndex !== undefined ? `<span>片段 ${Number(chunkIndex) + 1}</span>` : ""}<span>${e(label(item.visibility))}</span><span>${e(scopeName(item.scope_id))}</span></div><div class="record-actions">${button("查看记忆与来源","search-memory-detail","ghost small","memory",`data-id="${e(item.id)}"`)}</div></article>`;
}




async function showProfile(offset = 0) {
  const userId = $('[data-form="knowledge-search"] [name="user_id"]')?.value.trim() || state.knowledgeUser || "owner", profile = await api(`/api/profile?${query({scope_id:state.scope,user_id:userId,limit:50,offset})}`);
  const sections = [["facts","事实"],["preferences","偏好"],["recent_events","共同经历"]];
  const content = sections.map(([key,title]) => `<h3 style="margin:16px 0 10px">${title}</h3><div class="source-list">${rows(profile[key]).length ? rows(profile[key]).map(item => `<div class="source-item"><p>${e(item.content || item)}</p><small>v${e(item.version || 1)} · ${(item.source_ids || []).length} 条来源</small></div>`).join("") : '<p class="help-caption">还没有对应的有效记忆。</p>'}</div>`).join("");
  openModal("从记忆里，认识你",`${scopeName(state.scope)} · ${userId} · 仅根据当前可见的有效记忆整理`,content + `<p class="help-caption">事实和偏好按50条候选分页，失效或无权查看的记录不显示；共同经历最多12条。</p><div class="section-actions">${offset > 0 ? button("上一页","profile-page","","",`data-id="${Math.max(0,offset-50)}"`) : ""}${profile.has_more ? button("下一页","profile-page","","",`data-id="${profile.next_offset}"`) : ""}</div>`);
}
async function showJargonEvidence(item) {
  const rawEvidence = item.evidence || {}, evidence = Array.isArray(rawEvidence) ? {message_ids:rawEvidence.map(value => value.message_id).filter(Boolean),observations:rawEvidence.map(value => `${value.user_id || "群成员"}：${value.quote || value.content || "（未保存原文）"}`)} : rawEvidence;
  const ids = evidence.message_ids || item.source_ids || [], messages = rows(await api(`/api/messages?${query({scope_id:item.scope_id,limit:200})}`));
  const observations = evidence.examples || evidence.observations;
  openModal("它为什么学到了这个词",`${item.term} · ${label(item.status)}`,`<p class="record-text">${e(item.meaning)}</p><dl class="detail-table"><dt>学习来源</dt><dd>${e(item.created_by || "手动教词")}</dd><dt>置信度</dt><dd>${Number.isFinite(Number(evidence.confidence)) ? `${Math.round(Number(evidence.confidence) * 100)}%` : "未记录"}</dd><dt>观察次数</dt><dd>${e(Array.isArray(observations) ? observations.length : observations || ids.length || "未记录")}</dd></dl>${Array.isArray(observations) && observations.length ? `<h3 style="margin-bottom:10px">观察线索</h3><div class="source-list">${observations.map(value => `<div class="source-item"><p>${e(typeof value === "string" ? value : value.text || value.content || value.quote || JSON.stringify(value))}</p></div>`).join("")}</div>` : ""}<h3 style="margin:18px 0 10px">来源消息</h3><div class="source-list">${ids.length ? ids.map(id => { const source = messages.find(message => message.id === id); return `<div class="source-item"><small>${e(source ? `${source.user_id} · ${fmt(source.created_at)}` : id)}</small><p>${e(source?.content || "来源已不在最近记录中，或已被删除。")}</p></div>`; }).join("") : '<p class="help-caption">手动词条没有关联的聊天证据。</p>'}</div>`);
}

async function saveModal(kind,form) {
  const data = getForm(form), item = state.modalRecord;
  if (kind === "provider") { await saveProvider(form); return; }
  if (kind === "mcp-server") { await saveMcpServer(form); return; }
  let endpoint, payload;
  if (kind === "memory") { endpoint = "/api/memories"; payload = {...data,importance:Number(data.importance),valid_to:data.valid_to ? new Date(data.valid_to).toISOString() : null}; }
  else if (kind === "jargon") { endpoint = "/api/jargon"; payload = data; }
  else if (kind === "task") { endpoint = "/api/tasks"; if (data.kind === "reminder" && !data.due_at) throw new Error("请为提醒选择时间。"); if (data.repeat !== "none" && !data.due_at) throw new Error("重复任务需要设置提醒时间。"); payload = {...data,due_at:data.due_at || null}; }
  else if (kind === "persona") { endpoint = "/api/personas"; payload = {...data,interests:splitNames(data.interests),excluded_topics:splitNames(data.excluded_topics),exploration:{}}; for (const key of ["interval_hours","daily_searches","valid_days"]) { payload.exploration[key] = data["explore_"+key] === "" ? null : Number(data["explore_"+key]); delete payload["explore_"+key]; } }
  else if (kind === "scope") { endpoint = "/api/scopes"; payload = {...data,bot_aliases:splitNames(data.bot_aliases)}; if (!payload.id) delete payload.id; for (const key of ["cooldown_seconds","merge_window_ms","followup_seconds","daily_tokens"]) payload[key] = Number(data[key]); for (const key of ["trigger_at","trigger_reply","trigger_name","plan_enabled","proactive_reply"]) payload[key] = data[key] === "" ? null : data[key] === "true"; for (const key of ["planner_min_interval_ms","planner_daily_calls","planner_daily_tokens","planner_max_age_ms"]) payload[key] = data[key] === "" ? null : Number(data[key]); payload.output = data.output_override ? Object.fromEntries(["enabled","mode","target_chars","max_chars","platform_max_chars","interval_ms","max_parts","reply_max_chars","message_max_chars","remove_parentheses"].map(key => [key,["enabled","remove_parentheses"].includes(key) ? !!data["out_"+key] : key === "mode" ? (data.out_mode || "model") : Number(data["out_"+key] ?? (["reply_max_chars","message_max_chars"].includes(key) ? 0 : undefined))])) : null; for (const key of Object.keys(payload)) if (key.startsWith("out_") || key === "output_override") delete payload[key]; for (const key of Object.keys(item || {})) if (key.startsWith("output_")) payload[key] = null; if (item?.settings?.output || Object.keys(item?.settings || {}).some(key => key.startsWith("output_"))) payload.settings = {output:null,...Object.fromEntries(Object.keys(item.settings).filter(key => key.startsWith("output_")).map(key => [key,null]))}; }
  else return;
  if (kind === "persona") { payload.profile = {}; for (const key of ["residence","occupation","age","birthday","routine","background","relationship"]) { payload.profile[key] = data["profile_"+key] ?? item?.profile?.[key] ?? ""; delete payload["profile_"+key]; } }
  if (kind === "scope") {
    if(data.debounce_enabled !== undefined)payload.debounce_enabled=data.debounce_enabled === "" ? null : data.debounce_enabled === "true";
    for(const key of ["debounce_seconds","debounce_max_wait_seconds"])if(data[key] !== undefined)payload[key]=data[key] === "" ? null : Number(data[key]);
    const inherited = ['debounce_enabled','debounce_seconds','debounce_max_wait_seconds','persona_id','model_id','activity','trigger_at','trigger_reply','trigger_name','plan_enabled','proactive_reply','always_reply','allow_tools','allow_mcp','use_persona_knowledge'];
    if (item?.profile_id != null) for (const key of inherited) if (payload[key] === item[key]) delete payload[key];
    if (!item && payload.persona_id === 'default') delete payload.persona_id;
  }
  await api(endpoint + (item ? `/${encodeURIComponent(item.id)}` : ""),{method:item ? "PUT" : "POST",body:payload});
  closeModal(); toast(item ? "修改已保存" : "已添加到这个小世界"); await bootstrap();
}
function modelPayload(form) { const data = getForm(form), model = {base_url:data.base_url,model:data.model,vision:data.vision}; if (data.api_key) model.api_key = data.api_key; for (const key of ["temperature","top_p","input_price","output_price","timeout_seconds","first_token_timeout_seconds","total_timeout_seconds","max_tokens"]) if (data[key] !== undefined) model[key] = Number(data[key]); if (data.reasoning_effort !== undefined) model.reasoning_effort = data.reasoning_effort.trim(); if (data.custom_params !== undefined) model.custom_params = typeof parseCustomParams === "function" ? parseCustomParams(data.custom_params) : {}; if (data.stream_mode !== undefined) model.stream_mode = data.stream_mode; if (data.timeout_fallback !== undefined) model.timeout_fallback = !!data.timeout_fallback; return model; }
function splitIds(value) { return String(value || "").split(/[\s,，;；]+/).map(item => item.trim()).filter(Boolean); }
function splitNames(value) { return [...new Set(String(value || "").split(/[\n\r,，;；]+/).map(item => item.trim()).filter(Boolean))]; }












// 0.3: provider routing, grounded context summaries, expression learning and diagnostics.
const routeNames = {chat:"日常对话",planner:"Planner 语义规划",memory:"记忆与学习",gate:"群聊判断（兼容路由）",summary:"上下文与人格知识整理",distillation:"人格生成与蒸馏（留空继承日常对话）",embedding:"语义检索"};
const checkNames = {ok:"正常",warning:"待留意",error:"需处理",info:"提示"};
























const toolRunNames = {pending:"待批准",running:"执行中",succeeded:"已完成",failed:"执行失败",unknown:"结果未知",rejected:"已拒绝",cancelled:"已取消"};
















document.addEventListener("click",async event => {
  // Any click outside an open icon palette dismisses it (opening one closes the
  // rest); clicks inside it are untouched so choosing an icon still works.
  window.ShioriSidebar?.closeIconPickers?.(event.target);
  const pageButton = event.target.closest("[data-page]");
  if (pageButton) { event.preventDefault(); await navigate(pageButton.dataset.page); return; }
  const target = event.target.closest("[data-action]"); if (!target || target.disabled) return;
  const action = target.dataset.action, id = target.dataset.id, item = state.records.find(record => record.id === id);
  try {
    if (await window.ShioriLearning?.handleAction(action,target)) return;
    if (await window.ShioriWebSearch?.handleAction(action,target)) return;
    if (await window.ShioriBotModels?.handleAction(action,target)) return;
    if (await window.ShioriEmbedding?.handleAction(action,target)) return;
    if (await window.ShioriPages.handleAction(action,target)) return;
    if (await window.ShioriExperience.handleAction(action,target)) return;
    if (await window.ShioriQuickStart.handleAction(action,target)) return;
    if (await window.ShioriFeatures.handleAction(action,target)) return;
    if (window.ShioriSidebar?.handleAction?.(action,target,sidebarContext())) return;
    if (action === "close-modal") { closeModal(); return; }
    if (action === "refresh") { await bootstrap(); toast("数据已刷新"); }
    else if (action.startsWith("go-")) await navigate(action.slice(3));
    else if (action === "setup") showWizardModal();
    else if (action === "skip-wizard") { sessionStorage.setItem("shiori-wizard-skipped","1"); closeModal(); }
    else if (action === "qq-settings") { await navigate("settings"); $("#qq-section")?.scrollIntoView({behavior:"smooth",block:"start"}); }
    else if (action === "filter") { state.filters.status = target.dataset.status; await renderPage(); }
    else if (action === "culture-mode") { state.cultureMode = target.dataset.mode; state.filters = {}; await renderPage(); }
    else if (action === "new-expression") expressionModal();
    else if (action === "edit-expression" && item) expressionModal(item);
    else if (action === "analyze-expressions") { target.disabled = true; try { const result = await api("/api/expressions/analyze",{method:"POST",body:{scope_id:state.scope}}); toast(result.message || `返回 ${(result.candidates || []).length} 条表达候选`); state.cultureMode = "expressions"; state.filters.status = "pending"; await bootstrap(); } finally { target.disabled = false; } }
    else if (action === "new-provider") providerModal();
    else if (action === "new-mcp") mcpModal();
    else if (action === "edit-mcp") mcpModal((state.settings.mcp_servers || []).find(value => value.id === id));
    else if (action === "resume-tool-runs") await confirmAction("恢复 MCP 调用？","损坏的工具记录已隔离。恢复后可以提出新请求，每次执行仍须批准；隔离记录不会被重新执行。",async () => { await api("/api/tool-runs/resume",{method:"POST"}); },"恢复 MCP 调用");
    else if (action === "go-persona-knowledge") await navigate("personaKnowledge");
    else if (action === "explore-persona") await explorePersona(target);
    else if (action === "delete-persona-knowledge") await confirmAction("删除这条人格知识？","删除后停止用于回答，同一公开来源也不会被定期探索自动恢复。",async () => { await api(`/api/knowledge/${encodeURIComponent(id)}`,{method:"DELETE"}); },"删除知识");
    else if (action === "discover-mcp") await discoverMcp(target);
    else if (action === "delete-mcp") await confirmAction("删除这个工具服务？","移除配置和已保存令牌。该服务的工具将不能继续提出请求，执行记录仍保留。",async () => { const fresh = await api("/api/settings"); await api("/api/settings",{method:"PUT",body:{mcp_servers:mcpArray(fresh).filter(value => value.id !== id)}}); },"删除服务");
    else if (action === "review-tool-run") await toolRunModal(id,true);
    else if (action === "tool-run-detail") await toolRunModal(id);
    else if (action === "reject-tool-run") await confirmAction("拒绝这次工具调用？","本次请求不会发到目标服务执行。",async () => { await api(`/api/tool-runs/${encodeURIComponent(id)}/reject`,{method:"POST",body:{}}); state.pendingToolNotice = null; },"拒绝本次调用");
    else if (action === "edit-provider") providerModal((state.settings.providers || []).find(value => value.id === id));
    else if (action === "test-provider") { target.disabled = true; try { const result = await api("/api/model/test",{method:"POST",body:{provider_id:id}}); toast(result.ok ? `已保存配置连接成功 · ${result.latency_ms ?? "—"} ms` : result.message || "连接测试未通过",!result.ok); } finally { target.disabled = false; } }
    else if (action === "delete-provider") await confirmAction("删除这套模型配置？","相关任务将回到默认模型，并从备用顺序中移除此配置。已保存密钥一并删除。",async () => { const providers = providerArray().filter(value => value.id !== id),routing = {...(state.settings.routing || {})}; for (const key of Object.keys(routeNames)) if (routing[key] === id) routing[key] = ""; routing.fallback = (routing.fallback || []).filter(value => value !== id); await api("/api/settings",{method:"PUT",body:{providers,routing}}); },"删除配置");
    else if (action === "compact-context") await compactContext(target);
    else if (action === "context-sources") await contextSources();
    else if (action === "open-sidebar-manage") openSidebarManage();
    else if (action === "expression-sources") { const message = state.messages.find(value => value.id === id); if (message) await expressionSources(message); }
    else if (action === "review-memory" && item) await reviewMemory(item);
    else if (action === "reject-memory" && item) { await api(`/api/memories/${encodeURIComponent(id)}/reject`,{method:"POST"}); toast("这条候选不会进入记忆"); await bootstrap(); }
    else if (action === "search-memory-detail") { const record = rows(state.knowledgeResults).find(value => value.id === id); if (record) await memoryDetail(record); }
    else if (action === "show-profile") await showProfile();
    else if (action === "profile-page") await showProfile(Number(id));
    else if (action === "analyze-memory") await analyzeMemory(target);
    else if (action === "open-memory-candidates") { state.page = "memory"; state.filters = {status:"pending"}; localStorage.setItem("shiori-page","memory"); renderNavigation(); await renderPage(); window.scrollTo(0,0); }
    else if (action === "jargon-evidence" && item) await showJargonEvidence(item);
    else if (action === "analyze-jargon") { target.disabled = true; try { toast("正在从最近聊天中分析群词候选…"); const result = await api("/api/jargon/analyze",{method:"POST",body:{scope_id:state.scope}}); toast(result.message || `发现 ${(result.candidates || []).length} 个待确认词条`); state.filters.status = "pending"; await bootstrap(); } finally { target.disabled = false; } }
    else if (action === "new-memory") memoryModal();
    else if (action === "edit-memory" && item) memoryModal(item);
    else if (action === "memory-detail" && item) await memoryDetail(item);
    else if (action === "archive-memory" && item) { await api(`/api/memories/${encodeURIComponent(id)}`,{method:"PUT",body:{status:item.status === "archived" ? "active" : "archived"}}); toast(item.status === "archived" ? "记忆已恢复" : "记忆已归档"); await bootstrap(); }
    else if (action === "delete-memory" && item) await confirmAction("删除这条记忆？","删除后它将不再参与回答，并留下删除记录。你也可以先归档。",async () => { await api(`/api/memories/${encodeURIComponent(id)}`,{method:"DELETE"}); },"删除记忆");
    else if (action === "new-jargon") jargonModal();
    else if (action === "edit-jargon" && item) jargonModal(item);
    else if (["approve-jargon","reject-jargon","toggle-jargon"].includes(action) && item) { await api(`/api/jargon/${encodeURIComponent(id)}`,{method:"PUT",body:{status:action === "approve-jargon" ? "approved" : action === "reject-jargon" ? "rejected" : item.status === "disabled" ? "approved" : "disabled"}}); toast("词条状态已更新"); await bootstrap(); }
    else if (action === "delete-jargon" && item) await confirmAction("删除这个词条？","删除后它不再参与理解或表达。",async () => { await api(`/api/jargon/${encodeURIComponent(id)}`,{method:"DELETE"}); },"删除词条");
    else if (action === "new-task") taskModal();
    else if (action === "edit-task" && item) taskModal(item);
    else if (action === "complete-task" && item) { await api(`/api/tasks/${encodeURIComponent(id)}`,{method:"PUT",body:{status:"completed"}}); toast("这件事，完成了 ✓"); await bootstrap(); }
    else if (["cancel-task","retry-task"].includes(action)) { await api(`/api/tasks/${encodeURIComponent(id)}/${action === "cancel-task" ? "cancel" : "retry"}`,{method:"POST"}); toast(action === "cancel-task" ? "任务已取消" : "任务已重新排队"); await bootstrap(); }
    else if (action === "new-persona") personaModal();
    else if (action === "edit-persona") personaModal(state.personas.find(p => p.id === id));
    else if (action === "use-persona") { await api(`/api/scopes/${encodeURIComponent(state.scope)}`,{method:"PUT",body:{persona_id:id}}); toast("当前会话已切换人格"); await bootstrap(); }
    else if (action === "delete-persona") await confirmAction("删除这个人格？","使用它的配置与聊天将切换到其余人格；没有其他人格时使用普通助手。记忆与词典保留，初音未来不会在重启后自动恢复。",async () => { await api(`/api/personas/${encodeURIComponent(id)}`,{method:"DELETE"}); },"删除人格");
    else if (action === "new-scope") scopeModal();
    else if (action === "edit-scope") scopeModal(state.scopes.find(s => s.id === id));
    else if (action === "toggle-scope") { const group = state.scopes.find(s => s.id === id); await api(`/api/scopes/${encodeURIComponent(id)}`,{method:"PUT",body:{enabled:!group.enabled}}); toast(group.enabled ? "会话已暂停" : "会话已启用"); await bootstrap(); }
    else if (action === "scope-chat") { if(state.sending || state.debouncePending)throw new Error("请先完成或取消当前回复与消息收集。");state.scope = id; localStorage.setItem("shiori-scope",id); await navigate("chat"); }
    else if (action === "attach-image") $("#image-input").click();
    else if (action === "remove-image") { state.images.splice(Number(target.dataset.index),1); renderImagePreviews(); }
    else if (action === "view-image") { openModal("对话图片","",`<img class="image-lightbox" src="${e(target.src)}" alt="对话原图">`); $("#modal").classList.add("modal-wide"); }
    else if (action === "chat-suggestion") { if ($("#chat-text")) { $("#chat-text").value = target.dataset.text; $("#chat-text").focus(); state.draft = target.dataset.text; } }
    else if (action === "cancel-chat") state.controller?.abort();
    else if (action === "clear-messages") await confirmAction("清空当前聊天记录？","已提取的记忆、词典和任务会保留。聊天来源可能因此无法展示。",async () => { await api(`/api/messages?${query({scope_id:state.scope})}`,{method:"DELETE"}); },"清空记录");
    else if (action === "message-sources") { const message = state.messages.find(m => m.id === id); if (message) await showSources(message); }
    else if (action === "test-model") await testModel(target);
    else if (action === "model-preset") { const form = target.closest("form"), provider = target.dataset.provider; $('[name="base_url"]',form).value = provider === "openai" ? "https://api.openai.com/v1" : provider === "deepseek" ? "https://api.deepseek.com/v1" : ""; $('[name="model"]',form).value = provider === "deepseek" ? "deepseek-chat" : ""; }
    else if (action === "list-models") { target.disabled = true; try { const result = await api("/api/models"), models = Array.isArray(result) ? result : result.models || result.data || []; $("#model-list").innerHTML = models.map(m => `<option value="${e(typeof m === "string" ? m : m.id)}">`).join(""); toast(`已读取 ${models.length} 个模型，请在模型名称中选择。`); } finally { target.disabled = false; } }
    else if (action === "qq-reconnect") { target.disabled = true; try { await api("/api/qq/reconnect",{method:"POST"}); toast("已请求重新连接"); await bootstrap(); } finally { target.disabled = false; } }
    else if (action === "test-official") { target.disabled = true; try { const form = target.closest("form"),data = getForm(form),result = await api("/api/qq/official/test",{method:"POST",body:{app_id:data.app_id,app_secret:data.app_secret || undefined}}); $("#qq-result").innerHTML = `<div class="result-block">${e(result.ok ? "官方凭据验证通过" : "验证未通过")}\n${e(result.message || result.detail || "")}</div>`; } finally { target.disabled = false; } }
    else if (action === "upload-note") $("#note-input").click();
    else if (action === "download-backup") { target.disabled = true; try { const response = await api("/api/backup",{raw:true}),blob = await response.blob(),url = URL.createObjectURL(blob),link = document.createElement("a"); link.href = url; link.download = `Shiori备份-${new Date().toISOString().slice(0,10)}.zip`; link.click(); setTimeout(() => URL.revokeObjectURL(url),30000); toast("完整备份已开始下载，工具历史恢复后只读；不包含密钥。"); } finally { target.disabled = false; } }
    else if (action === "shutdown") { await confirmAction("退出Shiori？","将停止本地服务，退出后提醒暂停。再次双击 shiori.exe 即可启动。",async () => { await api("/api/shutdown",{method:"POST"}); state.stopped = true; $("#content").innerHTML = empty("stop","Shiori已退出","再次双击 shiori.exe 即可启动 Shiori。提醒会在重新启动后继续处理。"); },"退出Shiori"); }
    else if (action === "restore-backup") $("#restore-input").click();
  } catch (error) { toast(error.message,true); }
});

document.addEventListener("submit",async event => {
  const form = event.target.closest("form[data-form]"); if (!form) return; event.preventDefault();
  const kind = form.dataset.form;
  if (kind === "chat") { await window.ShioriExperience.sendChat(form); return; }
  const submit = $('[type="submit"]',form); if (submit) submit.disabled = true;
  if ($("#modal-error",form)) $("#modal-error",form).hidden = true;
  try {
    if (await window.ShioriLearning?.handleSubmit(kind,form)) return;
    if (await window.ShioriWebSearch?.handleSubmit(kind,form)) return;
    if (await window.ShioriBotModels?.handleSubmit(kind,form)) return;
    if (await window.ShioriPages.handleSubmit(kind,form)) return;
    if (await window.ShioriEmbedding?.handleSubmit(kind,form)) return;
    if (await window.ShioriExperience.handleSubmit(kind,form)) return;
    if (kind === "quick-start") { await window.ShioriQuickStart.submit(form); return; }
    if (await window.ShioriFeatures.handleSubmit(kind,form)) return;
    if (kind === "sidebar-manage") {
      saveSidebarTitles(form); closeModal();
      toast(window.ShioriSidebar?.isTemporary?.() ? "本次侧边栏调整仅临时生效，较新版本的原存档已保留。" : "侧边栏布局已保存");
      renderNavigation(); return;
    }
    if (kind === "approve-tool-run") await approveToolRun(form);
    else if (kind === "mcp-allowlist") await saveMcpAllowlist(form);
    else if (kind === "confirm") { await state.confirm(); closeModal(); toast(state.stopped ? "Shiori已退出，可以关闭页面" : "操作已完成"); if (!state.stopped) await bootstrap(); }
    else if (kind === "memory-review") { await api(`/api/memories/${encodeURIComponent(state.modalRecord.id)}/approve`,{method:"POST",body:{replace_conflicts:getForm(form).replace_conflicts || false}}); closeModal(); toast("这段记忆，已经认真记住了"); await bootstrap(); }
    else if (kind === "culture-switches") { await api(`/api/scopes/${encodeURIComponent(state.scope)}`,{method:"PUT",body:getForm(form)}); toast("学习、理解与使用规则已保存"); await bootstrap(); }
    else if (kind === "expression-switches") { await api(`/api/scopes/${encodeURIComponent(state.scope)}`,{method:"PUT",body:getForm(form)}); toast("表达学习与使用规则已保存"); await bootstrap(); }
    else if (kind === "knowledge-search") { const values = getForm(form),searchedScope = state.scope; state.knowledgeQuery = values.q; state.knowledgeUser = values.user_id; const results = await api(`/api/memory/search?${query({scope_id:searchedScope,user_id:values.user_id,q:values.q})}`); state.knowledgeResults = {...results,scope_id:searchedScope}; await renderPage(); }
    else if (["model","wizard","qq","budget","system","memory-settings","routing","context-settings","identity","output","exploration","planner","resource-settings"].includes(kind)) await saveSettings(kind,form);
    else await saveModal(kind,form);
  } catch (error) { if (form.closest("dialog")) modalError(error.message); else toast(error.message,true); }
  finally { if (submit) submit.disabled = false; }
});
let searchTimer;
document.addEventListener("input",event => {
  if (typeof syncRangePair === "function") syncRangePair(event.target);
  window.ShioriPages.handleInput(event.target);
  window.ShioriEmbedding?.handleInput(event.target);
  window.ShioriExperience.handleInput(event.target);
  if (event.target.id === "record-search") { clearTimeout(searchTimer); const value = event.target.value,selection = event.target.selectionStart; searchTimer = setTimeout(async () => { state.filters.q = value; await renderPage(); const search = $("#record-search"); if (search) { search.focus(); search.setSelectionRange(selection,selection); } },350); }
  if (event.target.id === "chat-text") { state.draft = event.target.value; event.target.style.height = "auto"; event.target.style.height = Math.min(event.target.scrollHeight,180) + "px"; }
});
document.addEventListener("change",async event => {
  if(event.target.id === "time-zone-preset" && event.target.value){const form=event.target.closest("form");$('[name="timezone"]',form).value=event.target.value;return;}
  if (window.ShioriSidebar?.handleChange?.(event.target,sidebarContext())) return;
  try { if (await window.ShioriWebSearch?.handleChange(event.target)) return; } catch (error) { toast(error.message,true); return; }
  try { if (await window.ShioriEmbedding?.handleChange(event.target)) return; } catch (error) { toast(error.message,true); return; }
  try { if (await window.ShioriPages.handleChange(event.target)) return; } catch (error) { toast(error.message,true); return; }
  try { if (await window.ShioriExperience.handleChange(event.target)) return; } catch (error) { toast(error.message,true); return; }
  if (event.target.id === "global-scope") { if (state.sending || state.debouncePending) { event.target.value = state.scope; toast("请等待当前回复，或先停止等待。",true); return; } state.scope = event.target.value; localStorage.setItem("shiori-scope",state.scope); state.filters = {}; state.draft = ""; state.images = []; await renderPage(); }
  if (event.target.name === "adapter" && !window.ShioriQuickStart.adapterChanged(event.target)) updateAdapterFields();
  if (event.target.id === "knowledge-persona") { state.knowledgePersona = event.target.value; await renderPage(); }
  if (event.target.id === "image-input") {
    try { for (const file of [...event.target.files]) { if (state.images.length >= 3) throw new Error("一次最多附加 3 张图片。"); if (file.size > 5 * 1024 * 1024) throw new Error(`${file.name} 超过 5 MB，请压缩后再发送。`); if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) throw new Error("支持 PNG、JPEG、WebP 与 GIF 图片。"); const data = await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); }); state.images.push({name:file.name,data}); } renderImagePreviews(); }
    catch (error) { toast(error.message || "图片读取失败",true); } finally { event.target.value = ""; }
  }
  if (event.target.id === "note-input" && event.target.files[0]) {
    const file = event.target.files[0]; event.target.value = "";
    try { if (file.size > 8 * 1024 * 1024) throw new Error("资料大小不能超过 8 MB。"); const data = new FormData(); data.append("file",file); data.append("scope_id",state.scope); toast("正在解析并导入本地资料…"); const result = await api("/api/files",{method:"POST",body:data}); toast(result.message || "资料已存入当前会话，可在知识页检索"); await bootstrap(); } catch (error) { toast(error.message,true); }
  }
  if (event.target.id === "restore-input" && event.target.files[0]) {
    const file = event.target.files[0]; event.target.value = "";
    await confirmAction("恢复这份备份？",`将使用「${file.name}」替换现有数据。恢复后自动回复暂停，所有连接密钥需要重新配置。建议先下载当前备份。`,async () => { const data = new FormData(); data.append("file",file); await api("/api/backup/restore",{method:"POST",body:data}); },"恢复并替换");
  }
});
document.addEventListener("keydown",event => {
  if (event.target.id === "chat-text" && event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); event.target.closest("form").requestSubmit(); }
  // Escape closes an open icon palette and must NOT also close the dialog. The
  // keydown default action is what starts the dialog's close request, and keydown
  // is cancelable — the dialog's "cancel" event is not, so preventing it there is
  // unreliable. The flag lets the cancel handler cooperate if it fires anyway.
  if (event.key === "Escape" && window.ShioriSidebar?.closeIconPickers?.(null)) {
    paletteEscape = true;
    event.preventDefault();
    setTimeout(() => { paletteEscape = false; }, 0);
  }
});
$("#menu-toggle").addEventListener("click",() => { $("#sidebar").classList.toggle("open"); $("#sidebar-scrim").classList.toggle("open"); });
$("#sidebar-scrim").addEventListener("click",() => { $("#sidebar").classList.remove("open"); $("#sidebar-scrim").classList.remove("open"); });
// Escape first closes an open icon palette; only the next Escape closes the
// dialog. paletteEscape is set by the keydown handler above (see the note there).
let paletteEscape = false;
$("#modal").addEventListener("cancel",event => {
  if (paletteEscape || window.ShioriSidebar?.closeIconPickers?.(null)) { paletteEscape = false; event.preventDefault(); return; }
  if (state.modalKind === "auth") event.preventDefault();
  if (state.modalKind === "wizard") sessionStorage.setItem("shiori-wizard-skipped","1");
});
$("#modal").addEventListener("close",syncToastLayer);

(async function start() {
  await window.ShioriAuth.init(async () => { await bootstrap(true); });
})();
