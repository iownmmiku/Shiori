"use strict";

// Page functions use the shared authenticated shell; no page executes at load.
function toolRunLabel(status) { return toolRunNames[status] || status || "未知状态"; }

function toolRunTag(status) { return tag(toolRunLabel(status),status === "pending" || status === "unknown" ? "gold" : status === "failed" ? "red" : ["rejected","cancelled"].includes(status) ? "gray" : ""); }

function toolJSON(value,limit = 12000) {
  let text; try { text = typeof value === "string" ? value : JSON.stringify(value ?? {},null,2); } catch { text = "无法显示此项内容。"; }
  return text.length > limit ? `${text.slice(0,limit)}\n…内容过长，已截断展示。` : text;
}

function mcpArray(settings = state.settings) { return (settings.mcp_servers || []).map(item => { const clean = {...item}; delete clean.token; delete clean.token_set; return clean; }); }

function mcpDiscoveryKey(server) { return `${server.id}|${server.url}`; }

function mcpServerCard(server,status) {
  const discovered = state.mcpDiscoveries?.[mcpDiscoveryKey(server)], allowed = server.allowed_tools || [];
  return `<article class="mcp-server-card"><div class="record-card-head"><div><h3>${e(server.name)}</h3><p class="mcp-server-address mono">${e(server.url)}</p></div>${tag(server.enabled ? "已启用" : "已停用",server.enabled ? "" : "gray")}</div><div class="provider-flags">${tag(server.token_set ? "令牌已保存" : "未配置令牌","gray")}${tag(`允许 ${allowed.length} 个工具`,"gray")}${tag(status.configured === false ? "配置不完整" : "逐次审批","gold")}</div>${status.last_error ? `<p class="provider-error">${e(status.last_error)}</p>` : `<p class="help-caption">${status.last_checked_at ? `最近发现 · ${e(fmt(status.last_checked_at))} · ${number(status.tools_count)} 个工具` : "尚未发现工具。点击下方按钮才会连接这个服务。"}</p>`}<div class="record-actions">${button("发现工具（联网）","discover-mcp","small","search",`data-id="${e(server.id)}"`)}${button("编辑服务","edit-mcp","ghost small","edit",`data-id="${e(server.id)}"`)}<button class="icon-button push-right" data-action="delete-mcp" data-id="${e(server.id)}" aria-label="删除 MCP 服务">${icon("trash")}</button></div>${discovered ? `<form data-form="mcp-allowlist" class="mcp-allowlist"><input type="hidden" name="server_id" value="${e(server.id)}"><h4>哪些工具可以提出调用请求？</h4><p class="help-caption">选择仅允许模型提出请求。每一次实际执行，仍需你在本地批准。</p>${discovered.message ? `<p class="help-caption">${e(discovered.message)}</p>` : ""}<div class="mcp-discovered-list">${discovered.tools.length ? discovered.tools.map(tool => `<label class="mcp-tool-option"><input type="checkbox" name="allowed_tools" value="${e(tool.name)}" ${allowed.includes(tool.name) ? "checked" : ""}><span><strong>${e(tool.name)}</strong><small>${e(String(tool.description || "服务未提供工具说明。").slice(0,800))}</small><details><summary>参数格式</summary><pre>${e(toolJSON(tool.inputSchema,8000))}</pre></details></span></label>`).join("") : '<p class="help-caption">这个服务没有返回可用工具。</p>'}</div>${allowed.some(name => !discovered.tools.some(tool => tool.name === name)) ? '<p class="help-caption text-danger">部分已允许工具不在本次发现结果中，保存后会移除这些旧名称。</p>' : ""}<div class="section-actions"><button class="btn primary small" type="submit">保存允许列表</button></div></form>` : allowed.length ? `<div class="mcp-allowed-names"><small>已允许提出请求</small>${allowed.map(name => tag(name,"gray")).join("")}</div>` : ""}</article>`;
}

