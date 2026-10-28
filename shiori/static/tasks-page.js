"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
async function renderTasks() {
  state.records = rows(await api(`/api/tasks?${query({scope_id:state.scope,status:state.filters.status})}`));
  if (state.filters.q) state.records = state.records.filter(item => item.title?.includes(state.filters.q));
  return heading("SMALL PROMISES, KEPT","交代的事，放在心上。","安排提醒、记录待办，随时查看每一件事的结果。",button("添加任务","new-task","primary","plus")) + listToolbar("搜索任务",[["","全部"],["pending","待处理"],["completed","已完成"],["failed","失败"],["cancelled","已取消"]]) + notice("提醒需要shiori保持运行。电脑睡眠或关机期间无法送达；QQ 提醒需要连接在线。") + (state.records.length ? `<div class="task-list">${state.records.map(item => `<article class="task-card"><div class="task-symbol">${icon(item.kind === "reminder" ? "clock" : "tasks")}</div><div class="task-main"><h3>${e(item.title)}</h3><div class="record-meta"><span>${e(label(item.kind))}</span>${item.due_at ? `<span>${e(taskTime(item))}</span>` : ""}${item.repeat && item.repeat !== "none" ? `<span>${e(label(item.repeat))}</span>` : ""}<span>${e(scopeName(item.scope_id))}</span></div>${item.error || item.result ? `<p class="task-extra">${e(item.error || (typeof item.result === "string" ? item.result : JSON.stringify(item.result)))}</p>` : ""}</div>${tag(label(item.status),item.status === "failed" ? "red" : item.status === "completed" ? "" : "gray")}<div class="record-actions">${item.kind === "todo" && item.status === "pending" ? button("完成","complete-task","small","check",`data-id="${e(item.id)}"`) : ""}${item.status === "pending" ? button("修改","edit-task","ghost small","edit",`data-id="${e(item.id)}"`) : ""}${["failed","cancelled"].includes(item.status) ? button("重试","retry-task","small","refresh",`data-id="${e(item.id)}"`) : ""}${["pending","running"].includes(item.status) ? button("取消","cancel-task","ghost small","",`data-id="${e(item.id)}"`) : ""}</div></article>`).join("")}</div>` : empty("tasks","今天，还没有交代的事","安排一个提醒，或把待办留在这里。时间到了，它会在对应的会话提醒你。","new-task","添加第一件事"));
}

function taskTime(item) { const zone = item.timezone || state.settings?.time?.timezone || "Asia/Shanghai"; return `${toLocal(item.due_at,zone).replace("T"," ")} (${zone})`; }

function toLocal(value,timezone = state.settings?.time?.timezone || "Asia/Shanghai") {
  if (!value) return "";
  const d = new Date(value); if (Number.isNaN(d.getTime())) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:timezone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).formatToParts(d).map(part=>[part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function taskModal(item = null) {
  const timezone = item?.timezone || state.settings?.time?.timezone || "Asia/Shanghai";
  openModal(item ? "调整这件事" : "交代一件小事","提醒会发送到所选会话。周期提醒保留所选时区与当地钟点。",`<div class="form-grid">${scopesField(item?.scope_id)}${field("title","要做什么",item?.title || "","text","把提醒内容写清楚。","required data-full maxlength=\"1000\"")}${select("kind","类型",["todo","reminder"],item?.kind || "todo")}${select("repeat","重复",["none","daily","weekly"],item?.repeat || "none")}${field("timezone","提醒时区",timezone,"text","IANA 名称，例如 Asia/Shanghai、America/New_York；修改全局时区不会移动已有提醒。","required data-full")}${field("due_at","当地提醒时间",toLocal(item?.due_at,timezone),"datetime-local","使用上方时区。夏令时不存在的时间会拒绝；重叠钟点采用首次出现，周期遇不存在的钟点跳过当次。","data-full")}${field("user_id","任务用户",item?.user_id || "owner","text","本地聊天填写 owner。","data-full")}</div>`,"task",item);
}
