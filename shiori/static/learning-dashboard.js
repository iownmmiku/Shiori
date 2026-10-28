"use strict";
window.ShioriLearning = (() => {
  async function panel(kind) {
    const memory=kind==="memory",all=memory ? state.memoryAll !== false : Boolean(state.learningAll);
    const data=await api(`/api/learning/dashboard?${query({kind,scope_id:all?undefined:state.scope})}`);
    const policy=data.policy;
    const counts=Object.entries(data.counts).map(([name,count])=>`<div class="record-card"><small>${e(label(name))}</small><h2>${e(count)}</h2></div>`).join("");
    const max=Math.max(1,...data.trend.map(x=>x.count));
    const trend=`<div style="display:flex;gap:10px;align-items:end;min-height:110px" aria-label="最近七日新增数量">${data.trend.map(x=>`<div style="flex:1;text-align:center"><strong>${e(x.count)}</strong><div style="background:var(--accent,#138781);height:${Math.max(2,x.count/max*65)}px;border-radius:4px"></div><small>${e(x.day)}</small></div>`).join("")}</div>`;
    return `<section class="learning-dashboard"><div class="record-actions"><h3>${memory?"记忆整理概览":"黑话学习概览"}</h3>${memory ? "" : button(all?"正在看全部会话 · 切回当前":"查看全部会话","learning-all","small","",'data-kind="language"')}</div><div class="record-grid">${counts || '<p>尚无学习记录。</p>'}</div><div class="record-card"><h4>最近七日新增</h4>${trend}</div><details class="record-card"><summary>学习与审核设置</summary><form data-form="learning-policy" data-kind="${kind}">${memory?`${field("extraction_messages","累计几条消息后整理",policy.extraction_messages,"number","短对话也会在空闲时整理。","min=2 max=50")}${field("extraction_idle_seconds","空闲整理间隔（秒）",policy.extraction_idle_seconds,"number","","min=30 max=3600")}${field("daily_extractions","每日最多整理调用",policy.daily_extractions,"number","0 表示停止自动整理。","min=0 max=1000")}`:`${toggle("learning_enabled","自动发现黑话","先按本地频率和明确释义筛选，再调用模型。",policy.learning_enabled)}${toggle("group_isolation","按群隔离词义","关闭时只共享确认后的词义；私聊与原始聊天例句不共享。",policy.group_isolation)}`}<label class="field"><span>审核方式</span><select name="review_mode"><option value="automatic" ${policy.review_mode==="automatic"?"selected":""}>明确可靠的内容自动确认，其余待审核</option><option value="manual" ${policy.review_mode==="manual"?"selected":""}>全部手动审核</option></select></label><button class="btn small" type="submit">保存学习设置</button></form></details>${memory && data.progress.some(x=>x.last_error)?'<p class="notice warning">部分对话最近整理失败，原始消息和进度已保留；请检查运行日志与记忆模型。</p>':""}</section>`;
  }
  async function deliveryPanel(){
    const records=rows(await api(`/api/delivery?${query({scope_id:state.scope})}`));
    const incomplete=records.filter(x=>x.parts.some(p=>p.status!=="confirmed"));
    return `<details class="record-card"><summary>QQ 分段发送记录 · ${incomplete.length} 条有未确认或未发送段落</summary><p class="help-caption">“未确认”可能已经送达，系统不会自动重发；补发按钮只发送从未尝试发送的后续段落。</p>${incomplete.slice(0,15).map(x=>`<article class="source-item"><small>${e(fmt(x.updated_at,true))}</small>${x.parts.map((p,i)=>`<p><strong>${i+1}. ${{confirmed:"已确认",pending:"尚未发送",sending:"送达待确认",unknown:"送达未知"}[p.status]||"未知"}</strong> ${e(p.text)}</p>`).join("")}${x.parts.some(p=>p.status==="pending")?button("补发未发送的后续","delivery-resume","small","",`data-id="${e(x.id)}"`):""}</article>`).join("") || '<p>当前会话没有未完成的发送记录。</p>'}</details>`;
  }
  async function handleAction(action,target){
    if(action==="culture-page"){state.culturePage.offset=Number(target.dataset.offset);await renderPage();return true;}
    if(action==="learning-all"){
      if(target.dataset.kind==="memory" || (!target.dataset.kind && state.page==="memory"))state.memoryAll=!(state.memoryAll !== false);
      else state.learningAll=!state.learningAll;
      await renderPage();return true;
    }
    if(action==="delivery-resume"){
      await confirmAction("补发尚未发送的后续段落？","已确认和送达未知的段落均不会重发。权限或来源已变化时会拒绝补发。",async()=>{await api(`/api/delivery/${encodeURIComponent(target.dataset.id)}/resume`,{method:"POST",body:{confirm:true}});},"补发后续");return true;
    }
    return false;
  }
  async function handleSubmit(kind,form){
    if(kind!=="learning-policy")return false;
    const value=getForm(form),section=form.dataset.kind==="memory"?"memory":"language";
    if(section==="memory")for(const key of ["extraction_messages","extraction_idle_seconds","daily_extractions"])value[key]=Number(value[key]);
    await api("/api/learning/policy",{method:"PUT",body:{[section]:value}});await bootstrap();toast("学习设置已保存");return true;
  }
  return {panel,deliveryPanel,handleAction,handleSubmit};
})();