function toolRunCard(run) {
  const pending = run.status === "pending" && !run.read_only, expired = pending && run.expires_at && new Date(run.expires_at).getTime() <= Date.now();
  return `<article class="tool-run-card ${pending ? "awaiting" : ""}"><div class="record-card-head"><div><h3>${e(run.tool_name)}</h3><p>${e(run.server_name || run.server_id)} · ${e(run.user_id || "owner")}</p></div>${run.read_only ? tag("恢复历史 · 只读","gray") : toolRunTag(run.status)}</div><pre class="tool-argument-preview">${e(toolJSON(run.arguments,1800))}</pre><div class="record-meta"><span>${e(fmt(run.created_at,true))}</span><span>${(run.source_ids || []).length} 条来源</span>${pending && run.expires_at ? `<span>${expired ? "已超过审批有效时间" : `审批有效至 ${e(fmt(run.expires_at,true))}`}</span>` : ""}</div>${run.error ? `<p class="provider-error">${e(String(run.error).slice(0,1800))}</p>` : ""}${run.status === "unknown" ? '<p class="help-caption text-danger">服务可能已执行，但结果未确认。请先在目标服务核对，避免重复操作。</p>' : ""}<div class="record-actions">${pending ? button(expired ? "查看过期请求" : "审核本次调用","review-tool-run","primary small","shield",`data-id="${e(run.id)}"`) + button("拒绝","reject-tool-run","ghost small","",`data-id="${e(run.id)}"`) : button("查看详情与结果","tool-run-detail","small","file",`data-id="${e(run.id)}"`)}</div></article>`;
}

async function renderToolsBase() {
  const scopeId = state.scope, [settings,status,runs] = await Promise.all([api("/api/settings"),api("/api/mcp/status"),api(`/api/tool-runs?${query({scope_id:scopeId,status:state.filters.status})}`)]);
  state.settings = settings; state.mcpStatus = status; state.toolRuns = rows(runs); state.toolRunsScope = scopeId;
  const servers = settings.mcp_servers || [], statuses = status.servers || [], pending = state.toolRuns.filter(run => run.status === "pending" && !run.read_only).length, current = scope();
  return heading("A LITTLE HELP, ON YOUR TERMS","MCP","连接你选择的 MCP 服务。先看清工具与参数，再决定是否执行。",button("添加MCP服务","new-mcp","primary","plus")) + `<section class="tool-intro"><div class="tool-intro-copy"><div class="eyebrow">CONNECT · PROPOSE · APPROVE</div><h2>每一步，都由你决定。</h2><p>模型可以提出请求；实际调用需要本地管理员逐次批准。服务收到该次调用的参数，执行结果与任务继续状态保存在记录中；需要新的操作时会再次请求确认。</p></div><div class="tool-intro-steps"><div><span>01</span><strong>连接服务</strong><small>显式发现，选择允许工具</small></div><div><span>02</span><strong>检查请求</strong><small>核对参数与来源消息</small></div><div><span>03</span><strong>批准一次</strong><small>执行记录留在当前会话</small></div></div></section><section class="panel tool-scope-panel"><div><h3>${e(current.name)} · MCP 权限</h3><p>${current.allow_mcp && current.allow_tools !== false ? "此会话可以提出 MCP 请求，全部等待审批。" : current.allow_mcp ? "MCP 已开启，但工具总开关关闭，无法提出请求。" : "此会话尚未允许提出 MCP 请求。"}${current.enabled === false ? " 会话已停用。" : ""}</p></div>${button("调整会话权限","edit-scope","small","settings",`data-id="${e(current.id)}"`)}</section><section class="tools-section"><div class="tools-section-head"><div><h2>我的 MCP 服务</h2><p>支持 Streamable HTTP 的 JSON 响应。最多 8 个服务。</p></div>${tag(`${servers.length} / 8`,"gray")}</div>${servers.length ? `<div class="mcp-server-grid">${servers.map(server => mcpServerCard(server,statuses.find(item => item.id === server.id) || {})).join("")}</div>` : empty("tools","添加你需要的 MCP 服务","填写 HTTPS 或本机 HTTP 地址。添加后手动发现工具，选择哪些工具可以提出调用请求。","new-mcp","连接第一个 MCP 服务")}</section><section class="tools-section"><div class="tools-section-head"><div><h2>调用审批与记录</h2><p>当前会话 · ${e(scopeName(scopeId))}。执行中状态请点击刷新查看。</p></div>${button("刷新状态","refresh","ghost small","refresh")}</div><div class="toolbar tool-run-toolbar"><div class="tool-pending-count">${icon("shield")}<span>${pending ? `${pending} 项请求等待你确认` : "本页当前筛选中没有待批准请求"}</span></div><div class="filter-tabs">${[["","全部"],["pending","待批准"],["running","执行中"],["succeeded","已完成"],["unknown","结果未知"],["failed","失败"],["rejected","已拒绝"],["cancelled","已取消"]].map(([key,title]) => `<button class="filter-tab ${(state.filters.status || "") === key ? "active" : ""}" data-action="filter" data-status="${e(key)}">${e(title)}</button>`).join("")}</div></div>${state.toolRuns.length ? `<div class="tool-run-grid">${state.toolRuns.map(toolRunCard).join("")}</div>` : empty("shield","每次调用，都有一份清楚的记录","开启会话权限并允许工具后，在聊天里提出请求。模型需要调用 MCP 工具时，待批准记录会出现在这里。")}</section><p class="help-caption">当前不支持 stdio 进程或原始 SSE 传输。发现工具会连接指定服务并读取工具目录；批准执行会向它发送本次参数。</p>`;
}

