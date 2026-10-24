"use strict";

// Sidebar layout: pinned entries, user-editable groups and per-page visibility.
// The layout is a local UI preference, so it lives in localStorage and never
// touches the backend. Anything read from storage is treated as untrusted input:
// page keys are checked against the live page map, titles are escaped on render,
// and icons must come from the whitelist below.
window.MikubotSidebar = (() => {
  const STORAGE_KEY = "mikubot-sidebar-layout-v1";
  // v2 introduced 未分类; v3 retired 对话与人格 and renamed 模型与能力 to 拓展工具;
  // v4 renamed 系统 to 数据与日志; v5 renamed 记忆与知识 to 认知与表达.
  const VERSION = 5;
  // The version that introduced the 未分类 defaults. Older layouts adopt them;
  // newer ones keep whatever the user chose.
  const UNGROUPED_VERSION = 2;
  const PINNED = ["chat", "overview"];
  // Pinned entries keep their own sidebar wording (the overview page title is
  // "机器人概览", but the sidebar entry has always read "机器人管理").
  const PINNED_LABELS = { chat: "聊天", overview: "机器人管理" };
  const EXCLUDED = ["help"];
  const MAX_TITLE = 20;
  const MAX_GROUPS = 12;

  // Built-in baseline. Pages absent from a saved layout are appended back to
  // their default group, so a later release never loses a page.
  const DEFAULT_GROUPS = [
    { id: "model", title: "拓展工具", icon: "spark", pages: ["tools", "plugins"] },
    { id: "memory", title: "认知与表达", icon: "memory", pages: ["memory", "knowledge", "culture"] },
    { id: "automation", title: "自动化", icon: "clock", pages: ["tasks", "jobs"] },
    { id: "system", title: "数据与日志", icon: "settings", pages: ["groups", "logs", "statistics", "diagnostics"] },
  ];
  // Pages that stand on their own by default instead of living in a category.
  const DEFAULT_UNGROUPED = ["persona", "personaKnowledge", "models", "settings"];
  // Layout migration: a retired preset is dropped and its pages move to 系统; a
  // preset whose title the user never edited picks up the new default name.
  const RETIRED_GROUPS = ["talk"];
  const LEGACY_TITLES = { model: "模型与能力", system: "系统", memory: "记忆与知识" };
  const ICON_CHOICES = ["chat", "spark", "memory", "clock", "settings", "tasks", "tools", "file", "shield", "groups", "globe", "search"];

  const fallbackEscape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  // Every entry point accepts the shell's escaper; fall back to the local one so
  // the module still renders (and still escapes) when called without it.
  const esc = (value, fn) => (typeof fn === "function" ? fn(value) : fallbackEscape(value));
  const icon = (name, fn) => (typeof fn === "function" ? fn(name) : "");
  const groupId = value => (typeof value === "string" && /^[a-z0-9_-]{1,32}$/.test(value) ? value : "");
  // Only strings are titles. Calling String(value) is NOT safe on parsed JSON: an
  // object such as {"toString":1} makes String() throw "Cannot convert object to
  // primitive value", which would take the whole shell down.
  const cleanTitle = (value, fallback) => {
    if (typeof value !== "string") return fallback;
    const text = value.replace(/[\r\n\t]+/g, " ").trim();
    return text ? text.slice(0, MAX_TITLE) : fallback;
  };
  const cleanIcon = (value, fallback) => (ICON_CHOICES.includes(value) ? value : fallback);

  function knownPages(pageNames) {
    return Object.keys(pageNames || {}).filter(key => !PINNED.includes(key) && !EXCLUDED.includes(key));
  }

  function storage() {
    try { return typeof localStorage !== "undefined" ? localStorage : null; } catch { return null; }
  }

  function readSaved() {
    const store = storage();
    try {
      if (!store || typeof store.getItem !== "function") return null;
      const raw = store.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch { return null; }
  }

  // Merge a saved layout onto the live page map. Everything read from storage is
  // untrusted: entries are type-checked individually, so a malformed field falls
  // back to its default instead of throwing.
  function merge(pageNames, saved) {
    const known = new Set(knownPages(pageNames));
    const assigned = new Set();
    const hidden = new Set((Array.isArray(saved?.hidden) ? saved.hidden : []).filter(page => known.has(page)));
    // Pages moved out of every category stay loose: they are rendered as
    // standalone entries instead of inside a group. A layout saved by an older
    // release has no ungrouped list, so it adopts the new defaults once rather
    // than silently keeping the old grouping. A layout from a NEWER release is
    // left alone: its structure is not ours to rewrite.
    const savedVersion = Number(saved?.version) || 0;
    // Structure migrations run for anything older than this build; the 未分类
    // defaults only apply to layouts that predate the feature, so a later bump
    // never undoes a page the user deliberately moved back into a category.
    const migrated = !saved || savedVersion < VERSION;
    const adoptUngrouped = !saved || savedVersion < UNGROUPED_VERSION;
    const ungrouped = (adoptUngrouped ? DEFAULT_UNGROUPED : (Array.isArray(saved.ungrouped) ? saved.ungrouped : []))
      .filter(page => typeof page === "string" && known.has(page))
      .filter((page, index, list) => list.indexOf(page) === index);
    for (const page of ungrouped) assigned.add(page);
    const groups = [];
    // Dedupe while collecting: filtering first and mapping after would let the
    // same page appear twice when a saved group lists it twice.
    const takePages = pages => {
      const result = [];
      for (const page of (Array.isArray(pages) ? pages : [])) {
        if (typeof page !== "string" || !known.has(page) || assigned.has(page)) continue;
        assigned.add(page);
        result.push(page);
      }
      return result;
    };

    for (const raw of (Array.isArray(saved?.groups) ? saved.groups : [])) {
      if (!raw || typeof raw !== "object" || groups.length >= MAX_GROUPS) continue;
      const id = groupId(raw.id);
      const preset = DEFAULT_GROUPS.find(item => item.id === id);
      if (!id || groups.some(item => item.id === id)) continue;
      const pages = takePages(raw.pages);
      // Keep empty user-created groups so a freshly added category survives a
      // reload; empty built-in groups are re-created from the baseline anyway.
      if (!pages.length && !preset && !raw.custom) continue;
      groups.push({ id, title: cleanTitle(raw.title, preset?.title || "未命名分类"),
                    icon: cleanIcon(raw.icon, preset?.icon || "spark"), pages,
                    collapsed: raw.collapsed !== false, visible: raw.visible !== false,
                    custom: !preset || Boolean(raw.custom) });
    }
    // Built-in categories the user deleted on purpose must stay deleted; the
    // preset loop below would otherwise recreate them (empty) on every reload.
    const removed = [...new Set((Array.isArray(saved?.removed) ? saved.removed : [])
      .filter(id => typeof id === "string" && DEFAULT_GROUPS.some(preset => preset.id === id)))];
    const removedSet = new Set(removed);
    for (const preset of DEFAULT_GROUPS) {
      if (removedSet.has(preset.id)) continue;
      let group = groups.find(item => item.id === preset.id);
      if (!group) {
        group = { id: preset.id, title: preset.title, icon: preset.icon, pages: [], collapsed: true, visible: true };
        groups.push(group);
      }
      for (const page of preset.pages) if (known.has(page) && !assigned.has(page)) { group.pages.push(page); assigned.add(page); }
    }
    if (migrated) {
      // Retired presets disappear, handing their pages to 系统 so nothing is lost.
      const home = groups.find(item => item.id === "system") || groups[0];
      for (const id of RETIRED_GROUPS) {
        const at = groups.findIndex(item => item.id === id);
        if (at < 0) continue;
        const [gone] = groups.splice(at, 1);
        if (home && gone !== home) for (const page of gone.pages) if (!home.pages.includes(page)) home.pages.push(page);
      }
      // Only rename a preset the user has not renamed themselves.
      for (const [id, legacyTitle] of Object.entries(LEGACY_TITLES)) {
        const group = groups.find(item => item.id === id);
        const preset = DEFAULT_GROUPS.find(item => item.id === id);
        if (group && preset && group.title === legacyTitle) group.title = preset.title;
      }
    }
    // A page the defaults do not know about still stays reachable. Prefer a
    // VISIBLE category: appending to a hidden one would give the page no sidebar
    // entry at all, and navigating to it would be refused as "hidden".
    const homeFor = list => list.find(group => group.visible !== false);
    for (const page of known) if (!assigned.has(page)) {
      const target = homeFor(groups);
      if (target) { target.pages.push(page); assigned.add(page); }
      else { ungrouped.push(page); assigned.add(page); }
    }
    // The cap is a storage guard, never a reason to lose a page. Slice first,
    // then re-run the orphan pass against the groups that actually survive, so
    // presets appended past the cap cannot drop their pages.
    const kept = groups.slice(0, MAX_GROUPS);
    const reachable = new Set(kept.flatMap(group => group.pages));
    const loose = new Set(ungrouped);
    const orphans = [...known].filter(page => !reachable.has(page) && !loose.has(page));
    if (orphans.length) {
      // With every category hidden or deleted there is nowhere visible to push an
      // orphan, so it becomes a standalone entry rather than a silent no-entry.
      const target = homeFor(kept);
      if (target) target.pages.push(...orphans);
      else for (const page of orphans) { ungrouped.push(page); loose.add(page); }
    }
    return { version: VERSION, groups: kept, hidden: [...hidden], ungrouped, removed };
  }

  let state = null;
  let signature = "";
  // The current page's group opens automatically once per page change, but a
  // manual collapse of that same group still wins until the user navigates.
  let autoExpanded = "";
  let lastCurrent = "";

  function ensure(pageNames) {
    const next = knownPages(pageNames).sort().join(",");
    if (!state || next !== signature) {
      signature = next;
      state = merge(pageNames, readSaved());
    }
    return state;
  }

  function persist() {
    const store = storage();
    try { if (store && typeof store.setItem === "function") store.setItem(STORAGE_KEY, JSON.stringify(state)); } catch {}
  }

  // Loads (or reloads) the layout for the given page map. Callers that need to
  // ask about visibility before rendering can call this first.
  function prepare(pageNames) {
    return ensure(pageNames || {});
  }

  function groupFor(page) {
    return state ? state.groups.find(group => group.pages.includes(page)) : null;
  }
  const isHidden = page => Boolean(state && state.hidden.includes(page));
  const visiblePages = group => group.pages.filter(page => !isHidden(page));
  // A page is reachable from the sidebar only when neither the page nor its
  // whole group is hidden. The group flag is independent of the page count so an
  // empty, newly added category can still be shown or hidden.
  function isPageVisible(page) {
    if (!state || isHidden(page)) return false;
    const group = groupFor(page);
    return !group || group.visible !== false;
  }

  function navItem(page, title, current, iconFn, escFn) {
    const e = value => esc(value, escFn);
    return `<button type="button" class="nav-item ${page === current ? "active" : ""}" data-page="${e(page)}" ${page === current ? 'aria-current="page"' : ""}>${icon(page, iconFn)}<span>${e(title || page)}</span></button>`;
  }

  function render({ pageNames = {}, current = "", icon: iconFn, esc: escFn } = {}) {
    const layout = ensure(pageNames);
    const e = value => esc(value, escFn);
    if (current !== lastCurrent) { lastCurrent = current; autoExpanded = groupFor(current)?.id || ""; }
    const pin = PINNED.map(page => navItem(page, PINNED_LABELS[page] || pageNames[page], current, iconFn, escFn)).join("");
    const blocks = layout.groups.map(group => {
      if (group.visible === false) return "";
      const pages = visiblePages(group);
      const expanded = !group.collapsed || group.id === autoExpanded;
      const body = pages.map(page => navItem(page, pageNames[page], current, iconFn, escFn)).join("");
      // An empty but visible group still renders, otherwise re-showing it (or a
      // freshly added category) would silently do nothing until a page moved in.
      // Say which kind of empty it is: "no pages" and "all pages hidden" are
      // different situations and the user acts on them differently.
      const empty = pages.length ? "" : `<p class="nav-group-empty">${group.pages.length ? "这个分类的页面都已隐藏" : "这个分类还没有页面"}</p>`;
      return `<section class="nav-group" data-group="${e(group.id)}"><button type="button" class="nav-group-head" data-action="sidebar-toggle-group" data-id="${e(group.id)}" aria-expanded="${expanded}" aria-controls="nav-group-${e(group.id)}">${icon(group.icon, iconFn)}<span class="nav-group-title">${e(group.title)}</span><span class="nav-group-count">${pages.length}</span><span class="nav-group-chevron" aria-hidden="true">${expanded ? "▾" : "▸"}</span></button><div class="nav-group-pages" id="nav-group-${e(group.id)}" ${expanded ? "" : "hidden"}>${body}${empty}</div></section>`;
    }).join("");
    // Pages moved to 未分类 stand on their own, outside every category. They sit
    // above the grouped sections so the categories read as one block at the end.
    const loose = (layout.ungrouped || [])
      .filter(page => !isHidden(page))
      .map(page => navItem(page, pageNames[page], current, iconFn, escFn)).join("");
    const looseBlock = loose ? `<div class="nav-ungrouped">${loose}</div>` : "";
    return `<div class="primary-navigation">${pin}</div>${looseBlock}<div class="nav-groups">${blocks}</div>`;
  }

  // Two separate views keep the dialog readable: one edits categories, the other
  // edits which pages are visible and where they live. The active view is local
  // UI state (not persisted): reopening the dialog always starts on categories.
  let manageView = "groups";

  function groupRows(pageNames, iconFn, e) {
    const layout = ensure(pageNames);
    return layout.groups.map((group, index) => {
      const shown = group.visible !== false;
      const count = visiblePages(group).length;
      const palette = ICON_CHOICES.map(name => `<button type="button" class="icon-choice ${name === group.icon ? "active" : ""}" data-action="sidebar-group-icon" data-id="${e(group.id)}" data-icon="${e(name)}" title="选择图标" aria-label="选择图标">${icon(name, iconFn)}</button>`).join("");
      return `<div class="sidebar-manage-row" data-id="${e(group.id)}"><span class="sidebar-manage-order"><button type="button" class="icon-button" data-action="sidebar-group-up" data-id="${e(group.id)}" aria-label="上移分类" ${index === 0 ? "disabled" : ""}>↑</button><button type="button" class="icon-button" data-action="sidebar-group-down" data-id="${e(group.id)}" aria-label="下移分类" ${index === layout.groups.length - 1 ? "disabled" : ""}>↓</button></span><input class="sidebar-manage-title" data-sidebar-field="title" data-id="${e(group.id)}" value="${e(group.title)}" maxlength="${MAX_TITLE}" aria-label="分类名称"><details class="sidebar-icon-picker"><summary class="icon-button" aria-label="更换图标" title="更换图标">${icon(group.icon, iconFn)}</summary><div class="sidebar-icon-palette">${palette}</div></details><button type="button" class="icon-button" data-action="sidebar-group-visibility" data-id="${e(group.id)}" aria-pressed="${shown ? "true" : "false"}" aria-label="${shown ? "隐藏" : "显示"}该分类" title="${shown ? "隐藏" : "显示"}该分类">${shown ? "👁" : "🚫"}</button><button type="button" class="icon-button danger" data-action="sidebar-group-delete" data-id="${e(group.id)}" aria-label="删除分类" title="删除分类（页面会移到未分类）">✕</button><span class="sidebar-manage-count">${count} 个页面</span></div>`;
    }).join("");
  }

  function pageRows(pageNames, e) {
    const layout = ensure(pageNames);
    return knownPages(pageNames).map(page => {
      const group = groupFor(page);
      // An empty value means 未分类: the page leaves every category and renders on
      // its own. Group ids are never empty, so the sentinel cannot collide.
      const options = `<option value="" ${group ? "" : "selected"}>未分类</option>`
        + layout.groups.map(item => `<option value="${e(item.id)}" ${group && item.id === group.id ? "selected" : ""}>${e(item.title)}</option>`).join("");
      // A page whose whole category is hidden is not in the sidebar, even though
      // its own eye says "shown" — say so instead of letting the eye contradict
      // what the user sees.
      const groupHidden = Boolean(group && group.visible === false);
      const note = groupHidden ? '<small class="sidebar-manage-note">分类已隐藏</small>' : "";
      return `<div class="sidebar-manage-page ${isHidden(page) ? "is-hidden" : ""}${groupHidden ? " is-group-hidden" : ""}"><button type="button" class="icon-button" data-action="sidebar-page-toggle" data-target-page="${e(page)}" aria-pressed="${isHidden(page) ? "false" : "true"}" aria-label="显示或隐藏该页面" title="显示或隐藏该页面">${isHidden(page) ? "🚫" : "👁"}</button><span class="sidebar-manage-page-title">${e(pageNames[page] || page)}</span>${note}<select class="sidebar-manage-move" data-sidebar-field="page-group" data-target-page="${e(page)}" data-id="${e(group ? group.id : "")}" aria-label="移动到分类">${options}</select></div>`;
    }).join("");
  }

  function manageBody({ pageNames = {}, icon: iconFn, esc: escFn } = {}) {
    const e = value => esc(value, escFn);
    ensure(pageNames);
    const hiddenCount = knownPages(pageNames).filter(isHidden).length;
    // Toggle buttons rather than role=tab: the panels are re-rendered instead of
    // being kept in the DOM, so tab semantics (arrow keys, aria-controls) would be
    // a promise this markup cannot keep. aria-pressed is honest and complete.
    const tabs = `<div class="manage-tabs" role="group" aria-label="管理侧边栏视图"><button type="button" class="manage-tab ${manageView === "groups" ? "active" : ""}" data-action="sidebar-manage-view" data-view="groups" aria-pressed="${manageView === "groups"}">管理分类</button><button type="button" class="manage-tab ${manageView === "pages" ? "active" : ""}" data-action="sidebar-manage-view" data-view="pages" aria-pressed="${manageView === "pages"}">管理页面${hiddenCount ? `<span class="manage-tab-badge">${hiddenCount} 隐藏</span>` : ""}</button></div>`;
    if (manageView === "pages") {
      return `<div class="sidebar-manage">${tabs}<p class="help-caption">眼睛控制这个页面是否出现在侧边栏；右侧下拉框决定它属于哪个分类。隐藏只影响本机浏览器，功能与数据都保留。</p><div class="sidebar-manage-pages expanded">${pageRows(pageNames, e)}</div><div class="section-actions"><button type="button" class="btn ghost" data-action="sidebar-reset">恢复默认</button></div></div>`;
    }
    return `<div class="sidebar-manage">${tabs}<p class="help-caption">拖动顺序按钮调整分类位置；图标按钮可更换分类图标。这里只影响本机浏览器，不会修改机器人配置。</p><div class="sidebar-manage-list">${groupRows(pageNames, iconFn, e)}</div><div class="section-actions"><button type="button" class="btn" data-action="sidebar-group-add">＋ 新增分类</button><button type="button" class="btn ghost" data-action="sidebar-reset">恢复默认</button></div></div>`;
  }

  function moveGroup(id, delta) {
    const index = state.groups.findIndex(group => group.id === id);
    const next = index + delta;
    if (index < 0 || next < 0 || next >= state.groups.length) return false;
    const [group] = state.groups.splice(index, 1);
    state.groups.splice(next, 0, group);
    return true;
  }

  // Returns true when the action belonged to the sidebar UI.
  function handleAction(action, target, ctx = {}) {
    const { pageNames = {}, icon: iconFn, esc: escFn } = ctx;
    const refresh = () => { ctx.rerender?.(); ctx.rerenderManage?.(); };
    if (action === "open-sidebar-manage") { manageView = "groups"; ctx.openManage?.(); return true; }
    if (!action || !action.startsWith("sidebar-")) return false;
    ensure(pageNames);
    const id = target?.dataset?.id || "";
    const page = target?.dataset?.targetPage || "";
    if (action === "sidebar-manage-view") {
      const view = target?.dataset?.view;
      if (view === "groups" || view === "pages") { manageView = view; ctx.rerenderManage?.(); }
      return true;
    }
    if (action === "sidebar-toggle-group") {
      const group = state.groups.find(item => item.id === id);
      if (group) {
        // Toggle the state the user can actually see: a group that is open only
        // because it holds the current page collapses on the first click.
        const open = !group.collapsed || group.id === autoExpanded;
        if (open) { group.collapsed = true; if (autoExpanded === id) autoExpanded = ""; }
        else group.collapsed = false;
        persist(); ctx.rerender?.();
      }
      return true;
    }
    if (action === "sidebar-group-up" || action === "sidebar-group-down") {
      if (moveGroup(id, action.endsWith("up") ? -1 : 1)) { persist(); refresh(); }
      return true;
    }
    if (action === "sidebar-group-visibility") {
      const group = state.groups.find(item => item.id === id);
      if (group) {
        // Explicit per-group flag: works for empty groups too, and hiding a
        // group does not touch the per-page hidden list.
        group.visible = group.visible === false;
        persist(); refresh();
      }
      return true;
    }
    if (action === "sidebar-page-toggle" && page) {
      const at = state.hidden.indexOf(page);
      if (at >= 0) state.hidden.splice(at, 1); else state.hidden.push(page);
      persist(); refresh();
      return true;
    }
    if (action === "sidebar-group-icon") {
      const group = state.groups.find(item => item.id === id);
      const chosen = target?.dataset?.icon;
      if (group && ICON_CHOICES.includes(chosen)) {
        group.icon = chosen;
        persist(); refresh();
      }
      return true;
    }
    if (action === "sidebar-group-delete") {
      const index = state.groups.findIndex(item => item.id === id);
      if (index >= 0) {
        const [removed] = state.groups.splice(index, 1);
        // Deleting a category never deletes a page: its pages become 未分类
        // entries, which is visible, reversible and never hidden behind another
        // category that might itself be hidden.
        for (const page of removed.pages) {
          if (!state.ungrouped.includes(page)) state.ungrouped.push(page);
        }
        // A deleted built-in category must not be recreated on the next reload.
        if (DEFAULT_GROUPS.some(preset => preset.id === removed.id)) {
          state.removed = [...new Set([...(state.removed || []), removed.id])];
        }
        // Deleting the last category is allowed: the sidebar then shows only the
        // pinned entries and 未分类 pages. No rebuild, so nothing is resurrected.
        persist(); refresh();
      }
      return true;
    }
    if (action === "sidebar-group-add") {
      if (state.groups.length < MAX_GROUPS) {
        let index = 1;
        while (state.groups.some(group => group.id === `custom${index}`)) index++;
        state.groups.push({ id: `custom${index}`, title: "新分类", icon: "spark", pages: [], collapsed: false, visible: true, custom: true });
        persist(); refresh();
      } else {
        // Never fail silently: a button that does nothing reads as broken.
        ctx.notify?.(`最多 ${MAX_GROUPS} 个分类，请先删除一个再新增。`, true);
      }
      return true;
    }
    if (action === "sidebar-reset") {
      state = merge(pageNames, null); persist(); refresh();
      return true;
    }
    return true; // unknown sidebar-* action: swallow rather than fall through
  }

  function handleChange(target, ctx = {}) {
    const field = target?.dataset?.sidebarField;
    if (!field) return false;
    ensure(ctx.pageNames || {});
    const id = target.dataset.id || "";
    if (field === "title") {
      const group = state.groups.find(item => item.id === id);
      if (group) { group.title = cleanTitle(target.value, group.title); persist(); ctx.rerender?.(); ctx.rerenderManage?.(); }
      return true;
    }
    if (field === "page-group") {
      const page = target.dataset.targetPage;
      // The select's VALUE is the destination group. `data-id` only records the
      // current group, so never rely on it here: a stale/absent attribute used to
      // make the move a silent no-op. An empty value means 未分类.
      const chosen = String(target.value ?? "");
      const destination = state.groups.find(item => item.id === chosen);
      if (page && (destination || chosen === "")) {
        for (const item of state.groups) item.pages = item.pages.filter(value => value !== page);
        state.ungrouped = state.ungrouped.filter(value => value !== page);
        if (destination) {
          destination.pages.push(page);
          // Moving a page means the user wants to see it there: reveal the target
          // group and open it, otherwise the move looks like it did nothing.
          destination.visible = true;
          destination.collapsed = false;
        } else {
          // 未分类: the page stands on its own, outside every category.
          state.ungrouped.push(page);
        }
        const at = state.hidden.indexOf(page);
        if (at >= 0) state.hidden.splice(at, 1); // moving a page makes it visible again
        persist(); ctx.rerender?.(); ctx.rerenderManage?.();
      }
      return true;
    }
    return false;
  }

  // The icon palette is a <details> popover, which has no built-in dismissal.
  // Any click outside it closes it (and opening one palette closes the others),
  // matching the usual popover expectation. Clicks inside are left alone so
  // toggling the summary and choosing an icon keep working.
  function closeIconPickers(target) {
    if (typeof document === "undefined" || typeof document.querySelectorAll !== "function") return false;
    const owner = target && typeof target.closest === "function" ? target.closest(".sidebar-icon-picker") : null;
    let closed = false;
    for (const picker of document.querySelectorAll(".sidebar-icon-picker[open]")) {
      if (picker === owner) continue;
      picker.open = false;
      closed = true;
    }
    return closed;
  }

  return { STORAGE_KEY, VERSION, PINNED, EXCLUDED, DEFAULT_GROUPS, ICON_CHOICES, prepare, isHidden, isPageVisible, render, manageBody, handleAction, handleChange, closeIconPickers, _merge: merge, _view: () => manageView };
})();
