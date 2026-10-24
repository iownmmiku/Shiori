"use strict";
// Regression checks for the grouped sidebar and the sidebar management dialog.
// The layout module is loaded in a VM with a small storage/document shim so the
// real merge, hide/show, reorder and escaping rules are exercised.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "..", "mikubot", "static");
const source = fs.readFileSync(path.join(root, "sidebar-layout.js"), "utf8");
const appSource = fs.readFileSync(path.join(root, "app.js"), "utf8");
const indexSource = fs.readFileSync(path.join(root, "index.html"), "utf8");
const experienceCss = fs.readFileSync(path.join(root, "experience.css"), "utf8");
const toolsPageSource = fs.readFileSync(path.join(root, "tools-page.js"), "utf8");

const PAGE_NAMES = { overview: "机器人概览", chat: "聊天", models: "模型供应商", memory: "记忆档案",
  culture: "群语言与表达", knowledge: "知识与检索", personaKnowledge: "人格知识", tasks: "任务与提醒",
  tools: "MCP", persona: "机器人配置", groups: "群与会话", logs: "运行日志", statistics: "数据统计",
  plugins: "插件", diagnostics: "自检与诊断", settings: "系统设置", jobs: "后台任务", help: "使用指南" };

const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const icon = name => `<svg data-icon="${name}"></svg>`;

function makeStorage(initial) {
  const data = new Map(initial ? Object.entries(initial) : []);
  return { getItem: key => (data.has(key) ? data.get(key) : null),
           setItem: (key, value) => data.set(key, String(value)),
           removeItem: key => data.delete(key),
           _dump: () => Object.fromEntries(data) };
}

function boot(storageValue, pageNames = PAGE_NAMES) {
  const store = makeStorage(storageValue);
  // Minimal document shim so the popover-dismissal helper can be exercised.
  const pickers = [];
  const document = { querySelectorAll: selector => (selector.includes(".sidebar-icon-picker[open]") ? pickers.filter(item => item.open) : []) };
  const sandbox = { window: {}, localStorage: store, console, document };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "sidebar-layout.js" });
  return { api: sandbox.window.MikubotSidebar, store, pageNames, pickers };
}

const render = (api, pageNames, current) => api.render({ pageNames, current, icon, esc: escape });
const count = (text, needle) => text.split(needle).length - 1;
const dialog = (api, pageNames) => api.manageBody({ pageNames, icon, esc: escape });
// The dialog has two views; switching is an action, so tests must enter the view
// they assert on (the category view is the default).
const switchView = (api, pageNames, view) => api.handleAction("sidebar-manage-view", { dataset: { view } }, { pageNames });
const pagesView = (api, pageNames) => { switchView(api, pageNames, "pages"); return dialog(api, pageNames); };
const groupsView = (api, pageNames) => { switchView(api, pageNames, "groups"); return dialog(api, pageNames); };

// Strict per-group parsing. A lazy /data-group="a"[\s\S]*?data-page="b"/ regex
// can match across group boundaries and would mask a page that failed to move.
function groupsOf(html) {
  const result = new Map();
  // Strip the un-grouped block (it renders above the sections) so its pages are
  // never attributed to a category.
  const grouped = html.replace(/<div class="nav-ungrouped">[\s\S]*?<\/div>/, "");
  for (const section of grouped.split('<section class="nav-group"').slice(1)) {
    const id = /data-group="([^"]+)"/.exec(section)?.[1];
    if (!id) continue;
    result.set(id, [...section.matchAll(/data-page="([^"]+)"/g)].map(match => match[1]));
  }
  return result;
}
const groupOfPage = (html, page) => {
  for (const [id, pages] of groupsOf(html)) if (pages.includes(page)) return id;
  return null;
};
// Pages moved to 未分类 render outside every group section.
function looseOf(html) {
  const at = html.indexOf('<div class="nav-ungrouped">');
  if (at < 0) return [];
  return [...html.slice(at).split("</div>")[0].matchAll(/data-page="([^"]+)"/g)].map(match => match[1]);
}
// Reads one management row's visibility toggle (aria-pressed + glyph). Rows now
// contain a nested icon palette, so split on the row marker instead of lazily
// matching the first `</div>` (which would stop inside the palette).
function manageRow(html, id) {
  const parts = html.split('<div class="sidebar-manage-row"').slice(1);
  const chunk = parts.find(part => part.startsWith(` data-id="${id}"`));
  if (!chunk) return null;
  return chunk.split('<div class="sidebar-manage-row"')[0];
}
function eyeOf(html, id) {
  const row = manageRow(html, id);
  if (!row) return null;
  return { pressed: /aria-pressed="(true|false)"/.exec(row)?.[1], shown: row.includes("👁") };
}
function expandedOf(html, id) {
  const section = html.split('<section class="nav-group"').find(part => part.startsWith(` data-group="${id}"`));
  return section ? /aria-expanded="(true|false)"/.exec(section)?.[1] === "true" : null;
}