async function renderTools() {
  const html = await renderToolsBase(), status = state.mcpStatus || {};
  if (!status.warning && !status.suspended) return html;
  return `<section class="tool-state-warning">${notice(status.warning || "工具记录已隔离，MCP 调用暂停。其他功能可继续使用。","warning")}${status.suspended ? `<div class="section-actions">${button("审阅后恢复 MCP 调用","resume-tool-runs","","shield")}</div>` : ""}</section>` + html;
}

function mcpModal(item = null) {
  if (!item && (state.settings.mcp_servers || []).length >= 8) { toast("最多配置 8 个 MCP 服务，请先调整现有配置。",true); return; }
  const id = item?.id || `mcp-${Date.now().toString(36)}`;
  openModal(item ? "编辑 MCP 服务" : "连接一个 MCP 服务","保存服务地址后，点击“发现工具（联网）”读取工具目录。",`<input type="hidden" name="id" value="${e(id)}"><div class="form-grid">${field("name","服务名称",item?.name || "","text","例如：个人笔记、日程服务。","required data-full maxlength=\"100\"")}${field("url","MCP 服务地址",item?.url || "","url","支持 Streamable HTTP 的 JSON 响应；HTTPS 或本机 HTTP。","required data-full placeholder=\"http://127.0.0.1:3000/mcp\"")}${field("token","Bearer Token（可选）","","password",item?.token_set ? "已保存，留空保持原值。" : "按服务要求填写，仅保存在本机，不回显。","data-full autocomplete=\"new-password\"")}</div>${toggle("enabled","启用这个服务","启用后，允许列表中的工具可供模型提出请求。",item?.enabled ?? false)}${item?.token_set ? toggle("clear_token","清除已保存令牌","保存后移除现有令牌。",false) : ""}${notice("所有 MCP 调用均需本地逐次批准。保存配置不会发现工具，也不会执行任何调用。")}`,"mcp-server",item);
}

async function saveMcpServer(form) {
  const data = getForm(form), fresh = await api("/api/settings"), servers = mcpArray(fresh), index = servers.findIndex(server => server.id === data.id), previous = index >= 0 ? servers[index] : null;
  if (data.name.trim().length > 80) throw new Error("服务名称请控制在 80 个字符以内。");
  if (data.clear_token && data.token) throw new Error("清除令牌和填写新令牌请选择一项。");
  const changedTarget = previous && previous.url !== data.url.trim(), record = {id:data.id,name:data.name.trim(),url:data.url.trim(),enabled:data.enabled,allowed_tools:changedTarget ? [] : previous?.allowed_tools || []};
  if (data.token) record.token = data.token;
  if (data.clear_token) record.clear_token = true;
  if (index < 0) servers.push(record); else servers[index] = record;
  await api("/api/settings",{method:"PUT",body:{mcp_servers:servers}});
  if (state.mcpDiscoveries && previous) delete state.mcpDiscoveries[mcpDiscoveryKey(previous)];
  closeModal(); toast(changedTarget ? "服务地址已更新，允许列表已清空，请重新发现工具。" : "服务配置已保存，请按需发现工具。" ); await bootstrap();
}

async function discoverMcp(target) {
  const id = target.dataset.id, server = (state.settings.mcp_servers || []).find(item => item.id === id); if (!server) return;
  target.disabled = true; target.innerHTML = '<span class="spinner small"></span>正在连接服务…';
  try { const result = await api("/api/mcp/discover",{method:"POST",body:{server_id:id}}); state.mcpDiscoveries ||= {}; state.mcpDiscoveries[mcpDiscoveryKey(server)] = {tools:rows(result.tools),message:result.message}; toast(result.message || `已发现 ${rows(result.tools).length} 个工具，请选择允许列表。`); await renderPage(); }
  finally { target.disabled = false; }
}

