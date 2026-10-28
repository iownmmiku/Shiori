"use strict";

// All bot behavior lives behind one navigation entry. Existing form handlers
// are shared with settings, so credentials and validation have one code path.
(() => {
  const experience = window.ShioriExperience;
  const library = experience.renderPersonas, settingsPage = experience.renderSettings;
  function section(html, id) {
    const template=document.createElement("template");template.innerHTML=html;
    return template.content.querySelector(`#${id}`)?.outerHTML || "";
  }
  function enhanceQQ(html) {
    const template=document.createElement("template");template.innerHTML=html;
    const grid=template.content.querySelector('#onebot-fields>.form-grid');
    if(grid){
      const lists=['owner_ids','allowed_groups'].map(name=>grid.querySelector(`[name="${name}"]`)?.closest('.field')).filter(Boolean);
      const details=document.createElement('details');details.className='details';details.innerHTML='<summary>手动连接（远端连接器或高级设置）</summary>';grid.replaceWith(details);details.append(grid);details.after(...lists);
    }
    return template.innerHTML;
  }
  experience.renderSettings=async()=>{
    const template=document.createElement('template');template.innerHTML=await settingsPage();
    template.content.querySelector('.settings-aside')?.remove();
    for(const id of ['model-section','providers-section','routing-section','updates-section','qq-section','identity-section','computer-section','admins-section','output-section','planner-budget-section','memory-settings-section','context-section','exploration-section'])template.content.querySelector('#'+id)?.remove();
    template.content.querySelectorAll('section').forEach(node=>{if(node.querySelector('[data-form="routing"],[data-form="provider"]'))node.remove();});
    template.content.querySelectorAll('.settings-foldout').forEach(node=>{if(!node.querySelector('section,form'))node.remove();});
    return template.innerHTML.replace('<h1>设置与连接</h1>','<h1>系统设置</h1>').replace('连接模型与 QQ，管理预算，也照顾好本地数据。','管理登录与远程入口、预算和本地数据。').replace('<div class="settings-main">','<div class="settings-main"><section class="panel settings-section"><h2>机器人配置已集中到一页</h2><p>名称、人格、回复、QQ 接入和电脑权限都在这里调整。</p>'+button('打开机器人配置','go-persona','primary','settings')+'</section>');
  };
  experience.renderPersonas=async()=>{
    const tab=state.botTab || "basic";
    const tabs=[["basic","基本配置"],["models","模型"],["search","网页搜索"],["reply","回复与记忆"],["qq","QQ 连接"],["computer","电脑与管理员"],["personas","人格库"],["stickers","表情包"]];
    let body;
    if(tab==="basic")body=await experience.profilePanel()+identitySection()+timeAwarenessSection()+`<section class="panel settings-section"><h2>默认聊天模型</h2><p>${e(configuredModel().model || "尚未配置")}</p>${button("配置模型与供应商","go-model-sources","primary","spark")}</section>`;
    else if(tab==="models")body=await window.ShioriBotModels.render();
    else if(tab==="search")body=await window.ShioriWebSearch.render();
    else if(tab==="reply"){
      const settings=await settingsPage();
      body=window.ShioriFeatures.debounceSection()+outputSection()+`<details class="panel settings-section"><summary>高级 · 判断预算与记忆检索</summary>${plannerSection()+section(settings,"memory-settings-section")+section(settings,"context-section")+explorationSection()}</details>`;
    }else if(tab==="stickers")body=await window.ShioriStickers.render();
    else if(tab==="personas")body=await library();
    else {
      const settings=enhanceQQ(await settingsPage());
      body=tab==="qq"?section(settings,"qq-section"):section(settings,"computer-section")+section(settings,"admins-section");
    }
    return heading("BOT CONFIGURATION","机器人配置","先设置一份默认配置，所有新聊天自动使用。特殊聊天再单独调整。",button("快速入门","setup","","spark"))+`<nav class="bot-config-tabs" aria-label="机器人配置分类">${tabs.map(([id,title])=>button(title,"bot-tab",id===tab?"primary":"ghost","",`data-id="${id}" aria-pressed="${id===tab}"`)).join("")}</nav><div class="bot-config-body">${body}</div>`;
  };
  const originalAction=experience.handleAction;
  experience.handleAction=async(action,target)=>{
    if(action==="bot-tab"){state.botTab=target.dataset.id;await renderPage();return true;}
    if(action==="bot-default"){state.botTab="basic";await navigate("persona");return true;}
    if(action==="scope-inherit"){
      await api('/api/scopes/'+encodeURIComponent(target.dataset.id)+'/inherit',{method:'POST',body:{}});
      toast("已恢复默认机器人配置；记忆与聊天记录保留");await bootstrap();return true;
    }
    if(action==="scope-admission"){
      const id=target.dataset.id, item=state.scopes.find(s=>s.id===id), blocked=target.dataset.blocked==="true";
      if(!item)throw new Error("聊天已不存在，请刷新。");
      if(!id.startsWith("web:")){
        const [platform,kind,account]=id.split(":"), key=platform==="qqofficial"?"qq_official":"onebot", cfg=state.settings[key] || {};
        const list=(cfg.chat_policy || "blacklist")==="blacklist" ? "blocked_" : "allowed_";
        const field=list+(kind==="group"?"groups":"users"), values=new Set(cfg[field] || []);
        const add=list==="blocked_"?!blocked:blocked;
        if(add)values.add(account);else values.delete(account);
        await api('/api/settings',{method:'PUT',body:{[key]:{[field]:[...values]}}});
      }
      await api('/api/scopes/'+encodeURIComponent(id),{method:'PUT',body:{enabled:blocked}});
      toast(blocked?"已允许此聊天":"已屏蔽此聊天");await bootstrap();return true;
    }
    if(action==="qq-settings"){state.botTab="qq";await navigate("persona");return true;}
    if(action==="computer-settings"){state.botTab="computer";await navigate("persona");return true;}
    return originalAction(action,target);
  };
})();
