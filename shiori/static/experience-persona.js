"use strict";

window.ShioriExperiencePersona={create(runtime,helpers){
  const {safeLink}=helpers;
    function distillChooser() {
      runtime.distillDraft = null;
      openModal("创建人格","生成过程会使用你配置的模型并计入用量。先确认资料，再生成草稿。",`<div class="distill-options"><button type="button" class="distill-option" data-action="persona-generate">${icon("spark")}<strong>按描述直接生成</strong><small>写下要求，直接生成人格草稿，不需要资料或 ZIP。</small></button><button type="button" class="distill-option" data-action="distill-character">${icon("search")}<strong>已有作品角色</strong><small>输入作品和角色，搜索资料并确认身份、版本，生成聊天人格与 Skill 包。</small></button><button type="button" class="distill-option" data-action="distill-corpus">${icon("upload")}<strong>已有语料 / 真人表达</strong><small>上传 ZIP，选择文件与目标说话者，仅提炼人设描写，不生成 Skill。</small></button></div><p class="sensitive-note">仅选中的文本发送到你配置的模型服务。真人语料不自动联网补全生平，不推断材料中不存在的私事。</p>`);
    }

    function characterForm() {
      const previous=runtime.distillDraft || {};
      openModal("检索作品角色","先找到角色，再选择要蒸馏的对象。无需准备资料链接。",`<div class="form-grid">${field("query","角色名或作品名",previous.query || "","text","例如：和泉纱雾；也可以输入“埃罗芒阿老师”，从角色列表中选择。","required data-full maxlength=\"160\"")}</div><details class="details"><summary>补充作品范围或自备资料（可选）</summary>${field("work","限定作品",previous.work || "","text","同名角色较多时再填写，通常可留空。","maxlength=\"160\"")}${textarea("source_urls","自备资料链接","","每行一个，最多6个。仅在希望使用指定资料时填写，同时补充作品名称。","rows=\"3\" maxlength=\"18005\"")}</details>`,"distill-search",null,"检索角色");
    }

    function generateForm() {
      openModal("按描述生成人格","输入你的要求，由当前模型生成可编辑草稿；不检索网页，不生成 Skill。",`<div class="form-grid">${field("name","人格名称（可选）","","text","留空使用“自定义人格”。","data-full maxlength=\"100\"")}${textarea("description","希望它是什么样的人？","","例如：喜欢天文、说话简洁，有自己的观点；我疲惫时先听我说，不急着讲道理。","required rows=\"7\" maxlength=\"6000\"")}</div><p class="help-caption">描述会发送给已配置的模型，计入用量。生成后可预览、试聊，再保存。</p>`,"persona-generate",null,"生成人格草稿");
    }

    async function profilePanel() {
      const config = await api('/api/bot-profiles');
      state.botProfiles = config;
      return `<section class="panel"><div class="panel-head"><div><h2>机器人配置</h2><p>统一选择人格、模型和回复规则。新聊天继承默认配置；记忆隔离不受影响。</p></div>${button("新增配置","profile-new","small","plus")}</div><div class="panel-body"><div class="record-grid">${config.items.map(p => `<article class="record-card"><h3>${e(p.name)} ${p.id === config.default_id ? tag("默认配置") : ""}</h3><p>人格：${e(personaName(p.values.persona_id ?? "default"))}</p><p class="help-caption">${config.scopes.filter(s => s.profile_id === p.id || s.profile_id === "" && p.id === config.default_id).length} 个聊天使用</p><div class="record-actions">${button("编辑","profile-edit","small","edit",`data-id="${e(p.id)}"`)}${button("绑定聊天","profile-bind","small","groups",`data-id="${e(p.id)}"`)}${p.id !== config.default_id ? button("删除","profile-delete","ghost small","trash",`data-id="${e(p.id)}"`) : ""}</div></article>`).join("")}</div>${config.scopes.some(s => s.profile_id == null) ? notice("已有聊天保留原来的独立设置。点击“绑定聊天”可批量迁入统一配置，绑定后清除这些聊天的配置覆盖；聊天记录和记忆保留。") : ""}</div></section>`;
    }

    function profileForm(id) {
      const config=state.botProfiles, item=config.items.find(p=>p.id===id), values=item?.values || {};
      openModal(item ? "编辑机器人配置" : "新增机器人配置","保存后对继承此配置的聊天生效；单独覆盖的设置保留。",`<input type="hidden" name="id" value="${e(item?.id || '')}"><div class="form-grid">${field("name","配置名称",item?.name || "","text","例如：默认陪伴、工作群。","required data-full maxlength=\"80\"")}${select("persona_id","人格",[["","不使用人格"],...state.personas.map(p=>[p.id,p.name])],values.persona_id ?? "default")}${select("model_id","聊天模型",[["","跟随全局聊天模型"],...(state.settings.providers || []).filter(p=>p.model_type!=="embedding").map(p=>[p.id,p.name || p.model])],values.model_id || "")}${select("activity","群聊回应方式",[["quiet","仅被提及"],["balanced","适时回应"],["active","积极接话"]],values.activity || "balanced")}</div>${toggle("is_default","设为默认配置","所有跟随默认配置的聊天与新聊天会使用它。",item?.id===config.default_id)}<details class="details"><summary>回复与工具能力</summary>${select("trigger_at","被 @ 时直接回复",[["","继承全局"],["true","直接回复"],["false","由 Planner 判断"]],values.trigger_at == null ? "" : String(values.trigger_at))}${select("trigger_reply","被回复时触发",[["","跟随全局设置"],["true","开启"],["false","关闭"]],values.trigger_reply == null ? "" : String(values.trigger_reply))}${select("trigger_name","提及名称时直接回复",[["","继承全局"],["true","直接回复"],["false","由 Planner 判断"]],values.trigger_name == null ? "" : String(values.trigger_name))}${select("plan_enabled","启用 Planner",[["","跟随全局设置"],["true","开启"],["false","关闭"]],values.plan_enabled == null ? "" : String(values.plan_enabled))}${select("proactive_reply","允许主动接话",[["","跟随全局设置"],["true","开启"],["false","关闭"]],values.proactive_reply == null ? "" : String(values.proactive_reply))}<p class="help-caption">@ 和名称选择“由 Planner 判断”时，提及仍单独触发判断；最终生效的 Planner 关闭时，不因该提及回复。开启消息防抖时，先合并一轮再判断或回复。</p>${toggle("private_always_reply","私聊每条回复","对未单独覆盖的私聊和本地聊天生效；关闭后按 Planner 判断。",values.private_always_reply ?? true)}${toggle("auto_memory","自动记忆","新聊天继承；自动提取仍遵守记忆审核。",values.auto_memory ?? true)}${toggle("dictionary_learn","学习群词","先筛选证据，按学习页审核规则确认。",values.dictionary_learn ?? true)}${toggle("dictionary_use","使用群词","仅使用已确认且允许表达的词条。",values.dictionary_use ?? false)}${toggle("always_reply","群聊每条必回复","群内每轮消息跳过 Planner，防抖仍先合并；接收规则、暂停与日程安排继续生效。",values.always_reply ?? false)}${toggle("allow_tools","允许普通工具","电脑能力与管理员权限仍需独立开启。",values.allow_tools ?? true)}${toggle("allow_mcp","允许 MCP 调用","还需对应服务及工具允许列表。",values.allow_mcp ?? false)}${toggle("use_persona_knowledge","使用人格知识","使用人格已确认的公开资料。",values.use_persona_knowledge ?? true)}</details>`,"bot-profile",null,"保存配置");
      const body=$("#modal .modal-body");
      if(body && window.ShioriFeatures?.debounceFields)body.insertAdjacentHTML("beforeend",window.ShioriFeatures.debounceFields(values));
    }

    function profileBindForm(id) {
      const config=state.botProfiles, item=config.items.find(p=>p.id===id);
      openModal("绑定聊天到“"+item.name+"”","所选聊天将清除人格、模型和回复规则的独立覆盖，继承这份配置。记忆和聊天记录不变。",`<input type="hidden" name="profile_id" value="${e(id)}"><div class="corpus-files">${config.scopes.map(s=>`<label><input type="checkbox" name="scope_ids" value="${e(s.id)}"> ${e(s.name)} <small>${e(s.profile_id == null ? "旧独立设置" : s.profile_id || "跟随默认配置")}${s.overrides?.length ? " · 有独立覆盖" : ""}</small></label>`).join("<br>")}</div>`,"profile-bind",null,"绑定所选聊天");
    }

    function corpusForm() { openModal("上传蒸馏语料","ZIP 中的文件先进入独立任务空间，不会自动进入长期记忆。",`<div class="corpus-upload">${icon("upload")}${button("选择 ZIP 文件","choose-corpus","primary","upload")}<p>ZIP 最多 24 MB；自动识别可阅读文件，下一步展示文件清单与说话者。支持真人和虚构人物语料。</p></div><div id="corpus-preview"></div>`); }

    function showDistillPreview() {
      const draft = runtime.distillDraft; if (!draft) return;
      if (draft.mode === "character") {
        if (draft.lookup_id) {
          const characters=draft.characters || [];
          openModal("选择要蒸馏的角色","选中角色后点击开始，程序会自动整理资料并生成人格与 Skill 包。",`<div class="corpus-files">${characters.map(item=>`<div><label><input type="radio" name="character_id" value="${e(item.id)}" ${characters.length===1?"checked":""} required> <strong>${e(item.name)}</strong></label><p>${e((item.works || [item.work]).filter(Boolean).slice(0,4).join(" / "))}</p>${item.aliases?.length?`<p class="help-caption">别名：${e(item.aliases.join("、"))}</p>`:""}<p>${e(item.description || "该站点未提供简介，请核对角色名称与作品。")}</p>${safeLink(item.url)?`<a href="${e(item.url)}" target="_blank" rel="noopener noreferrer">查看角色资料 ↗</a>`:""}</div>`).join("") || notice("没有找到匹配角色，请返回换用角色全名或作品名称重试。","warning")}</div>${button("重新检索","distill-character","ghost small","search")}<details class="details"><summary>时间线与人格名称（可选）</summary>${field("version","作品版本 / 时间范围","","text","例如动画第一季；留空会保留资料中的版本边界。","maxlength=\"240\"")}${field("name","另存的人格名称","","text","留空使用选中角色的名称。","maxlength=\"100\"")}</details><p class="help-caption">检索角色不调用模型；开始蒸馏后才使用已配置的模型，计入模型用量。资料不足会明确标注。</p>`,"distill-start",null,"确认角色并开始蒸馏");
          $("#modal").classList.add("modal-wide");
          const submit=$('#modal button[type="submit"]');if(submit)submit.disabled=!characters.length;
          return;
        }
        const candidates = draft.candidates || [];
        openModal("确认要蒸馏的角色","请核对角色、作品版本与来源。候选中的资料只作为研究内容。",`<input type="hidden" name="mode" value="character"><div class="corpus-files">${candidates.map((item,index) => `<div><label><input type="checkbox" name="candidate_ids" value="${e(item.id || index)}" ${candidates.length === 1 ? "checked" : ""}> <strong>${e(item.name || item.character || item.title || "候选角色")}</strong> ${e(item.work || item.description || item.summary || item.snippet || "")}</label>${item.version_hint ? `<p>版本线索：${e(item.version_hint)}</p>` : ""}${item.officiality === "not_verified" ? '<p>来源身份尚未核实，请结合原文确认。</p>' : ""}${safeLink(item.url) ? `<p><a href="${e(item.url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a></p>` : ""}</div>`).join("") || '<p class="help-caption">未找到候选。请返回并更换作品或角色名称。</p>'}</div>${draft.warnings?.length ? notice(draft.warnings.join("；"),"warning") : ""}<div class="form-grid">${field("version","作品版本 / 时间范围（可选）","","text","例如：动画第一季，避免混用不同时间线。","data-full")}${field("name","人格名称",draft.character || "","text","生成后还可以编辑。","data-full")}</div>${toggle("confirmed","我已核对角色与资料范围","开始后会搜索公开资料并调用模型，生成 Skill 包与聊天人格。",false)}`,"distill-start",null,"开始蒸馏");
      } else {
        const files = draft.files || [], speakers = draft.speakers || [];
        openModal("确认语料与目标说话者","只使用选中的可读文件。不联网补充真人资料，不自动写入记忆。",`<input type="hidden" name="mode" value="corpus"><div class="corpus-files">${files.map((file,index) => `<div><label><input type="checkbox" name="selected_files" value="${e(file.id || file.path || file.name || index)}" ${file.readable !== false && !["skipped","failed","unsupported"].includes(file.status) ? "checked" : "disabled"}> <strong>${e(file.name || file.path)}</strong> ${e({readable:"可读取",skipped:"已跳过",failed:"读取失败"}[file.status] || file.status || "可读取")} ${e(file.reason || file.error || "")} ${file.characters ? e(file.characters) + " 字符" : ""} ${file.warnings?.length ? e(file.warnings.join("；")) : ""}</label></div>`).join("")}</div><div class="form-grid">${speakers.length ? select("target_speaker","目标说话者",[["","请选择"],...speakers.map(item => typeof item === "string" ? [item,item] : [item.id || item.name,item.name || item.label || item.id])],"","required data-full") : field("target_speaker","目标说话者 / 人物","","text","说明需要提炼谁。多人材料必须区分目标、引用和转述。","required data-full")}${field("name","人格名称","","text","可用真实姓名、昵称或你自定义的名称。","required data-full maxlength=\"100\"")}</div>${draft.warnings?.length ? notice(draft.warnings.join("；"),"warning") : ""}${toggle("confirmed","确认使用所选语料蒸馏人设","只生成人设描写，不生成 Skill。选中文本会发送给你的模型服务。",false)}`,"distill-start",null,"开始蒸馏");
      }
      $("#modal").classList.add("modal-wide");
    }
  return {distillChooser,characterForm,generateForm,profilePanel,profileForm,profileBindForm,corpusForm,showDistillPreview};
}};