async function saveMcpAllowlist(form) {
  const data = new FormData(form), id = String(data.get("server_id")), fresh = await api("/api/settings"), servers = mcpArray(fresh), server = servers.find(item => item.id === id);
  if (!server) throw new Error("这个服务已被删除，请刷新后重试。");
  const discovery = state.mcpDiscoveries?.[mcpDiscoveryKey(server)]; if (!discovery) throw new Error("服务配置已变化，请重新发现工具。");
  server.allowed_tools = [...new Set(data.getAll("allowed_tools").map(String))].filter(name => discovery.tools.some(tool => tool.name === name));
  if (server.allowed_tools.length > 50) throw new Error("每个服务最多允许 50 个工具，请减少勾选。");
  await api("/api/settings",{method:"PUT",body:{mcp_servers:servers}}); toast("允许列表已保存。每次执行仍需单独批准。"); await bootstrap();
}

async function toolRunModal(id,review = false,requestedScope = state.scope) {
  const records = rows(await api(`/api/tool-runs?${query({scope_id:requestedScope})}`)), run = records.find(item => item.id === id);
  if (!run) throw new Error("这项调用记录已不可用，请刷新后重试。");
  const messages = rows(await api(`/api/messages?${query({scope_id:run.scope_id,limit:200})}`)), pending = review && run.status === "pending" && !run.read_only, expired = run.expires_at && new Date(run.expires_at).getTime() <= Date.now();
  const sourceIds = run.source_ids || [], sources = sourceIds.map(sourceId => messages.find(message => message.id === sourceId));
  const body = `${run.read_only ? notice("这是备份恢复的只读历史，不会再次执行，也不会作为新的办事依据。") : ""}<div class="tool-review-summary">${toolRunTag(run.status)}<dl class="detail-table"><dt>目标服务</dt><dd>${e(run.server_name || run.server_id)}</dd><dt>工具名称</dt><dd class="mono">${e(run.tool_name)}</dd><dt>会话</dt><dd>${e(scopeName(run.scope_id))}</dd><dt>请求用户</dt><dd>${e(run.user_id)}</dd><dt>创建时间</dt><dd>${e(fmt(run.created_at,true))}</dd>${run.expires_at ? `<dt>审批有效至</dt><dd>${e(fmt(run.expires_at,true))}</dd>` : ""}</dl></div><h3 class="tool-review-heading">本次参数</h3><pre class="tool-review-json">${e(toolJSON(run.arguments,Number.MAX_SAFE_INTEGER))}</pre><h3 class="tool-review-heading">来源消息</h3><p class="help-caption">展示最近 200 条中仍可读取的来源；服务执行时会收到上方参数。</p><div class="source-list">${sourceIds.length ? sourceIds.map((sourceId,index) => { const source = sources[index]; return `<div class="source-item"><small>${e(source ? `${source.user_id} · ${fmt(source.created_at)}` : sourceId)}</small><p>${e(source?.content || "原始消息已删除，或不在最近可读取范围内。")}</p></div>`; }).join("") : '<p class="help-caption">这项请求没有返回聊天来源。</p>'}</div>${pending ? notice(expired ? "本次请求已超过审批有效时间。请在对话里重新提出请求。" : "批准后会向目标服务发送上述参数并执行一次。执行结果仅在本地查看，不会自动发到 QQ。","warning") : ""}${run.result !== undefined && run.result !== null ? `<h3 class="tool-review-heading">服务返回结果</h3><pre class="tool-review-json">${e(toolJSON(run.result))}</pre>` : ""}${run.error ? `<div class="inline-error">${e(String(run.error).slice(0,6000))}</div>` : ""}${run.status === "unknown" ? notice("结果未确认，服务可能已经执行。请先核对目标服务记录，避免重复操作。","warning") : ""}`;
  openModal(pending ? "核对后，批准本次调用" : "工具调用记录",pending ? "请确认目标、参数和来源符合你的意图。" : "展示实际执行状态与服务返回内容。",body,pending && !expired ? "approve-tool-run" : "",run,"批准并执行一次");
  $("#modal").classList.add("modal-wide");
}

async function approveToolRun(form) {
  const run = state.modalRecord; if (!run) throw new Error("请重新打开调用请求。");
  const submit = $('[type="submit"]',form); if (submit) submit.innerHTML = '<span class="spinner small"></span>执行中，请稍候…';
  const result = await api(`/api/tool-runs/${encodeURIComponent(run.id)}/approve`,{method:"POST",body:{}}), record = result.run || result;
  state.pendingToolNotice = null;
  closeModal(); toast(`本次调用：${toolRunLabel(record.status)}。请查看记录。`,["failed","unknown"].includes(record.status)); await bootstrap(); await toolRunModal(run.id,false,run.scope_id);
}