function main() {
  // 1. Default layout: pinned entries plus every page inside a group, no page lost.
  {
    const { api, pageNames } = boot(null);
    const html = render(api, pageNames, "chat");
    assert.match(html, /data-page="chat"/);
    assert.match(html, /data-page="overview"/);
    assert.equal(count(html, "nav-group-head"), 4, "four built-in groups");
    for (const page of Object.keys(pageNames)) {
      if (page === "help") continue; // help lives in the sidebar footer, not the nav
      assert.ok(html.includes(`data-page="${page}"`), `default layout still reaches ${page}`);
    }
    assert.ok(!html.includes('data-page="help"'), "help stays out of the grouped nav");
  }

  // 1b. Default grouping: four pages stand on their own, the rest stay grouped.
  {
    const { api, pageNames } = boot(null);
    const html = render(api, pageNames, "chat");
    assert.deepEqual(looseOf(html), ["persona", "personaKnowledge", "models", "settings"], "default ungrouped pages");
    assert.deepEqual([...groupsOf(html)].map(([id, pages]) => [id, pages]), [
      ["model", ["tools", "plugins"]],
      ["memory", ["memory", "knowledge", "culture"]],
      ["automation", ["tasks", "jobs"]],
      ["system", ["groups", "logs", "statistics", "diagnostics"]],
    ], "the remaining pages keep their categories");
    // 对话与人格 is gone, 模型与能力 is renamed, and 群与会话 moved into 系统.
    assert.equal(groupsOf(html).has("talk"), false, "the retired category is gone");
    assert.ok(!html.includes("对话与人格"), "and its title is not rendered");
    assert.match(html, /data-group="model"[\s\S]*?<span class="nav-group-title">拓展工具<\/span>/, "模型与能力 is renamed 拓展工具");
    assert.match(html, /data-group="system"[\s\S]*?data-page="groups"/, "群与会话 moved into 数据与日志");
    assert.match(html, /data-group="system"[\s\S]*?<span class="nav-group-title">数据与日志<\/span>/, "系统 is renamed 数据与日志");
    assert.match(html, /data-group="memory"[\s\S]*?<span class="nav-group-title">认知与表达<\/span>/, "记忆与知识 is renamed 认知与表达");
    assert.ok(!html.includes("记忆与知识"), "the old title is gone");
    assert.match(html, /data-group="memory"[\s\S]*?data-page="culture"/, "群语言与表达 stays under it");
    assert.match(html, /data-page="tools"[^>]*>[\s\S]{0,80}MCP/, "the tools card is renamed MCP");
    // Every page is still reachable exactly once.
    const seen = [...looseOf(html), ...[...groupsOf(html)].flatMap(([, pages]) => pages)].sort();
    assert.deepEqual(seen, Object.keys(pageNames).filter(page => page !== "help" && page !== "chat" && page !== "overview").sort(), "every page is reachable exactly once");
  }

  // 1c. A layout saved by an older release is migrated once: it adopts the new
  //     defaults (未分类, retired 对话与人格, renamed 拓展工具), then keeps the
  //     user's own choices from then on.
  {
    const legacy = JSON.stringify({ version: 1, hidden: [], groups: [
      { id: "talk", title: "对话与人格", icon: "chat", pages: ["persona", "personaKnowledge", "groups"] },
      { id: "model", title: "模型与能力", icon: "spark", pages: ["models", "tools", "plugins"] },
      { id: "memory", title: "记忆与知识", icon: "memory", pages: ["memory", "knowledge", "culture"] },
      { id: "automation", title: "自动化", icon: "clock", pages: ["tasks", "jobs"] },
      { id: "system", title: "系统", icon: "settings", pages: ["settings", "logs", "statistics", "diagnostics"] },
    ] });
    const upgraded = boot({ "mikubot-sidebar-layout-v1": legacy });
    const html = render(upgraded.api, upgraded.pageNames, "chat");
    assert.deepEqual(looseOf(html), ["persona", "personaKnowledge", "models", "settings"], "the old layout adopts the 未分类 defaults");
    assert.equal(groupsOf(html).has("talk"), false, "the retired category is dropped on migration");
    assert.match(html, /data-group="model"[\s\S]*?<span class="nav-group-title">拓展工具<\/span>/, "the old preset title is renamed");
    assert.match(html, /data-group="system"[\s\S]*?data-page="groups"/, "its page lands in 系统 rather than disappearing");
    // The user's own choice afterwards is respected across reloads.
    upgraded.api.handleChange({ value: "memory", dataset: { sidebarField: "page-group", targetPage: "persona" } }, { pageNames: upgraded.pageNames });
    const again = boot(upgraded.store._dump());
    assert.ok(!looseOf(render(again.api, again.pageNames, "chat")).includes("persona"), "a page moved back into a category stays there");
    // A user rename is NOT overwritten by the migration.
    const renamed = boot({ "mikubot-sidebar-layout-v1": JSON.stringify({ version: 2, hidden: [], ungrouped: [], groups: [
      { id: "model", title: "我的工具", icon: "spark", pages: ["tools", "plugins"] },
    ] }) });
    assert.match(render(renamed.api, renamed.pageNames, "chat"), /<span class="nav-group-title">我的工具<\/span>/, "a user-renamed category keeps its name");
    // A v3 layout gets the preset renames but keeps the user's own 未分类 choices.
    const v3 = boot({ "mikubot-sidebar-layout-v1": JSON.stringify({ version: 3, hidden: [], ungrouped: ["models"], groups: [
      { id: "system", title: "系统", icon: "settings", pages: ["groups", "logs", "statistics", "diagnostics"] },
      { id: "memory", title: "记忆与知识", icon: "memory", pages: ["memory", "knowledge", "culture"] },
    ] }) });
    const v3html = render(v3.api, v3.pageNames, "chat");
    assert.match(v3html, /<span class="nav-group-title">数据与日志<\/span>/, "v3 layout picks up the 数据与日志 rename");
    assert.match(v3html, /<span class="nav-group-title">认知与表达<\/span>/, "and the 认知与表达 rename");
    assert.deepEqual(looseOf(v3html), ["models"], "and does not reset the user's 未分类 choices");
  }

  // 1d. A page shipped by a later release must never be filed into a HIDDEN
  //     category: it would have no sidebar entry and navigate() would refuse it.
  {
    const group = (id, title, icon, pages, visible) => ({ id, title, icon, pages, visible });
    const allHidden = JSON.stringify({ version: 5, hidden: [], ungrouped: ["persona", "personaKnowledge", "models", "settings"], groups: [
      group("model", "拓展工具", "spark", ["tools", "plugins"], false),
      group("memory", "认知与表达", "memory", ["memory", "knowledge", "culture"], false),
      group("automation", "自动化", "clock", ["tasks", "jobs"], false),
      group("system", "数据与日志", "settings", ["groups", "logs", "statistics", "diagnostics"], false),
    ] });
    const withFuture = { ...PAGE_NAMES, futurePage: "新页面" };
    const none = boot({ "mikubot-sidebar-layout-v1": allHidden }, withFuture);
    const html = render(none.api, withFuture, "chat");
    assert.ok(html.includes('data-page="futurePage"'), "with every category hidden the new page still gets an entry");
    assert.equal(none.api.isPageVisible("futurePage"), true, "and it is navigable");

    // With one visible category it joins that one, not the hidden first category.
    const oneVisible = JSON.stringify({ version: 5, hidden: [], ungrouped: ["persona", "personaKnowledge", "models", "settings"], groups: [
      group("model", "拓展工具", "spark", ["tools", "plugins"], false),
      group("memory", "认知与表达", "memory", ["memory", "knowledge", "culture"], true),
    ] });
    const some = boot({ "mikubot-sidebar-layout-v1": oneVisible }, withFuture);
    const someHtml = render(some.api, withFuture, "chat");
    assert.equal(groupOfPage(someHtml, "futurePage"), "memory", "the new page joins a visible category");
  }

  // 2. Only the current page's group starts expanded.
  {
    const { api, pageNames } = boot(null);
    const html = render(api, pageNames, "tools");
    const groups = groupsOf(html);
    assert.equal(groups.size, 4, "four built-in groups render");
    for (const id of groups.keys()) {
      assert.equal(expandedOf(html, id), id === "model", `group ${id} expanded state`);
    }
  }

  // 3. Manual collapse of the current group sticks; navigating elsewhere reopens it.
  {
    const { api, pageNames } = boot(null);
    render(api, pageNames, "tools");
    api.handleAction("sidebar-toggle-group", { dataset: { id: "model" } }, { pageNames });
    let html = render(api, pageNames, "tools");
    assert.equal(expandedOf(html, "model"), false, "manual collapse of the current group sticks");
    html = render(api, pageNames, "logs");
    assert.equal(expandedOf(html, "system"), true, "navigating opens the new page's group");
  }

  // 4. Hiding a page removes it everywhere but keeps it in the management list.
  {
    const { api, pageNames, store } = boot(null);
    api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "models" } }, { pageNames });
    const html = render(api, pageNames, "chat");
    assert.ok(!html.includes('data-page="models"'), "hidden page leaves the sidebar");
    assert.ok(html.includes('data-page="tools"'), "sibling pages stay");
    const saved = JSON.parse(store.getItem(api.STORAGE_KEY));
    assert.deepEqual(saved.hidden, ["models"], "hidden page persisted");
    assert.match(pagesView(api, pageNames), /data-target-page="models"/, "the page view still lists the hidden page");
  }

  // 5. Group visibility toggle hides/shows every page of that group.
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-group-visibility", { dataset: { id: "model" } }, { pageNames });
    const html = render(api, pageNames, "chat");
    assert.equal(groupsOf(html).has("model"), false, "hidden group is not rendered");
    for (const page of ["tools", "plugins"]) assert.ok(!html.includes(`data-page="${page}"`), `${page} hidden with its group`);
    // models is ungrouped by default, so hiding a category must not hide it.
    assert.ok(html.includes('data-page="models"'), "an ungrouped page survives hiding a category");
    api.handleAction("sidebar-group-visibility", { dataset: { id: "model" } }, { pageNames });
    const back = groupsOf(render(api, pageNames, "chat"));
    assert.deepEqual(back.get("model"), ["tools", "plugins"], "group restored with its pages");
  }

  // 5b. A brand-new EMPTY group reports itself as visible and can be toggled
  //     back and forth (regression: the eye used to read "hidden" and the
  //     toggle did nothing because visibility was derived from page count).
  {
    const { api, pageNames, store } = boot(null);
    api.handleAction("sidebar-group-add", { dataset: {} }, { pageNames });
    const eye = () => eyeOf(groupsView(api, pageNames), "custom1");
    assert.equal(eye().pressed, "true", "a new empty group starts visible");
    assert.equal(eye().shown, true, "and shows the eye glyph");
    api.handleAction("sidebar-group-visibility", { dataset: { id: "custom1" } }, { pageNames });
    assert.equal(eye().pressed, "false", "clicking the eye hides the empty group");
    api.handleAction("sidebar-group-visibility", { dataset: { id: "custom1" } }, { pageNames });
    assert.equal(eye().pressed, "true", "clicking again shows it");
    // Group visibility must be independent of the per-page hidden list.
    const saved = JSON.parse(store.getItem(api.STORAGE_KEY));
    assert.deepEqual(saved.hidden, [], "group visibility does not hide pages individually");
    // Hidden empty group keeps its state across a reload.
    api.handleAction("sidebar-group-visibility", { dataset: { id: "custom1" } }, { pageNames });
    const reborn = boot({ [api.STORAGE_KEY]: store.getItem(api.STORAGE_KEY) });
    assert.equal(eyeOf(groupsView(reborn.api, pageNames), "custom1").pressed, "false", "hidden state survives a reload");
  }

  // 5d. A visible empty group renders a placeholder in the sidebar, so re-showing
  //     it (or adding one) is visibly effective even before it holds a page.
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-group-add", { dataset: {} }, { pageNames });
    assert.match(render(api, pageNames, "chat"), /data-group="custom1"/, "a visible empty group appears in the sidebar");
    api.handleAction("sidebar-group-visibility", { dataset: { id: "custom1" } }, { pageNames });
    assert.ok(!render(api, pageNames, "chat").includes('data-group="custom1"'), "hidden group disappears");
    api.handleAction("sidebar-group-visibility", { dataset: { id: "custom1" } }, { pageNames });
    assert.match(render(api, pageNames, "chat"), /data-group="custom1"/, "re-showing it brings the group back");
  }

  // 5c. isPageVisible accounts for both the page flag and its group flag.
  {
    const { api, pageNames } = boot(null);
    api.prepare(pageNames);
    assert.equal(api.isPageVisible("tools"), true);
    api.handleAction("sidebar-group-visibility", { dataset: { id: "model" } }, { pageNames });
    assert.equal(api.isPageVisible("tools"), false, "hidden group hides its pages");
    assert.equal(api.isPageVisible("models"), true, "an ungrouped page has no category to hide it");
    assert.equal(api.isPageVisible("chat"), true, "pinned pages are unaffected");
    api.handleAction("sidebar-group-visibility", { dataset: { id: "model" } }, { pageNames });
    api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "models" } }, { pageNames });
    assert.equal(api.isPageVisible("models"), false, "individually hidden page stays hidden");
    assert.equal(api.isPageVisible("tools"), true);
  }

  // 6. Reorder, rename, add and delete groups.
  {
    const { api, pageNames, store } = boot(null);
    api.handleChange({ value: "我的模型", dataset: { sidebarField: "title", id: "model" } }, { pageNames });
    let html = render(api, pageNames, "chat");
    assert.match(html, /我的模型/);
    api.handleAction("sidebar-group-down", { dataset: { id: "model" } }, { pageNames });
    html = render(api, pageNames, "chat");
    assert.ok(html.indexOf("认知与表达") < html.indexOf("我的模型"), "reorder changes render order");
    api.handleAction("sidebar-group-add", { dataset: {} }, { pageNames });
    // An empty but visible group renders a placeholder; the management dialog
    // lists it so it can be filled.
    assert.match(groupsView(api, pageNames), /新分类/);
    assert.equal(groupsOf(render(api, pageNames, "chat")).get("custom1").length, 0, "empty group renders with no pages");
    // Move a page using the REAL select shape (value = destination, no data-id).
    api.handleChange({ value: "custom1", dataset: { sidebarField: "page-group", targetPage: "logs" } }, { pageNames });
    const moved = groupsOf(render(api, pageNames, "chat"));
    assert.deepEqual(moved.get("custom1"), ["logs"], "page really lands in the new group");
    assert.ok(!moved.get("system").includes("logs"), "page left its old group");
    // Deleting a user group keeps its pages reachable and drops the group.
    api.handleAction("sidebar-group-delete", { dataset: { id: "custom1" } }, { pageNames });
    const afterDelete = render(api, pageNames, "chat");
    assert.ok(!afterDelete.includes("新分类"));
    assert.ok(afterDelete.includes('data-page="logs"'), "page from a deleted group stays reachable");
    assert.ok(store.getItem(api.STORAGE_KEY));
  }

  // 7. Moving a page between groups also un-hides it.
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "models" } }, { pageNames });
    // Real select shape: the destination is the control's value.
    api.handleChange({ value: "memory", dataset: { sidebarField: "page-group", targetPage: "models" } }, { pageNames });
    const html = render(api, pageNames, "chat");
    assert.equal(groupOfPage(html, "models"), "memory", "page now lives in the target group");
    // The generated move control must expose the current group and all destinations.
    const body = pagesView(api, pageNames);
    const moveSelect = /<select class="sidebar-manage-move"[^>]*data-target-page="models"[^>]*>([\s\S]*?)<\/select>/.exec(body);
    assert.ok(moveSelect, "move select exists for the page");
    assert.match(moveSelect[0], /data-id="memory"/, "move select records the current group");
    assert.match(moveSelect[1], /<option value="system"/, "move select lists other destinations");
  }

  // 7b. F2: moving INTO a hidden or collapsed group must reveal and open it, so
  //     the move is visible instead of looking like a no-op.
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-group-visibility", { dataset: { id: "memory" } }, { pageNames });
    api.handleAction("sidebar-toggle-group", { dataset: { id: "memory" } }, { pageNames });
    let html = render(api, pageNames, "chat");
    assert.equal(groupsOf(html).has("memory"), false, "target group starts hidden");

    api.handleChange({ value: "memory", dataset: { sidebarField: "page-group", targetPage: "models" } }, { pageNames });
    html = render(api, pageNames, "chat");
    assert.equal(groupOfPage(html, "models"), "memory", "page lands in the previously hidden group");
    assert.equal(expandedOf(html, "memory"), true, "the destination group is open after the move");
    assert.equal(api.isPageVisible("models"), true, "the moved page is visible");

    // Same for a group that is visible but collapsed (built-in groups start
    // collapsed, and the toggle is a real user click: open first, then close).
    const second = boot(null);
    assert.equal(expandedOf(render(second.api, pageNames, "chat"), "memory"), false, "target group starts collapsed");
    second.api.handleChange({ value: "memory", dataset: { sidebarField: "page-group", targetPage: "models" } }, { pageNames });
    assert.equal(expandedOf(render(second.api, pageNames, "chat"), "memory"), true, "collapsed destination opens after the move");
  }

  // 7c. 未分类: a page can leave every category and render as its own entry.
  {
    const DEFAULTS = ["persona", "personaKnowledge", "models", "settings"];
    const { api, pageNames, store } = boot(null);
    assert.deepEqual(looseOf(render(api, pageNames, "chat")), DEFAULTS, "the four default pages are ungrouped");
    assert.match(pagesView(api, pageNames), /<option value="" >?未分类<\/option>/, "the page view offers 未分类");

    api.handleChange({ value: "", dataset: { sidebarField: "page-group", targetPage: "plugins" } }, { pageNames });
    const html = render(api, pageNames, "chat");
    assert.equal(groupOfPage(html, "plugins"), null, "the page left every category");
    assert.deepEqual(looseOf(html), [...DEFAULTS, "plugins"], "it renders as a standalone entry");
    assert.ok(!groupsOf(html).get("model").includes("plugins"), "its old category lost it");
    assert.ok(api.isPageVisible("plugins"), "an ungrouped page stays reachable");
    assert.equal(count(html, 'data-page="plugins"'), 1, "the page is rendered exactly once");
    // Un-grouped entries sit ABOVE the categories.
    assert.ok(html.indexOf('class="nav-ungrouped"') < html.indexOf('<section class="nav-group"'), "未分类 renders above the categories");
    assert.ok(html.indexOf('class="nav-ungrouped"') > html.indexOf('class="primary-navigation"'), "and below the pinned entries");
    assert.match(pagesView(api, pageNames), /data-target-page="plugins"[^>]*><option value="" selected>未分类/, "the control shows 未分类 as current");

    // The choice survives a reload.
    const reborn = boot(store._dump());
    assert.deepEqual(looseOf(render(reborn.api, reborn.pageNames, "chat")), [...DEFAULTS, "plugins"], "未分类 survives a reload");

    // Moving back into a category clears the loose entry.
    reborn.api.handleChange({ value: "model", dataset: { sidebarField: "page-group", targetPage: "plugins" } }, { pageNames: reborn.pageNames });
    const back = render(reborn.api, reborn.pageNames, "chat");
    assert.deepEqual(looseOf(back), DEFAULTS, "moving back into a category removes the loose entry");
    assert.equal(groupOfPage(back, "plugins"), "model", "and the page is in that category again");

    // Hiding an ungrouped page removes it from the sidebar like any other.
    reborn.api.handleChange({ value: "", dataset: { sidebarField: "page-group", targetPage: "plugins" } }, { pageNames: reborn.pageNames });
    reborn.api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "plugins" } }, { pageNames: reborn.pageNames });
    assert.deepEqual(looseOf(render(reborn.api, reborn.pageNames, "chat")), DEFAULTS, "a hidden ungrouped page is not shown");
    assert.equal(reborn.api.isPageVisible("plugins"), false, "and counts as hidden");

    // Deleting a category turns its pages into 未分类 entries: visible and
    // reversible, never hidden inside another (possibly hidden) category.
    const third = boot(null);
    third.api.handleChange({ value: "", dataset: { sidebarField: "page-group", targetPage: "plugins" } }, { pageNames: third.pageNames });
    third.api.handleAction("sidebar-group-delete", { dataset: { id: "model" } }, { pageNames: third.pageNames });
    const after = render(third.api, third.pageNames, "chat");
    assert.deepEqual(looseOf(after).slice().sort(), [...DEFAULTS, "plugins", "tools"].sort(), "the deleted category's pages become 未分类 entries");
    assert.equal(groupOfPage(after, "tools"), null, "and are no longer inside a category");
    assert.ok(after.includes('data-page="tools"'), "but stay reachable");
  }

  // 8. Corrupt or hostile storage never breaks rendering, and titles are escaped.
  {
    const hostile = JSON.stringify({ version: 99, groups: [
      { id: "talk", title: "<img src=x onerror=alert(1)>", icon: "nope", pages: ["models", "models", "unknown", 7] },
      { id: "not valid!", title: "x", pages: ["logs"] },
    ], hidden: ["models", "ghost", 42] });
    const { api, pageNames } = boot({ [JSON.parse(JSON.stringify("mikubot-sidebar-layout-v1"))]: hostile });
    const html = render(api, pageNames, "chat");
    assert.ok(!html.includes("<img src=x"), "hostile title is escaped");
    assert.ok(html.includes("&lt;img src=x onerror=a"), "hostile title is escaped and truncated to the title limit");
    assert.ok(!html.includes("not valid!"), "invalid group id is dropped");
    assert.ok(!html.includes('data-page="models"'), "hidden page stays hidden");
    for (const page of ["persona", "knowledge", "statistics", "diagnostics"]) {
      assert.ok(html.includes(`data-page="${page}"`), `${page} survives a hostile layout`);
    }
    // A completely broken JSON payload falls back to defaults.
    const broken = boot({ "mikubot-sidebar-layout-v1": "{{{not json" });
    assert.match(render(broken.api, pageNames, "chat"), /data-page="models"/);
  }

  // 8b. A non-string title must not crash the shell. String() on a parsed
  //     {"toString":1} throws, and render() runs uncaught during bootstrap.
  {
    const hostile = JSON.stringify({ version: 5, hidden: [], ungrouped: [], groups: [
      { id: "model", title: { toString: 1 }, icon: "spark", pages: ["tools"] },
      { id: "memory", title: ["a"], icon: "memory", pages: ["memory"] },
      { id: "automation", title: 42, icon: "clock", pages: ["tasks"] },
    ] });
    const { api, pageNames } = boot({ "mikubot-sidebar-layout-v1": hostile });
    let html;
    assert.doesNotThrow(() => { html = render(api, pageNames, "chat"); }, "a hostile title cannot throw");
    assert.ok(html.includes('data-page="tools"'), "and the sidebar still renders");
    assert.match(html, /<span class="nav-group-title">拓展工具<\/span>/, "a bad title falls back to the preset name");
  }

  // 8c. A category that lists the same page twice renders it once, and the count
  //     the dialog shows is the honest one.
  {
    const doubled = JSON.stringify({ version: 5, hidden: [], ungrouped: ["persona", "personaKnowledge", "models", "settings"], groups: [
      { id: "model", title: "工具", icon: "spark", pages: ["tools", "tools", "plugins", "tools"] },
    ] });
    const { api, pageNames } = boot({ "mikubot-sidebar-layout-v1": doubled });
    const html = render(api, pageNames, "chat");
    assert.equal(count(html, 'data-page="tools"'), 1, "the page renders exactly once");
    assert.match(manageRow(groupsView(api, pageNames), "model"), /2 个页面/, "the category counts each page once");
  }

  // 8d. Pages whose whole category is hidden are marked in the page view, and the
  //     sidebar placeholder distinguishes "no pages" from "all pages hidden".
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-group-visibility", { dataset: { id: "model" } }, { pageNames });
    const pages = pagesView(api, pageNames);
    assert.match(pages, /is-group-hidden/, "the row is marked as belonging to a hidden category");
    assert.match(pages, /分类已隐藏/, "with a plain-language reason");

    const solo = boot(null);
    solo.api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "tools" } }, { pageNames });
    solo.api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "plugins" } }, { pageNames });
    assert.match(render(solo.api, pageNames, "chat"), /这个分类的页面都已隐藏/, "the placeholder explains hidden pages");
  }

  // 9. Unknown pages from a newer release are appended, not dropped.
  {
    const { api } = boot(null);
    const extended = { ...PAGE_NAMES, future: "未来功能" };
    assert.ok(render(api, extended, "chat").includes('data-page="future"'), "new page is reachable");
  }

  // 10. Reset restores the built-in layout.
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "models" } }, { pageNames });
    api.handleChange({ value: "改名了", dataset: { sidebarField: "title", id: "system" } }, { pageNames });
    api.handleAction("sidebar-reset", { dataset: {} }, { pageNames });
    const html = render(api, pageNames, "chat");
    assert.ok(html.includes('data-page="models"'));
    assert.match(html, /系统/);
    assert.ok(!html.includes("改名了"));
  }

  // 10b. A user-created empty group survives a reload (fresh module, same storage).
  {
    const { api, pageNames, store } = boot(null);
    api.handleAction("sidebar-group-add", { dataset: {} }, { pageNames });
    api.handleChange({ value: "我的收藏", dataset: { sidebarField: "title", id: "custom1" } }, { pageNames });
    const saved = store.getItem(api.STORAGE_KEY);
    const reborn = boot({ [api.STORAGE_KEY]: saved });
    assert.match(groupsView(reborn.api, pageNames), /我的收藏/, "empty custom group survives a reload");
    assert.match(render(reborn.api, pageNames, "chat"), /data-group="custom1"/, "and is still shown in the sidebar");
  }

  // 10c. prepare() lets callers check visibility before the first render.
  {
    const fresh = boot({ [boot(null).api.STORAGE_KEY]: JSON.stringify({ version: 1, groups: [], hidden: ["models"] }) });
    assert.equal(fresh.api.isHidden("models"), false, "isHidden is false before prepare()");
    fresh.api.prepare(PAGE_NAMES);
    assert.equal(fresh.api.isHidden("models"), true, "prepare() loads the saved layout");
  }

  // 10d. Deleting the group that holds pages keeps those pages reachable.
  {
    const { api, pageNames } = boot(null);
    api.handleAction("sidebar-group-delete", { dataset: { id: "model" } }, { pageNames });
    const html = render(api, pageNames, "chat");
    for (const page of ["models", "tools", "plugins"]) {
      assert.ok(html.includes(`data-page="${page}"`), `${page} survives deleting its group`);
    }
  }

  // 10e. The group cap must never orphan a page (verifier defect D1). With the
  //      maximum number of custom groups saved, every built-in page still has a
  //      home and the sidebar stays complete.
  {
    const customs = Array.from({ length: 12 }, (_, index) => ({ id: `custom${index + 1}`, title: `自定义${index + 1}`, icon: "spark", pages: [], custom: true }));
    const { api, pageNames } = boot({ "mikubot-sidebar-layout-v1": JSON.stringify({ version: 1, groups: customs, hidden: [] }) });
    const html = render(api, pageNames, "chat");
    for (const page of Object.keys(pageNames)) {
      if (page === "help") continue;
      assert.ok(html.includes(`data-page="${page}"`), `D1: ${page} stays reachable under the group cap`);
    }
    assert.equal(count(html, 'class="nav-group"'), 12, "the cap itself is still enforced");
  }

  // 10f. Deleting the LAST remaining group must not drop its pages (D2).
  {
    const { api, pageNames } = boot(null);
    for (const id of ["model", "memory", "automation", "system"]) {
      api.handleAction("sidebar-group-delete", { dataset: { id } }, { pageNames });
    }
    const html = render(api, pageNames, "chat");
    for (const page of ["models", "logs", "plugins", "knowledge", "jobs"]) {
      assert.ok(html.includes(`data-page="${page}"`), `D2: ${page} survives deleting the last group`);
    }
  }

  // 10g. The module still renders when the shell omits its escaper (D3).
  {
    const { api, pageNames } = boot(null);
    const html = api.render({ pageNames, current: "chat" });
    assert.match(html, /data-page="chat"/, "render works without an esc function");
    assert.match(api.manageBody({ pageNames }), /manage-tabs/, "manageBody works without an esc function");
    // F1: the fallback escaper must actually escape, not merely avoid throwing.
    // A hostile group title is injected through storage while `esc` is omitted.
    const hostile = JSON.stringify({ version: 1, hidden: [], groups: [
      { id: "memory", title: "<img src=x onerror=alert(1)>", icon: "chat", pages: ["persona"], custom: false },
    ] });
    const dirty = boot({ "mikubot-sidebar-layout-v1": hostile });
    const rendered = dirty.api.render({ pageNames: dirty.pageNames, current: "chat" });
    const dialogHtml = dirty.api.manageBody({ pageNames: dirty.pageNames });
    for (const [label, output] of [["render", rendered], ["manageBody", dialogHtml]]) {
      // The fallback escaper must neutralise the payload. Without an unescaped
      // "<" the injected text can never open a tag, so no live handler can exist.
      assert.ok(!output.includes("<img"), `${label}: no raw img tag without esc`);
      assert.ok(!/<img\b/i.test(output), `${label}: hostile title cannot form a tag`);
      assert.ok(output.includes("&lt;img src=x"), `${label}: hostile title is escaped by the fallback`);
    }
    // F4: a double quote must also be escaped, otherwise a title can break out of
    // an attribute value and inject a live handler. The payload fits the 20-char
    // title cap so it survives cleaning.
    const quoteBreak = JSON.stringify({ version: 1, hidden: [], groups: [
      { id: "memory", title: '" onfocus=alert(1)', icon: "chat", pages: ["persona"], custom: false },
    ] });
    const breaker = boot({ "mikubot-sidebar-layout-v1": quoteBreak });
    for (const [label, output] of [["render", breaker.api.render({ pageNames: breaker.pageNames, current: "chat" })],
                                   ["manageBody", breaker.api.manageBody({ pageNames: breaker.pageNames })]]) {
      assert.ok(output.includes("&quot;"), `${label}: double quotes are escaped`);
      // Strip well-formed attr="value" pairs: any surviving `on...=` is unquoted,
      // i.e. a real event handler rather than inert text inside a value.
      for (const tag of [...output.matchAll(/<input\b[^>]*>/gi)].map(match => match[0])) {
        const stripped = tag.replace(/[a-zA-Z-]+="[^"]*"/g, "");
        assert.ok(!/\son[a-z]+\s*=/i.test(stripped), `${label}: no live handler via quote breakout`);
      }
    }
  }

  // 10h. The icon control is pure icon: a picker of glyph buttons, no English keys.
  {
    const { api, pageNames } = boot(null);
    const body = groupsView(api, pageNames);
    assert.match(body, /class="sidebar-icon-picker"/, "icon picker rendered");
    assert.match(body, /data-action="sidebar-group-icon"/, "icon choices are buttons");
    assert.ok(!/data-sidebar-field="icon"/.test(body), "the old icon select is gone");
    // No raw icon key may appear as visible text (group ids like "memory" are
    // legitimate option VALUES, so check rendered text, not attributes).
    for (const name of ["chat", "spark", "memory", "clock", "settings", "tasks", "tools", "file", "shield", "groups", "globe", "search"]) {
      assert.ok(!body.includes(`>${name}<`), `icon key ${name} is not shown as text`);
    }
    // Choosing an icon updates the group and persists.
    api.handleAction("sidebar-group-icon", { dataset: { id: "model", icon: "shield" } }, { pageNames });
    assert.match(render(api, pageNames, "chat"), /data-group="model"[\s\S]*?data-icon="shield"/, "chosen icon reaches the sidebar");
    // Unknown icons are ignored (whitelist stays the security boundary).
    api.handleAction("sidebar-group-icon", { dataset: { id: "model", icon: "<img src=x>" } }, { pageNames });
    assert.match(render(api, pageNames, "chat"), /data-group="model"[\s\S]*?data-icon="shield"/, "invalid icon is rejected");
  }

  // 10i. Two switchable views: categories vs pages, each showing only its own
  //      controls, with the page list no longer nested inside a foldout.
  {
    const { api, pageNames } = boot(null);
    const groups = groupsView(api, pageNames);
    assert.match(groups, /manage-tabs/, "tab bar rendered");
    assert.match(groups, /管理分类/);
    assert.match(groups, /管理页面/);
    assert.match(groups, /class="manage-tab active"[^>]*data-view="groups"/, "category tab active by default");
    assert.match(groups, /sidebar-manage-row/, "category rows shown");
    assert.ok(!groups.includes("sidebar-manage-page "), "page rows hidden in the category view");
    assert.ok(!groups.includes("<details class=\"details\">"), "the old nested foldout is gone");

    const pages = pagesView(api, pageNames);
    assert.match(pages, /class="manage-tab active"[^>]*data-view="pages"/, "page tab becomes active");
    assert.match(pages, /sidebar-manage-page/, "page rows shown");
    assert.ok(!pages.includes("sidebar-manage-row"), "category rows hidden in the page view");
    assert.ok(!pages.includes("sidebar-group-add"), "add-category button belongs to the category view");
    assert.match(pages, /sidebar-manage-pages expanded/, "page list is fully expanded, not clipped");
    // The view switch is an honest toggle group: the panels are re-rendered, so
    // tab semantics (arrow keys / aria-controls) are deliberately not claimed.
    assert.ok(!/role="tab"/.test(groups), "no half-implemented tab role");
    assert.match(groups, /aria-pressed="true"[^>]*>管理分类/, "the active view is exposed via aria-pressed");
    assert.match(pages, /aria-pressed="true"[^>]*>管理页面/, "and follows the switch");
    // A long page list must not push the dialog footer off-screen.
    assert.match(experienceCss, /\.sidebar-manage-pages\.expanded\{max-height:min\(56vh,520px\);overflow:auto/, "page list is bounded and scrolls");

    // Reopening the dialog returns to the category view.
    api.handleAction("open-sidebar-manage", { dataset: {} }, { pageNames, openManage: () => {} });
    assert.equal(api._view(), "groups", "reopening resets to the category view");
    // The hidden-page badge surfaces the count without opening the page view.
    api.handleAction("sidebar-page-toggle", { dataset: { targetPage: "models" } }, { pageNames });
    assert.match(groupsView(api, pageNames), /manage-tab-badge">1 隐藏/, "hidden count badge shown");
  }

  // 10j. The icon palette dismisses like a popover: an outside click or Escape
  //      closes it, clicking inside it does not, and opening one closes others.
  {
    const { api, pickers } = boot(null);
    const first = { open: true }, second = { open: true };
    // A real click inside the palette resolves to that picker element itself.
    const inside = { closest: selector => (selector === ".sidebar-icon-picker" ? first : null) };
    const outside = { closest: () => null };
    pickers.push(first, second);

    assert.equal(api.closeIconPickers(inside), true, "clicking inside one palette closes the OTHER open palette");
    assert.equal(first.open, true, "the palette you clicked stays open");
    assert.equal(second.open, false, "sibling palettes close");
    assert.equal(api.closeIconPickers(outside), true, "an outside click closes open palettes");
    assert.equal(first.open, false);
    assert.equal(second.open, false);
    // Escape passes no target: every open palette closes.
    first.open = true; second.open = true;
    assert.equal(api.closeIconPickers(null), true, "Escape closes open palettes");
    assert.equal(first.open, false);
    assert.equal(second.open, false);
    // Nothing open: no work, no error.
    assert.equal(api.closeIconPickers(outside), false, "no-op when nothing is open");
    // The real click handler is wired to the helper, plus an Escape path.
    assert.match(appSource, /MikubotSidebar\?\.closeIconPickers\?\.\(event\.target\)/, "outside clicks dismiss the palette");
    // Escape must close the palette WITHOUT closing the dialog. keydown is the
    // cancelable event, so the guard has to preventDefault there; the dialog's
    // own "cancel" event is not reliably cancelable in Chromium.
    assert.match(appSource, /event\.key === "Escape" && window\.MikubotSidebar\?\.closeIconPickers\?\.\(null\)[\s\S]{0,160}event\.preventDefault\(\);/, "keydown consumes Escape and prevents the dialog's default close");
    assert.match(appSource, /if \(paletteEscape \|\| window\.MikubotSidebar\?\.closeIconPickers\?\.\(null\)\) \{ paletteEscape = false; event\.preventDefault\(\); return; \}/, "the cancel handler cooperates if it fires anyway");
    assert.match(appSource, /setTimeout\(\(\) => \{ paletteEscape = false; \}, 0\)/, "the one-shot flag cannot leak into a later Escape");
    // A module that fails to load must not take the whole shell down with it.
    assert.match(appSource, /layout\?\.render\s*\n?\s*\?/, "renderNavigation falls back when the module is missing");
    assert.match(appSource, /MikubotSidebar\?\.handleAction\?\.\(action,target,sidebarContext\(\)\)/, "action dispatch is null-safe");
    assert.match(appSource, /MikubotSidebar\?\.handleChange\?\.\(event\.target,sidebarContext\(\)\)/, "change dispatch is null-safe");
  }

  // 10k. Deleting categories never loses a page: they become 未分类 entries, the
  //      user's own 未分类 choices survive, and deleting the LAST one leaves zero
  //      categories instead of resurrecting the baseline.
  {
    const { api, pageNames, store } = boot(null);
    api.handleChange({ value: "", dataset: { sidebarField: "page-group", targetPage: "plugins" } }, { pageNames });
    assert.ok(looseOf(render(api, pageNames, "chat")).includes("plugins"), "plugins starts ungrouped");
    for (const id of ["model", "memory", "automation", "system"]) {
      api.handleAction("sidebar-group-delete", { dataset: { id } }, { pageNames });
    }
    const html = render(api, pageNames, "chat");
    assert.equal(groupsOf(html).size, 0, "deleting every category leaves none behind");
    assert.equal(groupOfPage(html, "plugins"), null, "pages are not re-homed into a category");
    // Every page is reachable exactly once, as a 未分类 entry.
    const seen = [...looseOf(html), ...[...groupsOf(html)].flatMap(([, pages]) => pages)];
    assert.equal(new Set(seen).size, seen.length, "no page is duplicated");
    for (const page of Object.keys(pageNames)) {
      if (page === "help") continue;
      assert.ok(html.includes(`data-page="${page}"`), `${page} survives deleting every category`);
    }
    // The deletions persist: a reload must not resurrect the built-in categories.
    const reborn = boot(store._dump());
    assert.equal(groupsOf(render(reborn.api, reborn.pageNames, "chat")).size, 0, "deleted categories stay deleted after a reload");
    // 恢复默认 brings the whole baseline back.
    reborn.api.handleAction("sidebar-reset", { dataset: {} }, { pageNames: reborn.pageNames });
    const reset = render(reborn.api, reborn.pageNames, "chat");
    assert.equal(groupsOf(reset).size, 4, "reset restores every built-in category");
    assert.deepEqual([...groupsOf(reset)].map(([id]) => id), ["model", "memory", "automation", "system"], "with their default titles and order");
  }

  // 10l. Adding a category at the cap reports why nothing happened instead of
  //      silently ignoring the click.
  {
    const customs = Array.from({ length: 12 }, (_, index) => ({ id: `custom${index + 1}`, title: `自定义${index + 1}`, icon: "spark", pages: [], custom: true }));
    const { api, pageNames } = boot({ "mikubot-sidebar-layout-v1": JSON.stringify({ version: 5, groups: customs, hidden: [], ungrouped: [] }) });
    const messages = [];
    api.handleAction("sidebar-group-add", { dataset: {} }, { pageNames, notify: (text, bad) => messages.push([text, bad]) });
    assert.equal(messages.length, 1, "the cap is reported to the user");
    assert.match(messages[0][0], /最多 12 个分类/, "with an actionable message");
    assert.equal(messages[0][1], true, "and as a warning");
    assert.equal(api.manageBody({ pageNames }).includes('data-id="custom13"'), false, "no 13th category was created");
  }

  // 11. Wiring: app.js consults the module, index.html ships it and the footer entry.
  assert.match(appSource, /layout\.render\(\{ pageNames, current: state\.page/, "the module renders the navigation");
  assert.match(appSource, /MikubotSidebar\?\.handleAction\?\.\(action,target,sidebarContext\(\)\)/);
  assert.match(appSource, /MikubotSidebar\?\.handleChange\?\.\(event\.target,sidebarContext\(\)\)/);
  assert.match(appSource, /open-sidebar-manage/);
  assert.match(appSource, /MikubotSidebar\.isPageVisible\(page\)/);
  assert.match(appSource, /MikubotSidebar\.isPageVisible\(state\.page\)/);
  assert.match(appSource, /MikubotSidebar\?\.prepare\?\.\(pageNames\)/);
  assert.match(appSource, /kind === "sidebar-manage"/);
  assert.match(indexSource, /src="\/static\/sidebar-layout\.js\?v=/);
  assert.match(indexSource, /data-action="open-sidebar-manage"/);
  assert.match(indexSource, /管理侧边栏/);
  assert.match(experienceCss, /\.nav-group-head\{/);
  assert.match(experienceCss, /\.sidebar-manage-row\{/);
  assert.match(experienceCss, /\.sidebar-icon-picker/);
  assert.match(experienceCss, /\.icon-choice/);
  // The icon palette must not hug the modal edge: it is anchored to the row with
  // a gap and an inset, flips upward on the last row, and narrows on small screens.
  assert.match(experienceCss, /\.sidebar-manage-row\{position:relative/, "row is the palette anchor");
  assert.match(experienceCss, /\.sidebar-icon-picker\{position:static\}/, "palette is not anchored to the tiny button");
  assert.match(experienceCss, /\.sidebar-icon-palette\{[^}]*top:calc\(100% \+ 6px\)/, "palette sits below the row with a gap");
  assert.match(experienceCss, /\.sidebar-icon-palette\{[^}]*right:6px/, "palette is inset from the right edge");
  assert.match(experienceCss, /\.sidebar-icon-palette\{[^}]*grid-template-columns:repeat\(6,28px\)/, "fixed-size grid, not stretched to the edge");
  assert.match(experienceCss, /\.sidebar-icon-palette::before/, "palette has a pointer");
  assert.match(experienceCss, /\.sidebar-manage-row:nth-last-child\(-n\+2\) \.sidebar-icon-palette\{top:auto;bottom:calc\(100% \+ 6px\)\}/, "the last rows open upward");
  // Contrast: the new sidebar text must not be fainter than the existing nav.
  assert.ok(!/\.nav-group-count\{[^}]*color:#9db0b4/.test(experienceCss), "the category count is not the faintest text in the sidebar");
  assert.ok(!/\.nav-group-head\{[^}]*color:#7b9096/.test(experienceCss), "the category header meets contrast");
  // Mobile tap targets must not be cancelled by the more specific new rules.
  assert.match(experienceCss, /@media\(max-width:760px\)\{\.nav-group-pages \.nav-item,\.nav-ungrouped \.nav-item\{padding-top:10px/, "mobile tap targets kept for the new entries");
  assert.match(experienceCss, /@media\(max-width:760px\)\{[\s\S]*?\.sidebar-icon-palette\{right:auto;left:6px/, "narrow screens re-anchor the palette");
  // The five pages that previously shared the fallback spark icon now have their own.
  for (const name of ["models", "logs", "statistics", "plugins", "diagnostics"]) {
    assert.ok(new RegExp(`\\b${name}:'`).test(appSource), `${name} icon added`);
  }

  // 11b. The MCP page uses the same wording as its sidebar card: no leftover
  //      "扩展工具", and the add action reads 添加MCP服务.
  {
    assert.ok(!toolsPageSource.includes("扩展工具"), "the MCP page no longer says 扩展工具");
    assert.match(toolsPageSource, /"MCP","连接你选择的 MCP 服务/, "the page title is MCP");
    assert.match(toolsPageSource, /button\("添加MCP服务","new-mcp"/, "the add action reads 添加MCP服务");
    assert.match(toolsPageSource, /empty\("tools","添加你需要的 MCP 服务"/, "the empty state reads MCP");
    assert.match(toolsPageSource, /<h2>我的 MCP 服务<\/h2>/, "the services section reads MCP");
    assert.match(toolsPageSource, /所有 MCP 调用均需本地逐次批准/, "the approval notice reads MCP");
    // The sidebar card, the breadcrumb and the page title must agree.
    assert.match(appSource, /tools:"MCP"/, "the sidebar card reads MCP");
    assert.match(PAGE_NAMES.tools, /^MCP$/, "the test page map agrees");
  }

  console.log("sidebar grouping, management dialog, persistence, fallback and escaping checks passed");
}

main();
