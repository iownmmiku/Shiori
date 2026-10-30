"use strict";

// Character life is managed through Shiori's authenticated API. No AstrBot
// page bridge, additional server, external assets or timer-driven page redraw.
window.ShioriCharacterSchedule = (() => {
  const base = "/api/character-schedule/";
  const statuses = [["free", "空闲"], ["slow", "半忙"], ["busy", "忙碌"]];
  const kinds = [["festival", "节庆"], ["vacation", "休假"], ["activity", "活动"], ["anniversary", "纪念日"]];
  const tabs = [["today", "日程总览"], ["profile", "角色与周表"], ["calendar", "节庆与纪念日"], ["diary", "生活日记"], ["settings", "运行设置"], ["preview", "预演与撤销"]];
  const drafts = new Map(), pausedInputs = new Map();
  let selectedPersona = "", selectedDate = "", selectedTab = "today", diaryScope = "";
  let data = null, request = 0, previewRequest = 0, controller = null, timer = null, busy = false;
  let loadedScope = "", renderedData = null;
  let job = null, preview = null, diary = null;
  const arr = value => Array.isArray(value) ? value : [];
  const call = (path, value) => api(base + path, value === undefined ? {} : {method: "POST", body: value});
  const attr = value => e(String(value ?? ""));
  const opt = (values, value) => values.map(([id, title]) => `<option value="${attr(id)}" ${String(value) === String(id) ? "selected" : ""}>${e(title)}</option>`).join("");
  const cb = (key, title, checked, extra = "") => `<label class="life-check"><input type="checkbox" data-life-field="${attr(key)}" ${checked ? "checked" : ""} ${extra}><span>${e(title)}</span></label>`;
  const input = (key, value, title, type = "text", extra = "") => `<label class="life-cell"><span>${e(title)}</span><input type="${attr(type)}" data-life-field="${attr(key)}" value="${attr(value)}" ${extra}></label>`;
  const picker = (key, value, title, values) => `<label class="life-cell"><span>${e(title)}</span><select data-life-field="${attr(key)}">${opt(values, value)}</select></label>`;
  const note = (key, value, title, extra = "") => `<label class="life-cell life-wide"><span>${e(title)}</span><textarea data-life-field="${attr(key)}" ${extra}>${e(value || "")}</textarea></label>`;
  const statusName = status => statuses.find(([id]) => id === status)?.[1] || "尚无日程";
  const keyFor = kind => kind === "settings" ? "settings" : `${selectedPersona}|${kind === "profile" || kind === "recurrences" ? "shared" : kind === "calendar" ? selectedDate.slice(0, 4) : selectedDate}|${kind}`;
  const draftFor = (kind, fallback) => drafts.get(keyFor(kind))?.value ?? fallback;
  const staleDraft = kind => {
    const draft = drafts.get(keyFor(kind));
    return kind !== "settings" && Boolean(draft && draft.persona_key !== (data?.persona_key || ""));
  };
  // Compare only the section a form can replace. Other sections share a
  // revision, so saving one must not strand unrelated local drafts.
  const sourceFor = (kind, value) => JSON.stringify(kind === "day" ? (value?.day ? {summary: value.day.summary || "", slots: arr(value.day.slots)} : null)
    : kind === "calendar" ? arr(value?.calendar?.events).filter(row => !row.recurrence_id)
    : kind === "recurrences" ? arr(value?.recurrences) : value?.[kind] ?? null);
  function rebaseDraft(kind) {
    const draft = drafts.get(keyFor(kind));
    if (kind !== "settings" && draft && !staleDraft(kind) && draft.source === sourceFor(kind, data)) draft.revision = data.revision;
  }
  const conflictedDraft = kind => kind !== "settings" && Boolean(drafts.get(keyFor(kind)) && !staleDraft(kind) && drafts.get(keyFor(kind)).revision !== data?.revision);
  const defaultSlot = () => ({start: "00:00", end: "24:00", title: "日常活动", status: "free", note: "", locked: false});
  const names = value => String(value || "").split(/[\s,，]+/).map(x => x.trim()).filter(Boolean);
  const runningJob = item => item && ["pending", "running", "queued"].includes(item.status);
  const ownsJob = (item, value = data) => item && (!item.persona_id || item.persona_id === value?.persona_id) && (!item.persona_key || item.persona_key === value?.persona_key);
  const context = () => ({persona_id: selectedPersona, persona_key: data?.persona_key || "", revision: data?.revision, date: selectedDate, tab: selectedTab, scope_id: diaryScope});
  function checkContext(value, {scope = false, tab = false} = {}) {
    if (state.page !== "characterSchedule" || value.persona_id !== selectedPersona || value.date !== selectedDate || tab && value.tab !== selectedTab) throw new Error("角色、日期或页面已变化，请重新确认当前操作。");
    if (value.persona_key !== (data?.persona_key || "")) throw new Error("人格内容已变化，请刷新后重新确认当前操作。");
    if (value.revision !== data?.revision) throw new Error("角色日程已变化，请刷新后重新确认当前操作。");
    if (scope && value.scope_id !== diaryScope) throw new Error("日记来源已变化，请重新确认生成。");
  }
  function lifeTime(value, zone) {
    if (!value) return "时间未记录";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return "时间未记录";
    const timezone = zone || data?.profile?.timezone || data?.settings?.timezone || "Asia/Hong_Kong";
    try {return new Intl.DateTimeFormat("zh-CN", {timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(date);}
    catch {return date.toISOString().replace("T", " ").slice(0, 16) + " UTC";}
  }

  function rowValues(row) {
    const result = {};
    for (const item of $$('[data-life-field]', row)) result[item.dataset.lifeField] = item.type === "checkbox" ? item.checked : item.value;
    return result;
  }
  function slotValues(row) {
    const value = rowValues(row);
    for (const key of ["slow_min_seconds", "slow_max_seconds"]) {
      if (value[key] === "") delete value[key]; else if (value[key] !== undefined) value[key] = Number(value[key]);
    }
    if (row.dataset.day) value.days = [Number(row.dataset.day)];
    return value;
  }
  function readForm(form) {
    const kind = form.dataset.lifeKind, value = getForm(form);
    if (kind === "day") return {summary: value.summary, slots: $$('[data-life-row="slot"]', form).map(slotValues)};
    if (kind === "profile") return {...value, countries: names(value.countries).map(code => code.toUpperCase()), weekly: $$('[data-life-row="slot"]', form).map(slotValues)};
    if (kind === "calendar") return {year: Number(selectedDate.slice(0, 4)), events: $$('[data-life-row="event"]', form).map(row => {
      const event = rowValues(row); event.source_urls = String(event.source_urls || "").split(/\s+/).filter(Boolean); return event;
    })};
    if (kind === "recurrences") return $$('[data-life-row="recurrence"]', form).map(row => {
      const rule = rowValues(row);
      for (const key of ["month", "day", "duration_days"]) rule[key] = Number(rule[key]);
      for (const key of ["start_year", "end_year"]) rule[key] = rule[key] === "" ? null : Number(rule[key]);
      if (!rule.id) delete rule.id;
      return rule;
    });
    if (kind === "settings") {
      value.scope_ids = $$('[name="scope_ids"]:checked', form).map(x => x.value);
      value.diary_scopes = $$('[name="diary_scopes"]:checked', form).map(x => x.value);
      for (const key of ["slow_min_seconds", "slow_max_seconds", "daily_calls", "diary_limit", "transition_notice_minutes", "adaptive_max_delay_minutes", "active_conversation_minutes", "queue_limit", "queue_expire_hours"]) value[key] = Number(value[key]);
    }
    return value;
  }
  function remember(form) {
    if (!form?.dataset.lifeKind || busy || form.dataset.persona !== (selectedPersona || "") || form.dataset.date !== selectedDate) return;
    const key = keyFor(form.dataset.lifeKind), previous = drafts.get(key);
    drafts.set(key, {value: readForm(form), revision: previous?.revision ?? Number(form.dataset.revision || 0),
      persona_key: previous?.persona_key ?? form.dataset.personaKey ?? "",
      source: previous?.source ?? sourceFor(form.dataset.lifeKind, renderedData)});
    const marker = $("#life-draft-status"); if (marker) marker.textContent = "有未保存修改，切换页签会保留草稿。";
  }
  function identity(form, kind) {
    if (form.dataset.persona !== selectedPersona || form.dataset.date !== selectedDate || data?.persona_id !== selectedPersona || data?.date !== selectedDate || busy) throw new Error("角色或日期已变化，请重新打开当前编辑页。");
    const personaKey = drafts.get(keyFor(kind))?.persona_key ?? form.dataset.personaKey ?? "";
    if (kind !== "settings" && personaKey !== (data?.persona_key || "")) throw new Error("人格内容已变化，旧草稿不能保存到新的人格版本。请先放弃本页草稿，再重新编辑。");
    rebaseDraft(kind);
    if (conflictedDraft(kind)) throw new Error("这部分已保存内容已变化，草稿已保留。请刷新查看冲突提示，先复制需要保留的修改，再重新载入已保存内容。");
    return {persona_id: selectedPersona, persona_key: personaKey, date: selectedDate, kind, revision: drafts.get(keyFor(kind))?.revision ?? Number(form.dataset.revision || 0)};
  }
  const formStart = kind => `<form class="life-form" data-form="character-life-${kind}" data-life-kind="${kind}" data-persona="${attr(selectedPersona)}" data-persona-key="${attr(drafts.get(keyFor(kind))?.persona_key ?? data.persona_key ?? "")}" data-date="${attr(selectedDate)}" data-revision="${attr(drafts.get(keyFor(kind))?.revision ?? data.revision ?? 0)}">${staleDraft(kind) ? notice("人格内容已变化。这份草稿属于旧的人格版本，请放弃本页草稿后重新编辑；原草稿不会自动写入新人格。", "warning") : conflictedDraft(kind) ? notice("这部分已保存内容已变化，下方保留的是你的草稿，暂时不能保存。请先复制需要保留的修改，再重新载入已保存内容；其他区块的草稿不受影响。", "warning") + button("重新载入已保存内容", "life-reload-draft", "ghost small", "refresh", `data-kind="${kind}"`) : ""}`;
  const saveFooter = (title, kind) => `<div class="life-save"><button class="btn primary" type="submit" ${busy || staleDraft(kind) || conflictedDraft(kind) ? "disabled" : ""}>${e(title)}</button><small>保存前会校验时段；原有有效安排在失败时保留。</small></div></form>`;
  function masterToggleMarkup(cfg) {
    return `<div class="life-master"><span><strong>角色日程总开关</strong><small id="life-master-status">${cfg.enabled ? "已开启" : "已关闭"}</small></span><label class="switch"><input id="life-master-toggle" type="checkbox" role="switch" aria-label="启用角色日程" ${cfg.enabled ? "checked" : ""} ${busy ? "disabled" : ""}><span aria-hidden="true"></span></label></div>`;
  }
  function syncMasterToggle() {
    const toggle = $("#life-master-toggle"), status = $("#life-master-status");
    if (toggle && !busy) toggle.checked = Boolean(data?.settings?.enabled);
    if (status) status.textContent = data?.settings?.enabled ? "已开启" : "已关闭";
  }
  function slotRow(slot, weekday = "") {
    return `<fieldset class="life-slot" data-life-row="slot" ${weekday ? `data-day="${weekday}"` : ""}><legend>活动时段</legend><div class="life-slot-grid">${input("start", slot.start, "开始", "text", 'required pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]" placeholder="08:00" maxlength="5"')}${input("end", slot.end, "结束", "text", 'required pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00" placeholder="24:00" maxlength="5"')}${input("title", slot.title, "活动名称", "text", 'required maxlength="120"')}${picker("status", slot.status, "联系状态", statuses)}${cb("locked", "锁定此时段", slot.locked)}<div class="life-row-actions">${button("拆分", "life-split-slot", "ghost small")}${button("删除", "life-remove-row", "ghost small", "trash")}</div></div><details><summary>备注与独立慢回间隔</summary><div class="life-grid">${note("note", slot.note, "活动备注", 'maxlength="800"')}${input("slow_min_seconds", slot.slow_min_seconds ?? "", "半忙最短延迟（秒）", "number", 'min="5" max="7200" placeholder="继承全局"')}${input("slow_max_seconds", slot.slow_max_seconds ?? "", "半忙最长延迟（秒）", "number", 'min="5" max="7200" placeholder="继承全局"')}</div><p class="help-caption">两个延迟值一起填写，或一起留空继承全局；锁定后重新生成会保留这个时段。</p></details></fieldset>`;
  }
  function slotList(slots, day = "") {
    return `<div class="life-slots" data-life-slots ${day ? `data-day="${day}"` : ""}>${arr(slots).map(slot => slotRow(slot, day)).join("")}</div>${button("添加时段", "life-add-slot", "ghost small", "plus", day ? `data-day="${day}"` : "")}`;
  }
  function jobMarkup(item) {
    if (!item) return "";
    const running = ["pending", "running", "queued"].includes(item.status);
    return `<section class="life-job" role="status"><strong>${running ? "正在生成" : item.status === "completed" ? "生成完成" : "生成未完成"}</strong><p>${e(item.message || item.error || item.stage || "等待任务执行")}</p>${running ? `<progress max="100" ${Number.isFinite(item.progress) ? `value="${Math.min(100, Math.max(0, item.progress))}"` : ""} aria-label="生成进度"></progress>${button("取消任务", "life-cancel-job", "ghost small", "stop", `data-id="${attr(item.id)}"`)}` : ""}${arr(item.events).length ? `<details><summary>查看执行记录</summary>${arr(item.events).slice(-20).map(event => `<p><small>${e(lifeTime(event.at))}</small> ${e(event.message || event.stage || "")}</p>`).join("")}</details>` : ""}</section>`;
  }
  function currentMarkup() {
    const current = data.current || {}, slot = current.slot || current.activity || current.current_activity || {};
    const title = slot.title || current.title || (data.day ? "已保存当天安排" : "先生成或编辑日程");
    return `<section id="life-current-status" class="life-current panel"><div><span class="eyebrow">${e(current.timezone || data.profile?.timezone || data.settings?.timezone || "角色当地时间")}${current.now ? " · " + e(lifeTime(current.now, current.timezone)) : ""}</span><h2>${e(title)}</h2><p>${e(slot.note || current.note || current.summary || "日程在后台生成，正常聊天只读取已保存的安排。")}</p>${current.date && current.date !== selectedDate ? `<small>此刻状态按 ${e(current.date)} 显示；下方正在编辑 ${e(selectedDate)}。</small>` : ""}</div><div class="life-current-meta">${tag(statusName(slot.status || current.status))}${tag(data.settings?.enabled ? "日程已开启" : "日程未启用", data.settings?.enabled ? "" : "gray")}${tag(data.settings?.reply_control ? "按日程控制回复" : "保持正常回复", "gray")}${current.next_contact_at ? `<small>下次可联系：${e(lifeTime(current.next_contact_at, current.timezone))}</small>` : ""}</div></section>`;
  }
  function todayMarkup() {
    const day = draftFor("day", data.day || {summary: "", slots: [defaultSlot()]});
    return currentMarkup() + `<section class="panel life-section"><div class="life-section-head"><div><h2>${e(selectedDate)} 的生活</h2><p>每天完整覆盖 00:00～24:00；跨午夜拆成两段。可拆分现有时段后调整。</p></div>${button(data.day ? "重新生成这一天" : "生成这一天", "life-generate", "", "spark", 'data-kind="all"')}</div>${formStart("day")}${textarea("summary", "当天概述", day.summary || "", "可留空，填写当天的主要安排。", 'maxlength="1000"')}${slotList(day.slots)}${saveFooter("保存当天日程", "day")}</section>`;
  }
  function pendingMarkup() {
    const pending = arr(data.pending);
    return `<section id="life-pending-status" class="panel life-section"><h2>待回留言</h2><p>半忙与忙碌期间的留言会进入同一会话队列，恢复后通过原有聊天流程统一回复。</p>${pending.map(item => `<article class="life-snapshot"><div><strong>${e(arr(data.scopes).find(scope => scope.id === item.scope_id)?.name || item.scope_id)}</strong><small>${e(item.count ?? 0)} 条 · ${e(({pending: "等待可联系时间", uncertain: "上次处理未确认", dispatching: "正在统一回复"})[item.status] || item.status)}${item.due_at ? " · 最早尝试 " + e(lifeTime(item.due_at)) : ""}</small>${item.reason ? `<p>${e(item.reason)}</p>` : ""}</div><div class="life-row-actions">${item.status === "uncertain" ? button("核对后重试", "life-queue-retry", "small", "refresh", `data-scope="${attr(item.scope_id)}"`) : ""}${item.status !== "dispatching" ? button("清除留言", "life-queue-clear", "ghost small", "trash", `data-scope="${attr(item.scope_id)}"`) : ""}</div></article>`).join("") || '<p class="help-caption">当前没有待回留言。未开启回复控制时，聊天保持原来的回复节奏。</p>'}</section>`;
  }
  function profileMarkup() {
    const profile = draftFor("profile", data.profile || {world_type: "real", world_name: "", countries: [], timezone: data.settings?.timezone || "Asia/Hong_Kong", identity: "", lifestyle: "", calendar_notes: "", weekly: Array.from({length: 7}, (_, i) => ({...defaultSlot(), days: [i + 1]}))});
    return `<section class="panel life-section"><div class="life-section-head"><div><h2>角色生活档案</h2><p>独立于人设正文；分析时会参考人格已有的居住地、职业和作息。</p></div>${button("从人设分析", "life-generate", "", "spark", 'data-kind="profile"')}</div>${formStart("profile")}<div class="form-grid">${select("world_type", "世界观", [["real", "现实世界"], ["fanfiction", "作品 / 同人世界"], ["original", "自创世界"]], profile.world_type)}${field("world_name", "世界名称", profile.world_name, "text", "例如现实世界、原作世界名。", 'maxlength="160"')}${field("timezone", "角色时区", profile.timezone, "text", "使用 IANA 名称，例如 Asia/Tokyo。", 'required maxlength="80"')}${field("countries", "适用文化 / 地区代码", arr(profile.countries).join("，"), "text", "如 CN、JP；无明确关联可留空，不根据人设语言推断。")}${field("identity", "职业与身份", profile.identity, "text", "", 'maxlength="300"')}${textarea("lifestyle", "生活习惯", profile.lifestyle, "", 'maxlength="2500"')}${textarea("calendar_notes", "节庆与历法依据", profile.calendar_notes, "虚构日期请写明如何映射到现实日期。", 'maxlength="2500"')}</div><h3>每周基础作息</h3><p class="help-caption">每日完整覆盖 24 小时。锁定时段会在生成当天安排时保留。</p>${Array.from({length: 7}, (_, i) => `<details class="life-week" ${i === 0 ? "open" : ""}><summary>${["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i]} · ${arr(profile.weekly).filter(row => arr(row.days).includes(i + 1)).length} 个时段</summary>${slotList(arr(profile.weekly).filter(row => arr(row.days).includes(i + 1)), i + 1)}</details>`).join("")}${saveFooter("保存档案与周表", "profile")}</section>`;
  }
  function eventRow(event = {}) {
    return `<fieldset class="life-event" data-life-row="event"><legend>${e(event.title || "年度活动")}</legend>${event.id ? `<input data-life-field="id" type="hidden" value="${attr(event.id)}">` : ""}<div class="life-grid">${input("title", event.title, "名称", "text", 'required maxlength="160"')}${picker("kind", event.kind || "activity", "活动类型", kinds)}${input("start", event.start || selectedDate, "开始日期", "date", "required")}${input("end", event.end || selectedDate, "结束日期", "date", "required")}${cb("day_off", "暂停常规工作 / 上课", event.day_off)}${input("source_urls", arr(event.source_urls).join(" "), "参考链接（空格分隔）", "text")}${note("description", event.description, "活动说明及日期依据", 'maxlength="2000"')}</div><small>来源：${e(({public_calendar: "公共日历", reference: "检索参考", generated: "模型创作 / 推定", manual: "手动", recurring: "重复规则"})[event.origin] || "手动")}</small>${button("删除活动", "life-remove-row", "ghost small", "trash")}</fieldset>`;
  }
  function recurrenceRow(rule = {}) {
    return `<fieldset class="life-event" data-life-row="recurrence"><legend>${e(rule.title || "重复纪念日")}</legend>${rule.id ? `<input data-life-field="id" type="hidden" value="${attr(rule.id)}">` : ""}<div class="life-grid">${input("title", rule.title, "名称", "text", 'required maxlength="160"')}${picker("kind", rule.kind || "anniversary", "活动类型", kinds)}${input("month", rule.month ?? 1, "月份", "number", 'min="1" max="12" required')}${input("day", rule.day ?? 1, "日期", "number", 'min="1" max="31" required')}${input("duration_days", rule.duration_days ?? 1, "持续天数", "number", 'min="1" max="366" required')}${picker("leap_policy", rule.leap_policy || "skip", "2 月 29 日在平年", [["skip", "跳过"], ["feb28", "改到 2 月 28 日"]])}${cb("enabled", "启用规则", rule.enabled !== false)}${cb("day_off", "暂停常规工作 / 上课", rule.day_off)}</div><details><summary>起止年份、虚构历法及说明</summary><div class="life-grid">${input("start_year", rule.start_year ?? "", "起始年份", "number", 'min="1970" max="2100"')}${input("end_year", rule.end_year ?? "", "结束年份", "number", 'min="1970" max="2100"')}${picker("calendar", rule.calendar || "gregorian", "日期依据", [["gregorian", "公历"], ["fictional", "虚构历法映射"]])}${note("mapping", rule.mapping, "虚构日期的公历映射", 'maxlength="1500"')}${note("description", rule.description, "纪念日说明", 'maxlength="2000"')}</div></details>${button("删除规则", "life-remove-row", "ghost small", "trash")}</fieldset>`;
  }
  function calendarMarkup() {
    const calendar = draftFor("calendar", data.calendar || {events: []}), rules = draftFor("recurrences", data.recurrences || []);
    return `<section class="panel life-section"><div class="life-section-head"><div><h2>${e(selectedDate.slice(0, 4))} 年节庆与活动</h2><p>真实节庆参考适用地区日历；模型创作与推定会单独标注，休假不等于全天忙碌。</p></div>${button("整理年度活动", "life-generate", "", "spark", 'data-kind="calendar"')}</div>${arr(data.calendar?.notes).map(value => notice(String(value))).join("")}${formStart("calendar")}<div data-life-events>${arr(calendar.events).filter(row => !row.recurrence_id).map(eventRow).join("")}</div>${button("添加年度活动", "life-add-event", "ghost", "plus")}${saveFooter("保存年度活动", "calendar")}</section><section class="panel life-section"><h2>每年重复的纪念日</h2><p>生日和约定会按年展开，重新生成日历时保留。农历节日请使用年度日历。</p>${formStart("recurrences")}<div data-life-recurrences>${arr(rules).map(recurrenceRow).join("")}</div>${button("添加重复纪念日", "life-add-recurrence", "ghost", "plus")}${saveFooter("保存重复规则", "recurrences")}</section>`;
  }
  function scopeChoices(name, ids) {
    return `<div class="life-scope-list">${arr(data.scopes).map(item => `<label class="life-check"><input type="checkbox" name="${name}" value="${attr(item.id)}" ${arr(ids).includes(item.id) ? "checked" : ""}><span>${e(item.name || item.id)}<small>${e(item.id)}</small></span></label>`).join("") || "<p>收到聊天后，会话会出现在这里。</p>"}</div>`;
  }
  function settingsMarkup() {
    const cfg = draftFor("settings", data.settings || {}), models = arr(data.models).map(item => [item.id, item.name || item.model || item.id]);
    for (const id of [cfg.model_id, cfg.diary_model_id]) if (id && !models.some(([value]) => value === id)) models.push([id, "原选择不可用，请重新选择 · " + id]);
    return `<section class="panel life-section"><h2>运行设置</h2><p>先生成并检查日程，再开启。回复控制独立于生活状态，不会更改机器人原有的聊天黑名单。</p>${formStart("settings")}<div class="form-grid">${toggle("enabled", "启用角色日程", "向聊天提供已保存的角色生活状态。", cfg.enabled)}${toggle("reply_control", "按日程控制回复", "半忙时合并晚回，忙碌时暂存留言，恢复后统一回复；默认关闭。", cfg.reply_control)}${select("control_scope", "回复控制范围", [["all", "所有允许聊天的会话"], ["selected", "仅下方选择的会话"]], cfg.control_scope || "all")}${field("timezone", "默认角色时区", cfg.timezone || "Asia/Hong_Kong", "text", "角色档案中的时区优先。", 'required maxlength="80"')}${select("model_id", "日程生成模型", [["", "继承默认聊天模型"], ...models], cfg.model_id || "")}${select("diary_model_id", "日记模型", [["", "继承日程生成模型"], ...models], cfg.diary_model_id || "")}${field("daily_calls", "每日最多模型调用", cfg.daily_calls ?? 12, "number", "日程、日记与弹性判断共用；同时受全局预算限制。0 停止模型生成。", 'min="0" max="1000"')}${field("slow_min_seconds", "半忙最短延迟（秒）", cfg.slow_min_seconds ?? 60, "number", "", 'min="5" max="3600"')}${field("slow_max_seconds", "半忙最长延迟（秒）", cfg.slow_max_seconds ?? 180, "number", "新留言不会无限延长日程等待窗口。", 'min="5" max="7200"')}${toggle("auto_schedule_enabled", "自动生成每日程", "按角色当地时间生成，已有有效日程不会覆盖。", cfg.auto_schedule_enabled !== false)}${field("auto_schedule_time", "每日生成时间", cfg.auto_schedule_time || "00:00", "time")}${toggle("diary_enabled", "跨日后生成生活日记", "根据已结束日期的日程创作，不自动发送给用户。", cfg.diary_enabled !== false)}${field("diary_limit", "每个角色最多保留日记", cfg.diary_limit ?? 180, "number", "", 'min="7" max="1095"')}${toggle("diary_chat_enabled", "日记参考指定会话", "仅在下方明确选择的会话中采集；各会话分开保存，默认关闭。", cfg.diary_chat_enabled)}${toggle("adaptive_enabled", "允许对话影响生活节奏", "只能有限延后休息和个人活动，固定职责和锁定时段优先。", cfg.adaptive_enabled !== false)}${field("adaptive_max_delay_minutes", "最多延后个人安排（分钟）", cfg.adaptive_max_delay_minutes ?? 30, "number", "从原计划边界计算，不能无限续期。", 'min="1" max="120"')}${field("active_conversation_minutes", "持续聊天判断范围（分钟）", cfg.active_conversation_minutes ?? 10, "number", "", 'min="1" max="60"')}${toggle("transition_notice_enabled", "忙碌前自然告知", "正常回复时在临近安排前自然提及，不单独推送；默认关闭。", cfg.transition_notice_enabled)}${field("transition_notice_minutes", "提前告知（分钟）", cfg.transition_notice_minutes ?? 10, "number", "", 'min="1" max="60"')}</div><details class="life-week"><summary>选择回复控制会话（仅在范围为“指定会话”时使用）</summary>${scopeChoices("scope_ids", cfg.scope_ids)}</details><details class="life-week"><summary>选择日记素材会话（空列表不采集任何聊天）</summary>${scopeChoices("diary_scopes", cfg.diary_scopes)}</details><details class="life-week"><summary>联网参考与留言保留</summary><div class="form-grid">${toggle("search_enabled", "检索节庆与世界观资料", "使用 Shiori 已配置的搜索服务；未配置或失败时明确保留推定标记。", cfg.search_enabled)}${field("queue_limit", "每个会话最多待回条数", cfg.queue_limit ?? 60, "number", "达到上限时不再暂存新留言，原始聊天记录保留。", 'min="1" max="200"')}${field("queue_expire_hours", "待回留言保留时间（小时）", cfg.queue_expire_hours ?? 72, "number", "", 'min="1" max="168"')}</div></details>${saveFooter("保存运行设置", "settings")}</section>`;
  }
  const personaScopes = () => arr(data.scopes).filter(item => item.persona_id === undefined || item.persona_id === selectedPersona);
  const futureDiaryDate = () => Boolean((data?.today || data?.current?.date) && selectedDate >= (data.today || data.current.date));
  function diaryMarkup() {
    const future = futureDiaryDate();
    const shown = diary, entries = arr(data.diaries).filter(item => item.current_persona !== false && (!item.persona_key || item.persona_key === data.persona_key));
    return `<section class="panel life-section"><div class="life-section-head"><div><h2>生活日记</h2><p>日记是根据角色安排创作的回忆，单独存放，不会写成用户的真实长期记忆。</p></div>${button("生成 / 重写选中日期", "life-generate", "", "spark", 'data-kind="diary"' + (future ? ' disabled aria-describedby="life-diary-date-hint"' : ''))}</div>${future ? '<p id="life-diary-date-hint" class="help-caption">选中日期尚未结束，请选择角色当地今天之前的日期生成日记。</p>' : ''}<label class="life-cell"><span>日记来源</span><select id="life-diary-scope">${opt([["", "只参考日程"], ...personaScopes().filter(item => arr(data.settings?.diary_scopes).includes(item.id)).map(item => [item.id, item.name || item.id])], diaryScope)}</select></label><div class="life-diary-layout"><div class="life-diary-list">${entries.filter(item => String(item.scope_id || "") === diaryScope).map(item => button(`${item.date} · ${item.title || "生活日记"}`, "life-open-diary", "ghost", "", `data-date="${attr(item.date)}"`)).join("") || '<p class="help-caption">这个来源还没有日记。日期结束且保存过日程后可以生成。</p>'}</div><article class="life-diary">${shown ? `<small>${e(shown.date)}${shown.scope_id ? " · 指定会话素材" : " · 日程创作"}</small><h3>${e(shown.title || "生活日记")}</h3><p class="life-mood">${e(shown.mood || "")}</p><div class="life-prose">${e(shown.content || "")}</div>` : '<h3>选择日期查看日记</h3><p>当天及未来日期不会提前写成回忆。</p>'}</article></div></section>`;
  }
  function previewMarkup() {
    const snapshots = arr(data.snapshots || data.history);
    return `<section class="panel life-section"><h2>情景预演</h2><p>只读模拟角色当地时间，不发送消息、不暂存留言，也不调用模型。</p><form data-form="character-life-preview" data-persona="${attr(selectedPersona)}" data-persona-key="${attr(data.persona_key || "")}" data-date="${attr(selectedDate)}"><div class="form-grid">${field("time", "角色当地时间", "12:00", "time", "", "required")}${select("scope_id", "模拟会话", [["", "不指定会话"], ...personaScopes().map(item => [item.id, item.name || item.id])], state.scope || "")}</div><button class="btn" type="submit">查看回复状态</button></form><div id="life-preview-result" aria-live="polite">${preview ? previewResult(preview) : ""}</div></section><section class="panel life-section"><h2>编辑快照</h2><p>保存前自动留存安排。恢复只影响当前角色的安排，不回退聊天记录和日记。</p>${snapshots.map(item => `<article class="life-snapshot"><div><strong>${e(item.label || item.kind || "编辑快照")}</strong><small>${e(lifeTime(item.created_at || item.at))}</small>${item.persona_changed ? "<small>旧人设版本，仅供查看</small>" : ""}</div>${button("恢复这份安排", "life-restore", "small", "refresh", `data-id="${attr(item.id)}" ${item.persona_changed ? "disabled" : ""}`)}</article>`).join("") || '<p class="help-caption">保存档案、周表或活动后，这里会出现编辑前的快照。</p>'}</section>`;
  }
  function previewResult(value) {
    const current = value.current || {}, policy = value.policy || {}, slot = current.slot || {};
    return `<div class="life-preview"><strong>${e(slot.title || current.title || "所选时间没有已保存日程")}</strong><p>${e(statusName(current.status || slot.status))} · ${e(current.timezone || data.profile?.timezone || "")}</p>${policy.active === false ? "<p>此模拟会话不启用日程回复控制。</p>" : ""}${current.next_contact_at ? `<p>下次可联系：${e(lifeTime(current.next_contact_at, current.timezone))}</p>` : ""}${current.status === "slow" ? `<p>半忙延迟：${e(current.slow_min_seconds)}～${e(current.slow_max_seconds)} 秒</p>` : ""}${policy.reason ? `<p>${e(policy.reason)}</p>` : ""}<small>只读预演，未调用模型或发送消息。</small></div>`;
  }
  async function render() {
    clearTimeout(timer); controller?.abort(); controller = new AbortController();
    const ticket = ++request, signal = controller.signal;
    let next, source = diaryScope, entry = null;
    try {
      next = await api(base + "state?" + query({persona_id: selectedPersona || undefined, date: selectedDate || undefined}), {signal});
      if (ticket !== request) return "";
      if (source && (!arr(next.settings?.diary_scopes).includes(source) || !arr(next.scopes).some(item => item.id === source && (item.persona_id === undefined || item.persona_id === next.persona_id)))) source = "";
      if (selectedTab === "diary" && next.persona_id) {
        const value = await api(base + "diary?" + query({persona_id: next.persona_id, date: next.date, scope_id: source}), {signal});
        if (ticket !== request) return "";
        entry = Object.hasOwn(value, "diary") ? value.diary : value;
        if (entry?.persona_key && entry.persona_key !== next.persona_key) entry = null;
      }
    } catch (error) {
      if (ticket !== request) return "";
      // A failed selection must not redirect a still-visible form's edits to
      // the persona/date whose state could not be loaded.
      if (data) {selectedPersona = data.persona_id || ""; selectedDate = data.date || ""; diaryScope = loadedScope;}
      throw error;
    }
    if (data && (next.persona_id !== data.persona_id || next.persona_key !== data.persona_key || next.date !== data.date || next.revision !== data.revision)) preview = null;
    data = next; selectedPersona = next.persona_id || ""; selectedDate = next.date || selectedDate;
    for (const kind of ["day", "profile", "calendar", "recurrences"]) rebaseDraft(kind);
    renderedData = next;
    diaryScope = loadedScope = source; diary = entry;
    if (!ownsJob(job, next)) job = null;
    const availableJobs = arr(next.jobs).filter(item => ownsJob(item, next));
    job = availableJobs.find(runningJob) || availableJobs.find(item => item.id === job?.id) || (runningJob(job) ? job : null);
    const usage = next.usage || {}, cfg = next.settings || {};
    const title = heading("CHARACTER LIFE", "角色日程", "安排角色的生活、节庆与日记，在独立页面查看和管理。", masterToggleMarkup(cfg) + button("刷新", "life-refresh", "", "refresh"));
    if (!arr(next.personas).length) {job = null; return title + empty("persona", "先创建一个人格", "角色日程跟随人格保存，在机器人配置中创建人格后即可使用。", "go-persona", "前往机器人配置");}
    return title + `<div class="life-page"><section class="panel life-selector"><label class="life-cell"><span>管理人格</span><select id="life-persona" aria-label="管理人格">${opt(arr(next.personas).map(item => [item.id, item.name || item.id]), selectedPersona)}</select></label><label class="life-cell"><span>角色当地日期</span><input id="life-date" type="date" value="${attr(selectedDate)}" min="1970-01-01" max="2100-12-31" required></label><div class="life-usage"><strong>今日生成 ${e(usage.calls ?? usage.total ?? 0)} / ${e(usage.limit ?? cfg.daily_calls ?? 12)}</strong><small>${e(usage.timezone || cfg.timezone || "")} · 受全局预算限制</small></div></section><nav class="life-tabs" aria-label="日程功能">${tabs.map(([id, title]) => `<button type="button" class="life-tab ${selectedTab === id ? "active" : ""}" data-action="life-tab" data-tab="${id}" aria-current="${selectedTab === id ? "page" : "false"}">${title}</button>`).join("")}</nav><div class="life-draft-bar"><small id="life-draft-status">${drafts.has(keyFor(({today: "day", profile: "profile", calendar: "calendar", settings: "settings"})[selectedTab] || selectedTab)) ? "有未保存修改，切换页签会保留草稿。" : "修改后点击保存；日程和日记不会自动发送给用户。"}</small>${button("放弃本页草稿", "life-discard", "ghost small")}</div><div id="life-job-status">${jobMarkup(job)}</div><div id="life-page-error" class="inline-error" role="alert" hidden></div><div id="life-pane">${({today: () => todayMarkup() + pendingMarkup(), profile: profileMarkup, calendar: calendarMarkup, diary: diaryMarkup, settings: settingsMarkup, preview: previewMarkup})[selectedTab]()}</div></div>`;
  }
  function refreshButton(enabled) {
    busy = !enabled;
    if (!enabled) {
      for (const item of $$('.life-page input,.life-page select,.life-page textarea')) {
        if (!pausedInputs.has(item)) pausedInputs.set(item, item.disabled);
        item.disabled = true;
      }
    } else {
      for (const [item, disabled] of pausedInputs) item.disabled = disabled;
      pausedInputs.clear();
    }
    const master = $("#life-master-toggle"); if (master) master.disabled = !enabled;
    for (const item of $$('.life-page [type="submit"],.life-page [data-action="life-generate"]')) {
      const kind = item.closest('form[data-life-kind]')?.dataset.lifeKind;
      item.disabled = !enabled || item.dataset.kind === "diary" && futureDiaryDate() || staleDraft(kind) || conflictedDraft(kind);
    }
  }
  async function mutate(action, payload) {
    if (busy) throw new Error("请等待当前操作完成。");
    refreshButton(false);
    try { return await call(action, payload); }
    finally { refreshButton(true); }
  }
  async function handleAction(action, target) {
    if (!action.startsWith("life-")) return false;
    if (busy) throw new Error("正在保存，请稍后再操作。");
    if (!["life-tab", "life-refresh"].includes(action) && (data?.persona_id !== selectedPersona || data?.date !== selectedDate)) throw new Error("角色或日期正在加载，请稍后再操作。");
    if (action === "life-tab") { selectedTab = target.dataset.tab; if (!tabs.some(([id]) => id === selectedTab)) selectedTab = "today"; await renderPage(); }
    else if (action === "life-refresh") await renderPage();
    else if (action === "life-queue-clear" || action === "life-queue-retry") {
      const retry = action === "life-queue-retry", scopeId = target.dataset.scope;
      await confirmAction(retry ? "已核对平台消息，重试处理？" : "清除这个会话的待回留言？", retry ? "上次处理可能已发送回复。请先核对平台，确认后才重新处理这一批留言。" : "只清除等待队列，不删除原始聊天记录。", async () => {await mutate("queue", {scope_id: scopeId, action: retry ? "retry" : "clear"}); await renderPage();}, retry ? "确认重试" : "清除留言");
    }
    else if (action === "life-discard" || action === "life-reload-draft") {
      const reload = action === "life-reload-draft";
      const pageKinds = ({today: ["day"], profile: ["profile"], calendar: ["calendar", "recurrences"], settings: ["settings"]})[selectedTab] || [];
      if (reload && !pageKinds.includes(target.dataset.kind)) throw new Error("请选择当前页的草稿。");
      const origin = context(), keys = (reload ? [target.dataset.kind] : pageKinds).map(keyFor);
      const versions = keys.map(key => drafts.get(key));
      await confirmAction(reload ? "重新载入这个区块的已保存内容？" : "放弃当前页的草稿？", reload ? "这个区块的未保存修改会被放弃，请先复制需要保留的内容。其他区块的草稿会保留。" : "已保存的日程会保留。", async () => {
        checkContext(origin, {tab: true});
        if (keys.some((key, index) => drafts.get(key) !== versions[index])) throw new Error("草稿已变化，请重新确认放弃。");
        for (const key of keys) drafts.delete(key);
        await renderPage();
      }, reload ? "放弃此草稿并重新载入" : "放弃草稿");
    }
    else if (action === "life-add-slot") {
      const form = target.closest("form"), list = target.previousElementSibling;
      const last = $$('[data-life-row="slot"]', list).at(-1), start = last ? rowValues(last).end : "00:00";
      list.insertAdjacentHTML("beforeend", slotRow({...defaultSlot(), start}, target.dataset.day)); remember(form);
    }
    else if (action === "life-split-slot") {
      const row = target.closest('[data-life-row="slot"]'), slot = slotValues(row);
      const minute = clock => {const [h, m] = String(clock).split(":").map(Number); return h * 60 + m;};
      const start = minute(slot.start), end = minute(slot.end);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < 2) throw new Error("这个时段太短或时间无效，无法继续拆分。");
      const middle = Math.floor((start + end) / 2), clock = `${String(Math.floor(middle / 60)).padStart(2, "0")}:${String(middle % 60).padStart(2, "0")}`;
      const first = {...slot, end: clock}, second = {...slot, start: clock};
      row.insertAdjacentHTML("beforebegin", slotRow(first, row.dataset.day) + slotRow(second, row.dataset.day));
      const form = row.closest("form"); row.remove(); remember(form);
    }
    else if (action === "life-remove-row") { const row = target.closest("[data-life-row]"), form = row.closest("form"); row.remove(); remember(form); }
    else if (action === "life-add-event") { const form = target.closest("form"); $("[data-life-events]", form).insertAdjacentHTML("beforeend", eventRow()); remember(form); }
    else if (action === "life-add-recurrence") { const form = target.closest("form"); $("[data-life-recurrences]", form).insertAdjacentHTML("beforeend", recurrenceRow()); remember(form); }
    else if (action === "life-generate") {
      const kind = target.dataset.kind, origin = context();
      const perform = async () => {
        checkContext(origin, {scope: kind === "diary"});
        const {persona_id, persona_key, revision, date, scope_id} = origin;
        job = {...await mutate("generate", {persona_id, persona_key, revision, date, kind, ...(kind === "diary" ? {scope_id} : {})}), persona_id, persona_key};
        toast("生成任务已创建，进度会显示在当前页面。"); await renderPage();
      };
      if (data.day || data.profile || kind === "diary") await confirmAction("生成选中的角色安排？", "会使用所选模型并计入额度。锁定时段会保留；手动编辑前可通过快照恢复。未保存草稿会继续保留，需放弃草稿才能查看新安排。", perform, "开始生成");
      else await perform();
    }
    else if (action === "life-cancel-job") { await mutate("cancel", {id: target.dataset.id}); if (job?.id === target.dataset.id) job.status = "cancelled"; await renderPage(); }
    else if (action === "life-open-diary") {
      selectedDate = target.dataset.date; diary = preview = null; await renderPage();
    }
    else if (action === "life-restore") {
      const origin = context();
      const payload = {persona_id: selectedPersona, persona_key: data.persona_key || "", id: target.dataset.id, revision: data.revision};
      await confirmAction("恢复这份角色安排？", "当前安排会先保存为新快照。聊天记录、待回复消息及日记不会回退。", async () => {
        checkContext(origin);
        await mutate("restore", payload); for (const key of drafts.keys()) if (key.startsWith(payload.persona_id + "|")) drafts.delete(key);
        preview = null; toast("角色安排已恢复"); await renderPage();
      }, "恢复安排");
    }
    return true;
  }
  async function handleSubmit(kind, form) {
    if (!kind.startsWith("character-life-")) return false;
    const action = kind.slice("character-life-".length);
    if (action === "preview") {
      if (form.dataset.persona !== selectedPersona || form.dataset.date !== selectedDate) throw new Error("角色或日期已变化，请重新预演。");
      if (form.dataset.personaKey !== (data?.persona_key || "")) throw new Error("人格内容已变化，请重新打开预演页。");
      const origin = context(), value = getForm(form), ticket = request, previewTicket = ++previewRequest;
      const result = await call("preview", {persona_id: origin.persona_id, persona_key: origin.persona_key, date: origin.date, time: value.time, scope_id: value.scope_id});
      if (ticket === request && previewTicket === previewRequest && origin.persona_id === selectedPersona && origin.date === selectedDate && origin.persona_key === data?.persona_key && selectedTab === "preview" && state.page === "characterSchedule") {
        preview = result; const output = $("#life-preview-result"); if (output) output.innerHTML = previewResult(result);
      }
      return true;
    }
    remember(form);
    const payload = identity(form, action), value = readForm(form), draftKey = keyFor(action);
    try {
      await mutate(action === "settings" ? "settings" : "save", action === "settings" ? value : {...payload, value});
      drafts.delete(draftKey); toast("角色日程已保存"); await renderPage();
    } catch (error) {
      const node = $("#life-page-error"); if (node) { node.textContent = error.message; node.hidden = false; }
      throw error;
    }
    return true;
  }
  async function handleChange(target) {
    if (target.id === "life-master-toggle") {
      const previous = Boolean(data?.settings?.enabled), desired = Boolean(target.checked);
      if (busy) {target.checked = previous; throw new Error("正在保存，请稍后再切换。");}
      try {
        const result = await mutate("settings", {enabled: desired});
        data.settings = {...data.settings, ...(result?.settings || {enabled: desired})};
        const draft = drafts.get("settings");
        if (draft) draft.value = {...draft.value, enabled: Boolean(data.settings.enabled)};
        syncMasterToggle();
      } catch (error) {
        target.checked = previous; syncMasterToggle(); throw error;
      }
      await renderPage();
      toast(data.settings.enabled ? "角色日程已开启" : "角色日程已关闭，恢复普通聊天流程。");
      return true;
    }
    if (target.id === "life-persona" || target.id === "life-date") {
      if (busy) {target.value = target.id === "life-persona" ? selectedPersona : selectedDate; throw new Error("正在保存，请稍后切换。");}
      if (target.id === "life-persona") { selectedPersona = target.value; selectedDate = ""; } else selectedDate = target.value;
      preview = diary = null; await renderPage(); return true;
    }
    if (target.id === "life-diary-scope") {
      if (busy) {target.value = diaryScope; throw new Error("正在保存，请稍后切换。");}
      diaryScope = target.value; diary = null; await renderPage();
      return true;
    }
    const form = target.closest?.('form[data-life-kind]'); if (form) {remember(form); return true;}
    return false;
  }
  function handleInput(target) { const form = target.closest?.('form[data-life-kind]'); if (form) remember(form); }
  function afterRender() {
    clearTimeout(timer);
    if (state.page !== "characterSchedule" || !state.authenticated || state.stopped) return;
    if (busy) refreshButton(false);
    const running = Boolean(runningJob(job));
    if (!running && selectedTab !== "today") return;
    const id = job?.id, pid = selectedPersona, day = selectedDate, personaKey = data?.persona_key, ticket = request;
    timer = setTimeout(async () => {
      try {
        if (state.page !== "characterSchedule" || document.hidden || !state.authenticated || busy || ticket !== request || pid !== selectedPersona || day !== selectedDate || personaKey !== data?.persona_key) return;
        if (!running) {
          const value = await api(base + "state?" + query({persona_id: pid, date: day}));
          if (state.page !== "characterSchedule" || selectedTab !== "today" || ticket !== request || pid !== selectedPersona || day !== selectedDate || busy) return;
          if (value.persona_id !== pid || value.persona_key !== personaKey) {diary = preview = null; await renderPage(); return;}
          data = value;
          syncMasterToggle();
          const current = $("#life-current-status"), pending = $("#life-pending-status");
          if (current && !current.contains?.(document.activeElement)) current.outerHTML = currentMarkup();
          if (pending && !pending.contains?.(document.activeElement)) pending.outerHTML = pendingMarkup();
          const nextJob = arr(value.jobs).find(item => ownsJob(item, value) && runningJob(item));
          if (nextJob) {job = nextJob; const node = $("#life-job-status"); if (node) node.innerHTML = jobMarkup(job);}
          return;
        }
        const result = await api(base + "job?" + query({id}));
        if (job?.id !== id || state.page !== "characterSchedule" || ticket !== request || pid !== selectedPersona || day !== selectedDate || personaKey !== data?.persona_key || busy) return;
        const nextJob = result.job || result;
        if (!ownsJob(nextJob)) {job = null; await renderPage(); return;}
        job = nextJob;
        const node = $("#life-job-status"); if (node) node.innerHTML = jobMarkup(job);
        if (!["pending", "running", "queued"].includes(job.status)) {
          if (job.status === "completed") {diary = null; toast("角色安排已生成；有草稿时先保存或放弃草稿再查看新内容。");}
          await renderPage();
        }
      } catch (error) { if (state.page === "characterSchedule" && ticket === request && pid === selectedPersona && day === selectedDate) {const node = running ? $("#life-job-status") : $("#life-page-error"); if (node) {node.textContent = `暂时无法更新${running ? "任务进度" : "当前状态"}：${error.message}；稍后会继续查询。`;node.hidden = false;}} }
      finally {afterRender();}
    }, running ? 2000 : 15000);
  }
  return {render, handleAction, handleSubmit, handleChange, handleInput, afterRender};
})();
