"use strict";

// Provider credentials stay global. Profiles and conversations only reference
// model IDs; moving their editor must not duplicate or migrate secrets.
window.ShioriBotModels = (() => {
  let catalog = {};
  const usable = () => (catalog.models || []).filter(item => item.model_type !== "embedding" && item.model && item.enabled !== false && item.effective_enabled !== false);
  const title = item => `${item.provider_name || (catalog.providers || []).find(source => source.id === item.provider_id)?.name || "模型"} · ${item.name || item.model}`;
  const choices = items => items.map(item => [item.id === "default" ? "" : item.id,title(item)]);
  function keepSelection(options,selected) {
    if(selected && !options.some(([id])=>id===selected)) {
      const item=(catalog.models || []).find(item=>item.id===selected);
      return [...options,[selected,`原选择不可用：${item ? title(item) : selected}（请重新选择）`]];
    }
    return options;
  }
  function routeTitle(purpose) {
    const resolved = catalog.routes?.[purpose] || {};
    return resolved.model || "尚未配置";
  }
  function libraryBody(html) {
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelector(".page-heading")?.remove();
    template.content.querySelector(".catalog-routing")?.remove();
    return template.innerHTML;
  }
  async function render() {
    catalog = await api("/api/model-providers");
    const embedding = window.ShioriEmbedding?.active();
    const library = libraryBody(await window.ShioriPages.renderModels());
    if (embedding) return `<p class="help-caption">模型连接在这里统一管理；嵌入模型只用于记忆与知识检索。</p>${library}`;
    const options = keepSelection(choices(usable()),catalog.routing?.chat), defaultLabel = `基础默认模型：${state.settings.model?.model || "尚未配置"}`;
    const hasLegacy=!!(state.settings.model?.base_url && state.settings.model?.model);
    if (!options.some(([id]) => id === "")) options.unshift(["",defaultLabel]);
    const vision = usable().filter(item => item.vision);
    const chat = (catalog.models || []).find(item=>item.id===(catalog.routes?.chat?.model_id || "default")) || {};
    const primary = `<section class="panel settings-section" id="bot-model-defaults"><h2>默认使用哪些模型</h2><p>这里设置全局默认。机器人配置和会话有单独选择时，保留各自覆盖；供应商地址与密钥只需填写一次。</p><form data-form="bot-model-defaults"><div class="form-grid">${select("chat","默认对话模型",options,catalog.routing?.chat || "")}${select("sticker_model_id","表情包识图模型",keepSelection([["","跟随对话模型（需支持图片）"],...choices(vision).filter(([id])=>id)],state.settings.stickers?.model_id),state.settings.stickers?.model_id || "")}</div><p class="help-caption">聊天中的图片跟随当前对话模型：${e(chat.vision ? "已开启图片输入" : "尚未开启图片输入，请在模型参数中确认视觉能力")}。表情包识图可单独选择，嵌入模型在“嵌入模型”页签中设置。</p><div class="model-effective-summary">${tag(`对话：${routeTitle("chat")}`)}${tag(`人格蒸馏：${routeTitle("distillation")}`,"gray")}${tag(`检索：${catalog.embedding?.enabled ? catalog.embedding.model || "模型不可用" : "未开启"}`,"gray")}</div><div class="section-actions"><button type="submit" class="btn primary">保存默认模型</button>${button("测试所选对话模型","test-bot-chat-model","","link")}${button("连接新模型","quick-model","ghost","plus")}</div><p data-model-test-result role="status" aria-live="polite"></p></form></section>`;
    const purposes = Object.entries(routeNames).filter(([purpose]) => !["chat","embedding","gate"].includes(purpose));
    const advanced = `<details class="panel settings-section"><summary>高级 · 模型分工与备用顺序</summary><form data-form="bot-model-advanced"><div class="form-grid">${purposes.map(([purpose,label])=>select(purpose,label,keepSelection([["",purpose === "distillation" || !hasLegacy ? "继承全局对话模型" : defaultLabel],...choices(usable()).filter(([id])=>id)],catalog.routing?.[purpose]),catalog.routing?.[purpose] || "")).join("")}</div><div class="route-status-list">${purposes.map(([purpose,label])=>`<div class="route-status"><span>${e(label)}</span><strong>${e(routeTitle(purpose))}</strong></div>`).join("")}</div><p class="help-caption">上方显示已保存设置的实际结果。${hasLegacy ? "旧配置的空用途沿用已配置的基础默认模型；单独指定的用途保持原选择。" : "未单独设置的判断、记忆与整理继承全局对话模型，首次连接后即可使用。嵌入模型仍单独配置。"}</p><div class="form-grid">${Array.from({length:3},(_,index)=>select(`fallback_${index}`,`第 ${index+1} 个备用`,keepSelection([["","不使用备用"],...choices(usable()).filter(([id])=>id)],catalog.routing?.fallback?.[index]),catalog.routing?.fallback?.[index] || "")).join("")}</div><p class="help-caption">备用服务会接收同一份相关上下文；响应中断后是否尝试备用，由模型参数中的故障恢复开关决定。</p><div class="section-actions"><button type="submit" class="btn primary">保存模型分工</button></div></form></details>`;
    return primary + library + advanced;
  }
  async function handleSubmit(kind,form) {
    const data = getForm(form);
    if (kind === "bot-model-defaults") {
      await api("/api/settings",{method:"PUT",body:{routing:{chat:data.chat || ""},stickers:{model_id:data.sticker_model_id || ""}}});
    } else if (kind === "bot-model-advanced") {
      const routing = Object.fromEntries(["planner","memory","summary","distillation"].map(key=>[key,data[key] || ""]));
      routing.fallback = [...new Set([0,1,2].map(index=>data[`fallback_${index}`]).filter(Boolean))];
      await api("/api/model-catalog/routing",{method:"PUT",body:routing});
    } else return false;
    toast("模型设置已保存；会话的独立选择保留。");await bootstrap();return true;
  }
  async function handleAction(action,target) {
    if(action === "edit-base-model") {
      openModal("基础默认模型","保留旧版的独立模型连接。修改不会替换机器人或会话已指定的模型。",settingsModelForm(state.settings.model || {})+'<div id="model-result" aria-live="polite"></div>',"model");
      return true;
    }
    if (action !== "test-bot-chat-model") return false;
    const form=target.closest("form"), result=$('[data-model-test-result]',form);
    target.disabled=true;
    try {
      const selected=getForm(form).chat || "";
      const tested=await api("/api/model/test",{method:"POST",body:{provider_id:selected}});
      if(tested.ok!==true)throw new Error(tested.message || "模型测试未通过");
      if(result)result.textContent="连接成功。测试不保存选择，点击保存后生效。";
    } catch(error) {if(result)result.textContent=`测试失败：${error.message}`;else throw error;}
    finally {target.disabled=false;}
    return true;
  }
  return {render,handleSubmit,handleAction};
})();
