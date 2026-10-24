"use strict";

// Browser feature module. Shared UI helpers are resolved when a view opens.
// It runs directly from the portable app; Node and a build step are not needed.
window.MikubotFeatures = (() => {
function scopeModal(item = null) {
  scopeModalBase(item);
  const body = $("#modal .modal-body"), inherited = [["","继承全局"],["true","本会话开启"],["false","本会话关闭"]];
  const quiet = ["quiet_start","quiet_end"].map(name => $(`[name="${name}"]`,body)?.closest(".field")).filter(Boolean);
  body.insertAdjacentHTML("beforeend",`${toggle("always_reply","本会话每条必回复","跳过回复与否的 Planner 判断，仍遵守暂停、接收规则和工具权限。",item?.always_reply ?? false)}${toggle("use_persona_knowledge","使用人格的公开知识","关闭后，这个会话的回答与规划不再引用人格公开资料。",item?.use_persona_knowledge ?? true)}<details class="details advanced-settings"><summary>高级设置 · 触发、记忆、预算与输出</summary><div class="form-grid scope-advanced-grid">${field("bot_name","本会话机器人名称",item?.bot_name || "","text","留空继承全局名称。","maxlength=\"40\"")}${field("bot_aliases","本会话别名",(item?.bot_aliases || []).join("，"),"text","逗号或换行分隔；留空继承全局。")}${select("trigger_at","被 @ 时触发",inherited,item?.trigger_at ?? "")}${select("trigger_reply","被回复时触发",inherited,item?.trigger_reply ?? "")}${select("trigger_name","提及名称时触发",inherited,item?.trigger_name ?? "")}${select("plan_enabled","Planner 语义规划",inherited,item?.plan_enabled ?? "")}${select("proactive_reply","主动接话",inherited,item?.proactive_reply ?? "")}${field("memory_space","共享记忆空间",item?.memory_space || "","text","留空隔离；同名空间只共享显式允许的群可见记忆。","data-full maxlength=\"80\"")}${field("cooldown_seconds","接话冷却（秒）",item?.cooldown_seconds ?? 20,"number","限制连续插话。","min=\"0\" max=\"600\"")}${field("followup_seconds","话题跟进窗口（秒）",item?.followup_seconds ?? 90,"number","识别回复后的后续接话。","min=\"0\" max=\"600\"")}${field("merge_window_ms","短消息合并（毫秒）",item?.merge_window_ms ?? 0,"number","0 表示关闭。","min=\"0\" max=\"2000\"")}${field("daily_tokens","会话每日 Token 上限",item?.daily_tokens ?? 0,"number","0 表示不限，仍受全局预算限制。","min=\"0\" max=\"10000000\"")}${field("planner_min_interval_ms","Planner 最小间隔（毫秒）",item?.planner_min_interval_ms ?? "","number","留空继承；直接触发不受频率限制。","min=\"0\" max=\"60000\"")}${field("planner_daily_calls","Planner 每日调用上限",item?.planner_daily_calls ?? "","number","留空继承；0 表示不设置此项上限。","min=\"0\" max=\"100000\"")}${field("planner_daily_tokens","Planner 每日 Token 上限",item?.planner_daily_tokens ?? "","number","留空继承；0 表示不设置此项上限。","min=\"0\" max=\"10000000\"")}${field("planner_max_age_ms","Planner 消息有效期（毫秒）",item?.planner_max_age_ms ?? "","number","留空继承；过时的主动接话不再规划。","min=\"1000\" max=\"120000\"")}</div>${toggle("dictionary_learn","自动发现群词","发现候选，确认后采用。",item?.dictionary_learn ?? false)}${toggle("context_compress","整理长对话上下文","还需全局启用。",item?.context_compress ?? false)}${toggle("expression_learn","学习表达方式","候选确认前不采用。",item?.expression_learn ?? false)}${toggle("expression_use","采用已确认表达","独立于群词理解与主动用梗。",item?.expression_use ?? false)}${toggle("allow_mcp","允许提出 MCP 请求","还需允许工具总开关，调用须本地逐次批准。",item?.allow_mcp ?? false)}${toggle("output_override","单独设置本会话输出","关闭则继承全局的回复长度、括号处理与分段设置。",hasScopeOutput(item))}${outputFields(inheritedScopeOutput(item),"out_")}</details>`);
  const advanced = $(".scope-advanced-grid",body);
  quiet.forEach(node => advanced.append(node));
  for (const name of ['persona_id','activity']) {
    const node = $(`[name="${name}"]`,body)?.closest('.field');
    if(node) advanced.append(node);
  }
  body.insertAdjacentHTML('afterbegin',`<p class="help-caption">当前配置：${e(item?.profile_name || '跟随默认配置')}。统一人格和模型请前往“机器人配置与人格”；下面的高级选项仅用于单独覆盖。</p>`);
}

function hasScopeOutput(item) {
  return Boolean(item?.output || item?.settings?.output || [item,item?.settings].some(source => Object.entries(source || {}).some(([key,value]) => key.startsWith("output_") && value != null)));
}

function inheritedScopeOutput(item) {
  const result = {...(state.settings.output || {})}, local = item?.output || item?.settings?.output || {};
  for (const [key,value] of Object.entries(local)) if (value != null) result[key] = value;
  for (const source of [item?.settings,item]) for (const [key,value] of Object.entries(source || {})) if (key.startsWith("output_") && value != null) result[key.slice(7)] = value;
  return result;
}

function outputFields(output,prefix="") {
  return `<div class="output-content-rules"><h3>回复内容</h3><p class="help-caption">优先让 AI 在生成时遵守。开启上限后，超出内容会缩短展示，可在聊天中展开原始回复；代码和链接不会截成残缺片段。中文、英文都按字符计数，0 表示不限制。</p><div class="form-grid output-fields">${field(prefix+"reply_max_chars","整轮回复字符上限",output.reply_max_chars ?? 0,"number","一轮所有消息的合计上限；0 不限制。","min=\"0\" max=\"20000\" step=\"1\"")}${field(prefix+"message_max_chars","单条消息字符上限",output.message_max_chars ?? 0,"number","每条模型消息的上限；0 不限制。不是旧版分段目标长度。","min=\"0\" max=\"4000\" step=\"1\"")}${toggle(prefix+"remove_parentheses","删除括号及其中内容","移除回复正文中的中文、英文圆括号及其内容；默认关闭。代码和链接中的括号保留。",output.remove_parentheses ?? false)}</div></div><h3>消息分段与发送</h3><div class="form-grid output-fields">${toggle(prefix+"enabled","启用模型自然分段","由模型在生成时决定每条消息；空行分条，单换行留在条内。关闭后整条发送，内容限制与平台保护仍有效。",output.enabled ?? true)}${select(prefix+"mode","分段方式",[["model","模型自然分段（推荐）"],["length","旧版按长度分段"]],output.mode || "model","data-full")}${field(prefix+"platform_max_chars","平台发送长度保护上限",output.platform_max_chars ?? 1800,"number","用于通道传输保护，不是想让机器人说多少字。完整代码或链接超过此值时报告失败。","min=\"40\" max=\"20000\"")}${field(prefix+"interval_ms","发送间隔（毫秒）",output.interval_ms ?? 600,"number","模型选定的消息之间的间隔。","min=\"0\" max=\"5000\"")}${field(prefix+"max_parts","连续消息条数上限",output.max_parts ?? 8,"number","用于防刷屏，不强制分满；超出时均衡合并相邻的模型段落。平台保护可能需要更多条。","min=\"1\" max=\"20\"")}</div><details class="details"><summary>旧版长度参数（仅旧版模式生效）</summary><div class="form-grid">${field(prefix+"target_chars","旧版目标段长",output.target_chars ?? 160,"number","模型自然分段不使用此值。","min=\"20\" max=\"1000\"")}${field(prefix+"max_chars","旧版分段参考上限",output.max_chars ?? 320,"number","仅供旧模式使用，与上方回复内容上限独立。","min=\"40\" max=\"4000\"")}</div></details>`;
}

function identitySection() {
  const identity = state.settings.identity || {};
  return `<section class="panel settings-section" id="identity-section"><div class="section-title"><span>${icon("persona")}</span><div><h2>名称、触发与 Planner</h2><p>产品叫 mikubot；聊天时的称呼与人格可以独立设置。</p></div></div><form data-form="identity"><div class="form-grid">${field("name","机器人名称",identity.name || "初音未来","text","叫这个名字时可以触发机器人。","required maxlength=\"40\"")}${field("aliases","机器人别名",(identity.aliases || []).join("，"),"text","多个别名以逗号或换行分隔，例如 Miku、初音。","maxlength=\"1000\"")}</div>${toggle("respond_to_at","被 @ 时触发","与叫名字、回复消息分别控制；各会话可覆盖。",identity.respond_to_at ?? true)}${toggle("respond_to_reply","被回复时触发","识别对机器人消息的回复。",identity.respond_to_reply ?? true)}${toggle("respond_to_name","提及名称或别名时触发","按完整称呼识别；触发后仍可由 Planner 判断交谈对象。",identity.respond_to_name ?? true)}${toggle("planner_enabled","启用 Planner 语义规划","先判断在对谁说话、是否需要回复，以及准备说什么；只有回复或本轮不回复。",identity.planner_enabled ?? true)}${toggle("proactive_enabled","允许语义主动接话","同时遵守会话回应方式、安静时段与冷却；依据参与价值决定，不做概率抽签。",identity.proactive_enabled ?? true)}<p class="help-caption">每个会话可继承或覆盖这些设置。指定私聊可在会话设置中开启“每条必回复”，跳过回复与否的判断。自动化总暂停、QQ 接收规则仍然有效。</p><div class="section-actions"><button type="submit" class="btn primary">保存名称与触发</button>${button("调整会话覆盖","go-groups","ghost","groups")}</div></form></section>`;
}

function timeAwarenessSection() {
  const clock = state.settings.time || {};
  return `<section class="panel settings-section" id="time-awareness-section"><h2>现实时间</h2><form data-form="time-awareness">${toggle("awareness_enabled","感知现实时间","让对话和 Planner 获知当前日期、时间、星期与时区；关闭后不再主动提供当前时钟，提醒和日配额照常运行。",clock.awareness_enabled ?? true)}<div class="form-grid">${field("timezone","时区（IANA）",clock.timezone || "Asia/Shanghai","text","例如 Asia/Shanghai、Asia/Hong_Kong 或 America/New_York。","required data-full")}</div><div class="section-actions"><button type="submit" class="btn primary">保存时间设置</button></div></form></section>`;
}

function outputSection() {
  return `<section class="panel settings-section" id="output-section"><div class="section-title"><span>${icon("chat")}</span><div><h2>回复内容与消息分段</h2><p>模型在生成时决定消息边界；可独立设置整轮和单条长度，以及是否删除括号内容。</p></div></div><form data-form="output">${outputFields(state.settings.output || {})}<div class="section-actions"><button type="submit" class="btn primary">保存输出方式</button></div></form></section>`;
}

function plannerSection() {
  const planner = state.settings.planner || {};
  return `<section class="panel settings-section" id="planner-budget-section"><div class="section-title"><span>${icon("shield")}</span><div><h2>Planner 调用保护</h2><p>控制判断本身的频率与用量，避免只判断不聊天。</p></div></div><form data-form="planner"><div class="form-grid">${field("group_batch_ms","群聊集中判断窗口（毫秒）",planner.group_batch_ms ?? 1500,"number","普通消息积累后统一判断；0关闭。@、回复、点名与私聊不等待。","min=\"0\" max=\"10000\"")}${field("group_batch_messages","提前判断的消息数",planner.group_batch_messages ?? 4,"number","达到这个数量提前判断；否则窗口到期判断一次。","min=\"2\" max=\"20\"")}${field("min_interval_ms","最小判断间隔（毫秒）",planner.min_interval_ms ?? 1200,"number","直接触发不受频率限制；仍受日预算约束。","min=\"0\" max=\"60000\"")}${field("daily_calls","每日调用上限",planner.daily_calls ?? 300,"number","0 表示不设置此项上限。","min=\"0\" max=\"100000\"")}${field("daily_tokens","每日 Token 上限",planner.daily_tokens ?? 12000,"number","Planner 的独立上限，同时受全局模型预算控制。","min=\"0\" max=\"10000000\"")}${field("max_age_ms","主动消息有效期（毫秒）",planner.max_age_ms ?? 15000,"number","排队过久的主动接话会跳过。","min=\"1000\" max=\"120000\"")}</div><p class="help-caption">聊天请求优先于排队中的后台学习；已经发出的模型请求会等待实际结果。</p><div class="section-actions"><button class="btn primary" type="submit">保存 Planner 保护</button></div></form></section>`;
}

function explorationSection() {
  const exploration = state.settings.exploration || {};
  return `<section class="panel settings-section" id="exploration-section"><div class="section-title"><span>${icon("globe")}</span><div><h2>人格兴趣与定期探索</h2><p>获取公开的兴趣资讯与热梗，为人格积累带来源的知识。</p></div></div><form data-form="exploration">${toggle("enabled","开启定期联网探索","默认关闭。各人格还需单独启用；仅搜索其兴趣主题，不发送聊天私事。",exploration.enabled ?? false)}<div class="form-grid" style="margin-top:17px">${field("interval_hours","探索间隔（小时）",exploration.interval_hours ?? 24,"number","按人格分别记录上次尝试，重启不补发积压任务。","min=\"1\" max=\"720\" step=\"1\"")}${field("daily_searches","每天最多搜索次数",exploration.daily_searches ?? 6,"number","所有人格共用，按 UTC+8 自然日计数；失败尝试也计入。0 表示禁止搜索。","min=\"0\" max=\"100\" step=\"1\"")}${field("max_results","每次最多读取网页",exploration.max_results ?? 4,"number","减少重复抓取；搜索渠道验证失败时显示真实原因。","min=\"1\" max=\"8\" step=\"1\"")}${field("valid_days","知识有效天数",exploration.valid_days ?? 90,"number","到期后保留可管理的原条目，但不再用于回答。","min=\"1\" max=\"365\" step=\"1\"")}</div><p class="help-caption">网络笔记区分原文发布、采集日期与有效期。模型整理计入模型预算；模型不可用时只保存标明的原文摘录；弱来源、未知日期与模型整理先进入待审。探索不会自动发到群聊。</p><div class="section-actions"><button type="submit" class="btn primary">保存探索设置</button>${button("查看人格知识","go-persona-knowledge","ghost","globe")}</div></form></section>`;
}

async function renderPersonaKnowledge() {
  const selected = state.personas.find(item => item.id === state.knowledgePersona) || state.personas.find(item => item.id === scope().persona_id) || state.personas[0];
  if (!selected) return heading("PERSONA INTERESTS","人格知识","来自公开网页的兴趣资料。") + empty("persona","先创建一个人格","人格知识与聊天记忆分开管理。","go-persona","管理人格");
  state.knowledgePersona = selected.id;
  const [records,status] = await Promise.all([api(`/api/knowledge?${query({persona_id:selected.id,status:state.filters.status || ""})}`),api("/api/knowledge/status")]);
  state.personaKnowledge = rows(records);
  const items = state.personaKnowledge, schedule = status.personas?.[selected.id] || {}, effective = schedule.effective_config || {};
  const nextPlan = status.paused ? "恢复运行后检查" : !schedule.enabled ? "未开启定期探索" : status.searches_today >= status.daily_searches || schedule.remaining_searches === 0 ? "明日预算恢复后检查" : new Date(schedule.next_attempt_at).getTime() <= Date.now() ? "下次检查时尝试" : fmt(schedule.next_attempt_at,true);
  return heading("CURIOSITY WITH SOURCES","它感兴趣的事，也有自己的资料库。","公开资料先核对来源与日期，待审内容不会用于回答。",button("指定网页","explore-persona-urls","","link") + button("探索一次","explore-persona","primary","globe",`data-id="${e(selected.id)}"`)) + `<section class="panel settings-section"><div class="persona-knowledge-selector">${select("knowledge_persona","查看人格",state.personas.map(item => [item.id,item.name]),selected.id,"id=\"knowledge-persona\"")}<div><h3>${e(selected.name)}的兴趣</h3><p class="help-caption">${e((selected.interests || []).join(" · ") || "还未设置兴趣主题")}</p></div>${button("兴趣与频率","edit-persona","small","edit",`data-id="${e(selected.id)}"`)}${button("来源规则","knowledge-source-rules","small","shield")}</div><div class="record-meta"><span>全局今日 ${e(status.searches_today ?? 0)} / ${e(status.daily_searches ?? 0)}</span><span>该人格 ${e(schedule.searches_today ?? 0)} / ${e(effective.daily_searches ?? status.daily_searches ?? 0)}</span><span>有效 ${e(status.items ?? 0)} · 待审 ${e(status.pending ?? 0)}</span><span>容量 ${e(status.capacity_used ?? 0)} / ${e(status.capacity ?? 800)}</span><span>下次 · ${e(nextPlan)}</span></div>${schedule.last_message ? `<p class="help-caption">${e(schedule.last_message)}</p>` : ""}</section>${status.degraded ? notice(status.error || "人格知识暂不可用，探索已停止。","warning") : notice("采集日期不代表新闻发生日期。未知或过旧日期、弱来源、模型整理及日期冲突会进入待审。删除或拒绝会屏蔽该来源并释放条目容量；手动解除屏蔽后才可重新探索。")}<div id="persona-exploration-result" aria-live="polite"></div><div class="toolbar"><div class="filter-tabs">${[["","全部"],["active","可使用"],["pending","待审核"],["rejected","已拒绝"],["deleted","已删除"]].map(([key,title]) => `<button class="filter-tab ${(state.filters.status || "") === key ? "active" : ""}" data-action="filter" data-status="${e(key)}">${e(title)}</button>`).join("")}</div></div>${items.length ? `<div class="record-grid">${items.map(knowledgeCard).join("")}</div>` : empty("globe","还没有这类公开笔记","设置兴趣并探索公开资料；取得真实内容后才会形成笔记。","knowledge-source-rules","配置资料来源")}`;
}

function knowledgeCard(item) {
  const blocked = ["deleted","rejected"].includes(item.status), expired = item.valid_until && new Date(item.valid_until).getTime() <= Date.now();
  const statuses = {active:"可使用",pending:"待审核",deleted:"已删除",rejected:"已拒绝"};
  return `<article class="record-card persona-knowledge-card"><div class="record-card-head"><h3>${e(item.title)}</h3>${tag(statuses[item.status] || item.status,item.status === "pending" ? "gold" : blocked || expired ? "gray" : "")}</div>${knowledgeText(item)}${item.reason ? `<p class="help-caption">${e(item.reason)}</p>` : ""}${(item.review_reasons || []).length ? `<ul class="review-reasons">${item.review_reasons.map(reason => `<li>${e(reason)}</li>`).join("")}</ul>` : ""}${relevanceSummary(item)}<div class="record-meta"><span>${e(item.interest || "公开资料")}</span><span>原文发布 · ${e(item.published_at ? fmt(item.published_at,true) : "未记录")}</span>${item.updated_at ? `<span>原文更新 · ${e(fmt(item.updated_at,true))}</span>` : ""}<span>采集 · ${e(fmt(item.retrieved_at,true))}</span>${item.valid_until ? `<span>有效至 · ${e(fmt(item.valid_until,true))}${expired ? "（已过期）" : ""}</span>` : ""}<span>${item.method === "model_summary" ? "模型整理，需核对" : "原文摘录"}</span></div>${(item.event_dates || []).length ? `<div class="knowledge-dates"><strong>活动 / 播出日期候选</strong>${item.event_dates.map(event => `<p>${e(event.label)} · ${e(fmt(event.date,true))} · 待核对</p>`).join("")}</div>` : ""}${item.corroboration === "multiple_domains_candidate" ? '<p class="help-caption">发现相似标题的其他域名资料，仅作交叉核对线索，不代表事实已验证。</p>' : ""}${(item.conflicts || []).length ? `<div class="inline-error">${item.conflicts.map(conflict => `${e(conflict.label)}：${e(fmt(conflict.value))} / ${e(fmt(conflict.other_value))}`).join("<br>")}</div>` : ""}${(item.sources || []).length > 1 ? `<details class="details"><summary>查看 ${item.sources.length} 个交叉来源</summary>${item.sources.map(source => `<p><a href="${e(safeKnowledgeURL(source.url))}" target="_blank" rel="noopener noreferrer">${e(source.title || source.url)} ↗</a></p>`).join("")}</details>` : ""}<div class="record-actions"><a class="btn ghost small" href="${e(safeKnowledgeURL(item.url))}" target="_blank" rel="noopener noreferrer">${icon("link")}原文 ↗</a>${blocked ? button("解除来源屏蔽","knowledge-unblock","small","",`data-id="${e(item.id)}"`) : `${item.status === "pending" ? button("审核采用","knowledge-approve","primary small","check",`data-id="${e(item.id)}"`) : ""}${button("编辑","knowledge-edit","small","edit",`data-id="${e(item.id)}"`)}${button("拒绝","knowledge-reject","ghost small","",`data-id="${e(item.id)}"`)}<button class="icon-button push-right" data-action="delete-persona-knowledge" data-id="${e(item.id)}" aria-label="删除人格知识">${icon("trash")}</button>`}</div></article>`;
}

async function knowledgeSourcesModal() {
  const rules = await api(`/api/knowledge/sources?${query({persona_id:state.knowledgePersona})}`);
  openModal("公开资料来源","每个人格独立设置。空允许域名表示可以收集各域名，但新来源需审核。",`<div class="form-grid">${textarea("allowed_domains","允许域名",rules.allowed_domains.join("\n"),"每行一个域名。近期、有发布日期的原文摘录可以直接采用；模型整理仍需审核。")}${textarea("blocked_domains","屏蔽域名",rules.blocked_domains.join("\n"),"包括其子域名；优先于允许列表。")}${textarea("rss_urls","RSS / Atom 订阅地址",rules.rss_urls.join("\n"),"每行一个公开 HTTP/HTTPS 地址；发现文章后仍逐条读取原文。")}${textarea("manual_urls","固定公开网页",rules.manual_urls.join("\n"),"每行一个，定期探索会读取。")}</div>${toggle("use_bing","使用公开搜索发现网页","关闭后只探索订阅源与固定网页。",rules.use_bing)}`,"knowledge-sources",{persona_id:state.knowledgePersona});
}

function knowledgeEditModal(item) {
  openModal("编辑公开笔记","编辑后返回待审。保留原始来源，核对后再采用。",`<div class="form-grid">${field("title","标题",item.title,"text","","required data-full maxlength=\"240\"")}${field("interest","兴趣主题",item.interest,"text","","required maxlength=\"160\"")}${textarea("text","笔记正文",item.text,"","required maxlength=\"2000\" rows=\"7\"")}${field("published_at","原文发布日期",toLocal(item.published_at),"datetime-local","未知可以留空。")}${field("updated_at","原文更新日期",toLocal(item.updated_at),"datetime-local","不能代替发布日期。")}${field("valid_until","使用有效期",toLocal(item.valid_until),"datetime-local","过期内容需调整有效期并重新审核。","required data-full")}</div>`,"knowledge-edit",item);
}

function safeKnowledgeURL(value) { try { const url = new URL(value); return ["https:","http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "#"; } catch { return "#"; } }

function relevanceSummary(item) {
  const evidence = item.relevance;
  if (!evidence) return "";
  return `<p class="help-caption knowledge-topic-evidence">主题校验：${e(evidence.reason)}${evidence.matched_terms?.length ? ` · 正文线索：${e(evidence.matched_terms.join("、"))}` : ""}。关键词吻合不代表事实已验证，请核对公开来源。</p>`;
}

function knowledgeText(item) {
  const characters = Array.from(String(item.text || ""));
  if (characters.length <= 360) return `<p class="record-text">${e(item.text)}</p>`;
  return `<p class="record-text">${e(characters.slice(0,360).join(""))}</p><details class="details knowledge-full-text"><summary>展开后续内容 · 共 ${characters.length} 字</summary><p class="record-text">${e(characters.slice(360).join(""))}</p></details>`;
}

async function explorePersona(target) {
  const personaId = target.dataset.id || state.knowledgePersona, persona = state.personas.find(item => item.id === personaId);
  if (!persona?.interests?.length) { toast("请先给这个人格设置兴趣主题。",true); if (persona) personaModal(persona); return; }
  if (state.settings.paused) { toast("当前已暂停，请恢复运行后探索。",true); return; }
  const original = target.innerHTML; target.disabled = true; target.innerHTML = '<span class="spinner small"></span>正在寻找公开资料…';
  try { const result = await api("/api/knowledge/explore",{method:"POST",body:{persona_id:personaId}}); toast(result.message || "本次探索已结束"); if (state.page === "personaKnowledge" && state.knowledgePersona === personaId) { await renderPage(); if ($("#persona-exploration-result")) $("#persona-exploration-result").innerHTML = notice(result.message || "本次探索已结束",result.created || result.updated ? "" : "warning"); } }
  finally { if (target.isConnected) { target.disabled = false; target.innerHTML = original; } }
}

async function renderGroups() {
  state.scopes = rows(await api("/api/scopes"));
  const config = await api("/api/bot-profiles");
  state.botProfiles = config;
  const visible = state.scopes.filter(s => !state.filters.q || (s.name + s.id).toLowerCase().includes(state.filters.q.toLowerCase()));
  return heading("CHATS","收到的聊天，都在这里。","新聊天自动收集并使用默认配置。只有需要不同规则时，再单独设置。",button("修改默认配置","bot-default","primary","settings")) + listToolbar("搜索群名、账号或聊天名称",[]) + `<div class="chat-directory">${visible.map(item => {
    const platform=item.id.startsWith("qqofficial:")?"qq_official":"onebot", kind=item.id.split(":")[1], target=item.id.split(":")[2], settings=state.settings[platform] || {};
    const blocked=item.enabled===false || ((settings.chat_policy || "whitelist")==="blacklist" ? (settings[kind==="group"?"blocked_groups":"blocked_users"] || []).includes(target) : !item.id.startsWith("web:") && !(settings[kind==="group"?"allowed_groups":"allowed_users"] || []).includes(target));
    const inherited=item.profile_id === "" && !(item.profile_overrides || []).length;
    return `<article class="chat-directory-row"><div class="chat-directory-name"><h3>${e(item.name)}</h3><small>${e(item.id)}</small></div><div>${tag(blocked?"已屏蔽":"接收聊天",blocked?"gray":"")} ${tag(inherited?"跟随默认":item.profile_name || "单独配置","gray")}</div><div class="record-actions">${button("单独配置","edit-scope","small","settings",`data-id="${e(item.id)}"`)}${!inherited?button("恢复默认","scope-inherit","ghost small","",`data-id="${e(item.id)}"`):""}${button("记录","scope-chat","ghost small","",`data-id="${e(item.id)}"`)}${button(blocked?"允许聊天":"屏蔽","scope-admission","ghost small","",`data-id="${e(item.id)}" data-blocked="${blocked}"`)}</div></article>`;
  }).join("") || empty("groups","还没收到聊天","接入 QQ 后发一条消息，聊天就会出现在这里。","quick-qq","接入 QQ")}</div>`;
}

function personaExtras(item) {
  const config = item?.exploration || {};
  $("#modal .modal-body").insertAdjacentHTML("beforeend",`<details class="details advanced-settings"><summary>探索高级设置 · 频率、配额与排除主题</summary><div class="form-grid">${field("explore_interval_hours","探索间隔（小时）",config.interval_hours ?? "","number","留空继承全局。","min=\"1\" max=\"720\"")}${field("explore_daily_searches","每日探索上限",config.daily_searches ?? "","number","留空继承；0 禁止该人格探索，仍受全局上限限制。","min=\"0\" max=\"100\"")}${field("explore_valid_days","知识有效天数",config.valid_days ?? "","number","留空继承全局；旧新闻不会因新采集变成近期资讯。","min=\"1\" max=\"365\"")}${textarea("excluded_topics","排除主题",(item?.excluded_topics || []).join("\n"),"每行一个，命中标题或正文的网页不会保存。","maxlength=\"2000\" rows=\"3\"")}</div></details>`);
}

async function memoryToolsPanel() {
  if(!state.memoryToolsOpen)return `<section class="panel memory-tools-panel"><div class="panel-body">${button("打开关系与整理工具","open-memory-tools","ghost small","memory")}</div></section>`;
  const userId = state.memoryReviewUser || "owner", scopeId = state.scope;
  const [relations,organization,memories] = await Promise.all([api(`/api/memory/relations?${query({scope_id:scopeId,user_id:userId})}`),api(`/api/memory/organization?${query({scope_id:scopeId,user_id:userId})}`),api(`/api/memories?${query({scope_id:scopeId,status:"active",q:state.relationMemoryQuery,limit:200})}`)]);
  state.memoryRelations = rows(relations); state.memoryOrganization = rows(organization);
  state.relationMemories = rows(memories).filter(item => !["memory_relation","conversation_digest"].includes(item.property) && (item.visibility === "group" || item.user_id === userId));
  const excerpt = id => state.relationMemories.find(item => item.id === id)?.content || id;
  return `<details class="panel memory-tools-panel" ${state.memoryToolsOpen ? "open" : ""}><summary>关系与整理 · 可审核、可撤销</summary><div class="panel-body"><form data-form="memory-tools-user" class="memory-user-form">${field("user_id","查看用户 ID",userId,"text","本地聊天为 owner；QQ 使用平台用户 ID，不按昵称关联人物。","required")}<button class="btn small" type="submit">切换查看</button></form><div class="memory-tool-actions">${button("手动关联记忆","new-memory-relation","small","plus")}${button("分析关系（模型）","analyze-memory-relations","small","spark")}${button("生成整理建议（本地）","propose-memory-organization","small","memory")}</div><p class="help-caption">关系候选确认后最多沿两跳辅助召回。整理只建议合并相同事实、归档过期条目；应用前核对，应用后可以撤销。</p><form data-form="memory-relation-search" class="memory-user-form">${field("q","查找可关联的记忆",state.relationMemoryQuery || "","search","按关键字查找，每次最多显示200条。")}<button type="submit" class="btn small">查找</button></form><h3 class="tool-review-heading">记忆关系</h3>${state.memoryRelations.length ? `<div class="record-grid">${state.memoryRelations.map(item => `<article class="record-card"><div class="record-card-head"><h3>${e(item.label || item.content)}</h3>${tag(label(item.status),item.status === "pending" ? "gold" : "gray")}</div><p class="record-text">${e(String(excerpt(item.from_id)).slice(0,160))}</p><p class="help-caption">↔ ${e(String(excerpt(item.to_id)).slice(0,160))}</p><div class="record-meta"><span>${e(item.user_id)} · ${e(item.relation_type)}</span><span>依赖两端版本，撤销来源后失效</span></div>${item.status === "pending" ? `<div class="record-actions">${button("确认关系","approve-memory-relation","primary small","check",`data-id="${e(item.id)}"`)}${button("拒绝","reject-memory-relation","ghost small","",`data-id="${e(item.id)}"`)}</div>` : ""}</article>`).join("")}</div>` : '<p class="help-caption">还没有可见的关系候选。</p>'}<h3 class="tool-review-heading">整理建议</h3>${state.memoryOrganization.length ? `<div class="record-grid">${state.memoryOrganization.map(event => { const item = event.details || {}; return `<article class="record-card"><div class="record-card-head"><h3>${item.operation === "merge_duplicate" ? "合并重复事实" : "归档过期记忆"}</h3>${tag({pending:"待审",approved:"已应用",rejected:"已拒绝",reverted:"已撤销"}[item.status] || item.status,item.status === "pending" ? "gold" : "gray")}</div><p class="record-text">${e(item.preview || "")}</p>${item.stale ? '<p class="inline-error">依赖记忆已变化，旧预览不再显示，请重新生成建议。</p>' : `<ul class="organization-preview">${(item.before || []).map(record => `<li>${e(String(record.content || "").slice(0,240))}</li>`).join("")}</ul>`}<div class="record-actions">${item.stale ? "" : item.status === "pending" ? button("核对并应用","approve-memory-organization","primary small","check",`data-id="${e(event.id)}"`) + button("拒绝","reject-memory-organization","ghost small","",`data-id="${e(event.id)}"`) : item.status === "approved" ? button("撤销这次整理","revert-memory-organization","small","refresh",`data-id="${e(event.id)}"`) : ""}</div></article>`; }).join("")}</div>` : '<p class="help-caption">还没有整理建议，生成建议不会修改事实。</p>'}</div></details>`;
}

async function presetModal(scopeId) {
  const presets = rows(await api("/api/presets")), current = state.scopes.find(item => item.id === scopeId);
  const choices = presets.filter(item => item.id !== "private_always" || !scopeId.includes(":group:"));
  openModal("使用会话预设",`为「${current?.name || scopeId}」调整回复策略，其他记忆和工具权限保持原设置。`,`${select("preset_id","选择预设",choices.map(item => [item.id,item.name]),choices[0]?.id)}<div class="source-list preset-descriptions">${choices.map(item => `<div class="source-item"><strong>${e(item.name)}</strong><p>${e(item.description)}</p><small>将更新：${e(Object.keys(item.patch || {}).map(key => ({always_reply:"必回复",plan_enabled:"Planner",proactive_reply:"主动接话",activity:"回应方式",trigger_at:"@触发",trigger_reply:"回复触发",trigger_name:"名称触发",cooldown_seconds:"冷却",followup_seconds:"跟进窗口",use_persona_knowledge:"人格知识"}[key] || key)).join("、"))}</small></div>`).join("")}</div>`,"scope-preset",{scope_id:scopeId},"应用到此会话");
}

async function handleAction(action,target) {
  if(action === "memory-page"){state.memoryPage.offset=Number(target.dataset.offset)||0;await renderPage();return true;}
  if(action === "open-memory-tools"){state.memoryToolsOpen=true;await renderPage();return true;}
  if(action === "search-relation-memory") {
    const endpoint=target.dataset.endpoint,form=target.closest('form');
    if(!['from_id','to_id'].includes(endpoint) || !form)return true;
    const data=getForm(form),record=state.modalRecord;
    target.disabled=true;
    try {
      const page=await api(`/api/memories?${query({scope_id:record.scope_id,status:'active',q:data[endpoint+'_query'],limit:50})}`);
      if(state.modalRecord!==record || !form.isConnected)return true;
      const memories=rows(page).filter(item=>!['memory_relation','conversation_digest'].includes(item.property) && (item.visibility==='group' || item.user_id===record.user_id));
      const options=memories.map(item=>`<option value="${e(item.id)}">${e(String(item.content).slice(0,80))}</option>`).join('');
      $(`[name="${endpoint}"]`,form).innerHTML=options || '<option value="">没有匹配的可见记忆，请换个关键词</option>';
    } finally {target.disabled=false;}
    return true;
  }
  const id = target.dataset.id, knowledge = (state.personaKnowledge || []).find(item => item.id === id);
  if (action === "scope-preset") await presetModal(id);
  else if (action === "knowledge-source-rules") await knowledgeSourcesModal();
  else if (action === "knowledge-edit" && knowledge) knowledgeEditModal(knowledge);
  else if (action === "knowledge-approve" && knowledge) openModal("核对后采用公开笔记","采用后可用于当前人格的规划和回答。",`${knowledgeText(knowledge)}${notice((knowledge.review_reasons || []).join("；") || "请核对原文支持这些内容。")}<p><a href="${e(safeKnowledgeURL(knowledge.url))}" target="_blank" rel="noopener noreferrer">查看公开原文 ↗</a></p>${knowledge.conflicts?.length ? toggle("accept_conflicts","我已核对不同来源的日期冲突","采用后仍保留冲突来源供查看。",false) : ""}`,"knowledge-approve",knowledge,"采用这条笔记");
  else if (action === "knowledge-reject" && knowledge) openModal("拒绝这条公开笔记","拒绝后屏蔽该 URL，定期探索不会自动重新采用。",field("reason","拒绝原因","用户拒绝","text","可填写来源问题、过时信息或主题不符。","maxlength=\"500\""),"knowledge-reject",knowledge,"拒绝并屏蔽来源");
  else if (action === "knowledge-unblock" && knowledge) await confirmAction("解除这个来源的屏蔽？","原笔记不会恢复；后续探索可以重新采集这个 URL。",async () => { await api(`/api/knowledge/${encodeURIComponent(id)}/unblock`,{method:"POST",body:{persona_id:state.knowledgePersona}}); },"解除来源屏蔽");
  else if (action === "explore-persona-urls") openModal("指定公开资料","读取你提供的网页，仍遵守人格兴趣、来源规则和日预算。",textarea("urls","公开网页地址","","每行一个，最多8个。不会读取本机或内网地址。","required rows=\"4\""),"knowledge-explore-urls",{persona_id:state.knowledgePersona},"读取并生成待审笔记");
  else if (action === "new-memory-relation") {
    const memories = state.relationMemories || [];
    const choices = memories.map(item => [item.id,String(item.content).slice(0,80)]);
    const options=choices.length?choices:[["","请先搜索记忆"]];
    const picker=(key,title,index)=>`${field(key+'_query','查找'+title,'','search','可以分别查找两条较早的记忆；每次最多50条。','data-full')}${button('查找'+title,'search-relation-memory','ghost small','search',`data-endpoint="${key}"`)}${select(key,title,options,choices[index]?.[0] || '',"data-full required")}`;
    openModal("关联两条记忆","人物按平台和用户 ID 区分；关系审核前不会参与召回。",`<div class="form-grid">${picker('from_id','第一条记忆',0)}${picker('to_id','第二条记忆',1)}${select("relation_type","关系类型",[["related","相关事实"],["person_relation","人物关系"],["event_link","事件关联"]],"related")}${field("label","关系说明","","text","例如同一项目的后续安排。","required maxlength=\"160\"")}</div>`,"memory-relation",{scope_id:state.scope,user_id:state.memoryReviewUser || "owner"},"保存关系候选");
  }
  else if (["analyze-memory-relations","propose-memory-organization"].includes(action)) {
    target.disabled = true;
    try { const result = await api(action === "analyze-memory-relations" ? "/api/memory/relations/analyze" : "/api/memory/organization",{method:"POST",body:{scope_id:state.scope,user_id:state.memoryReviewUser || "owner"}}); state.memoryToolsOpen = true; toast(result.message || "候选已更新"); await renderPage(); }
    finally { target.disabled = false; }
  }
  else if (["approve-memory-relation","reject-memory-relation","approve-memory-organization","reject-memory-organization","revert-memory-organization"].includes(action)) {
    const reviewAction = action.split("-")[0], category = action.endsWith("relation") ? "relations" : "organization";
    await api(`/api/memory/${category}/${encodeURIComponent(id)}/review`,{method:"POST",body:{scope_id:state.scope,user_id:state.memoryReviewUser || "owner",action:reviewAction}});
    state.memoryToolsOpen = true; toast("审核结果已保存"); await renderPage();
  }
  else return false;
  return true;
}

async function handleSubmit(kind,form) {
  const data = getForm(form), record = state.modalRecord;
  if(kind === "memory-relation-search"){state.relationMemoryQuery=data.q;await renderPage();return true;}
  if (kind === "knowledge-sources") await api("/api/knowledge/sources",{method:"PUT",body:{persona_id:record.persona_id,rules:{allowed_domains:splitNames(data.allowed_domains),blocked_domains:splitNames(data.blocked_domains),rss_urls:String(data.rss_urls).split(/\r?\n/).map(value => value.trim()).filter(Boolean),manual_urls:String(data.manual_urls).split(/\r?\n/).map(value => value.trim()).filter(Boolean),use_bing:data.use_bing}}});
  else if (kind === "knowledge-edit") await api(`/api/knowledge/${encodeURIComponent(record.id)}`,{method:"PUT",body:{title:data.title,text:data.text,interest:data.interest,published_at:data.published_at ? new Date(data.published_at).toISOString() : "",updated_at:data.updated_at ? new Date(data.updated_at).toISOString() : "",valid_until:new Date(data.valid_until).toISOString()}});
  else if (kind === "knowledge-approve") await api(`/api/knowledge/${encodeURIComponent(record.id)}/approve`,{method:"POST",body:{accept_conflicts:data.accept_conflicts || false}});
  else if (kind === "knowledge-reject") await api(`/api/knowledge/${encodeURIComponent(record.id)}/reject`,{method:"POST",body:{reason:data.reason}});
  else if (kind === "knowledge-explore-urls") { const result = await api("/api/knowledge/explore",{method:"POST",body:{persona_id:record.persona_id,urls:String(data.urls).split(/\r?\n/).map(value => value.trim()).filter(Boolean)}}); toast(result.message); }
  else if (kind === "scope-preset") await api("/api/scopes/preset",{method:"POST",body:{scope_id:record.scope_id,preset_id:data.preset_id}});
  else if (kind === "memory-relation") await api("/api/memory/relations",{method:"POST",body:{...data,scope_id:record.scope_id,user_id:record.user_id}});
  else if (kind === "memory-tools-user") { state.memoryReviewUser = data.user_id.trim(); state.memoryToolsOpen = true; await renderPage(); return true; }
  else return false;
  closeModal(); toast("设置与审核结果已保存"); await bootstrap();
  return true;
}

return {scopeModal,identitySection,timeAwarenessSection,outputSection,plannerSection,explorationSection,renderPersonaKnowledge,safeKnowledgeURL,explorePersona,renderGroups,memoryToolsPanel,personaExtras,handleAction,handleSubmit};
})();
