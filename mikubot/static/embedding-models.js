"use strict";

// The provider connection is shared; embedding models have their own setup,
// real endpoint test and explicit activation, independent of chat routing.
window.MikubotEmbedding = (() => {
  let currentTab="chat", catalog={}, selectedSource="", queryText="", discoverySequence=0;
  const key=value=>encodeURIComponent(value);
  const sources=()=>catalog.providers || [];
  const models=()=>(catalog.models || []).filter(item=>item.model_type==="embedding" || item.id===catalog.routing?.embedding);
  const editableSources=()=>sources().filter(item=>!item.legacy);
  const tabs=()=>`<nav class="model-type-tabs" aria-label="模型类型">${[["chat","对话模型"],["embedding","嵌入模型"]].map(([id,title])=>button(title,"model-type-tab",currentTab===id?"primary":"ghost","",`data-id="${id}" aria-pressed="${currentTab===id}"`)).join("")}</nav>`;
  async function load(){catalog=await api('/api/model-providers');return catalog;}
  async function render(){
    await load();const config=catalog.embedding || {}, items=models();
    if(!sources().some(item=>item.id===selectedSource))selectedSource=items[0]?.provider_id || editableSources()[0]?.id || sources()[0]?.id || "";
    const source=sources().find(item=>item.id===selectedSource);
    const visible=items.filter(item=>item.provider_id===selectedSource && `${item.name} ${item.model}`.toLowerCase().includes(queryText.toLowerCase()));
    const status=config.enabled ? config.available ? "语义检索已开启" : "已开启，但模型不可用" : "语义检索已关闭";
    const summary=`<section class="panel embedding-status"><div><h2>${e(status)}</h2><p>当前模型：<strong>${e(config.model || "尚未选择")}</strong>${config.model ? ` · ${e(config.provider_name || "默认供应商")}` : ""}</p><p class="help-caption">用于记忆与知识检索。设置后所有聊天共用，不会替换对话模型；首次召回按需生成向量，可能稍慢。</p></div><div class="section-actions">${config.enabled ? button("关闭语义检索","disable-embedding","ghost","stop") : ""}${config.legacy && config.available ? button("测试并启用原有配置","activate-embedding","","link",'data-id=""') : ""}</div></section>`;
    const cards=sources().map(item=>`<button class="source-list-item ${item.id===selectedSource ? "active" : ""}" data-action="embedding-source" data-id="${e(item.id)}"><span class="source-initial">${e(item.name.slice(0,1))}</span><span><strong>${e(item.name)}</strong><small>${e(item.base_url)}</small><span class="source-count">${items.filter(model=>model.provider_id===item.id).length} 个嵌入模型${item.enabled===false ? " · 已停用" : ""}</span></span></button>`).join("");
    const rows=visible.map(item=>`<article class="catalog-model"><div class="catalog-model-main"><strong>${e(item.name || item.model)}</strong><code>${e(item.model)}</code><div class="catalog-model-tags">${tag("嵌入模型","gray")}${tag(item.effective_enabled===false ? "已停用" : "已启用",item.effective_enabled===false?"gray":"")}${catalog.routing?.embedding===item.id ? tag("当前检索模型") : ""}</div></div><div class="catalog-model-actions">${button("测试连接","test-embedding","ghost small","link",`data-id="${e(item.id)}" ${item.effective_enabled===false?"disabled":""}`)}${button("测试并用于检索","activate-embedding","small","check",`data-id="${e(item.id)}" ${item.effective_enabled===false?"disabled":""}`)}${button("参数","edit-embedding","ghost small","settings",`data-id="${e(item.id)}"`)}${button(item.enabled===false?"启用":"停用","toggle-catalog-model","ghost small","",`data-id="${e(item.id)}"`)}${button("删除","delete-catalog-model","ghost small","trash",`data-id="${e(item.id)}"`)}</div></article>`).join("");
    const right=`<header class="catalog-source-head"><div><h2>${e(source?.name || "嵌入模型")}</h2><p>${e(source?.base_url || "复用已有供应商，或填写新的连接。")}</p></div>${button("添加嵌入模型","new-embedding","primary","plus")}</header><div class="catalog-model-toolbar"><label class="search-field"><input id="embedding-model-query" type="search" aria-label="搜索嵌入模型" placeholder="搜索嵌入模型" value="${e(queryText)}"></label><span>${visible.length} 个模型</span></div><div class="catalog-model-list">${rows || empty("search","这里还没有嵌入模型","添加服务商提供的嵌入型号，测试通过后即可启用检索。","new-embedding","添加嵌入模型")}</div>`;
    return heading("MODELS","模型供应商","嵌入模型单独配置，在这一页完成添加、测试和启用。",button("添加嵌入模型","new-embedding","primary","plus"))+tabs()+summary+`<div class="model-catalog-layout"><aside class="model-source-panel"><div class="model-source-list">${cards}</div></aside><section class="panel model-catalog-panel">${right}</section></div>`;
  }
  function setupModal(){
    discoverySequence++;const initial=editableSources().some(item=>item.id===selectedSource)?selectedSource:editableSources()[0]?.id || "";
    openModal("添加嵌入模型","保存后发送一次短文本到 /embeddings 测试，计入模型用量；测试成功才启用检索，聊天模型保持原值。",
      `${select("provider_id","模型供应商",[["","新建供应商"],...editableSources().map(item=>[item.id,item.name])],initial)}<div id="embedding-new-source" ${initial?'hidden':''}>${field("source_name","供应商名称","","text","例如：我的向量模型服务","maxlength=\"80\"")}${field("base_url","API 地址","","url","兼容 OpenAI 的地址，通常以 /v1 结尾。","placeholder=\"https://…/v1\"")}${field("api_key","API 密钥","","password","本机服务没有密钥时可留空。","autocomplete=\"off\"")}</div><div class="section-actions">${button("获取模型列表","discover-embedding","","refresh")}</div><div id="embedding-discovery" aria-live="polite"></div>${field("model","嵌入模型名称","","text","从目录选择或手动填写准确型号；服务必须支持 /embeddings。","required maxlength=\"160\" list=\"embedding-model-options\"")}<datalist id="embedding-model-options"></datalist>${field("input_price","输入价格 / 百万 Token",0,"number","人民币估算单价；实际费用以供应商为准。","min=\"0\" step=\"0.001\"")}`,
      "embedding-setup",null,"保存、测试并启用检索");
  }
  async function test(modelId,activate){
    const result=await api('/api/model-catalog/embedding/test',{method:'POST',body:{model_id:modelId,activate}});
    if(result.ok!==true)throw new Error("嵌入测试未通过，检索设置未改变。");
    toast(`${activate?"语义检索已启用":"嵌入连接成功"} · ${result.dimension} 维 · ${result.elapsed_ms} ms`);
    return result;
  }
  async function handleAction(action,target){
    if(action==="model-type-tab" || action==="open-embedding-models"){
      currentTab=action==="open-embedding-models"?"embedding":target.dataset.id;
      queryText="";if(state.page!=="persona" || state.botTab!=="models")await navigate("models");else await renderPage();
    }else if(action==="embedding-source"){selectedSource=target.dataset.id;queryText="";await renderPage();}
    else if(action==="new-embedding"){await load();setupModal();}
    else if(action==="discover-embedding"){
      const form=target.closest('form'),data=getForm(form),id=form.dataset.savedProvider || data.provider_id;
      const sequence=++discoverySequence;target.disabled=true;
      try{
        const result=await api(id?`/api/model-providers/${key(id)}/discover`:'/api/model-providers/discover',
          {method:'POST',body:id?{}:{base_url:data.base_url,api_key:data.api_key || ""}});
        if(sequence!==discoverySequence || state.modalKind!=="embedding-setup")return true;
        const options=$('#embedding-model-options',form);if(options)options.innerHTML=(result.models || []).map(name=>`<option value="${e(name)}"></option>`).join("");
        const info=$('#embedding-discovery',form);if(info)info.textContent=`已获取 ${result.models?.length || 0} 个型号，请在下方输入框选择嵌入型号；目录不代表能力测试通过。`;
      }finally{target.disabled=false;}
    }else if(action==="test-embedding" || action==="activate-embedding"){
      target.disabled=true;try{await test(target.dataset.id || "",action==="activate-embedding");await bootstrap();}finally{target.disabled=false;}
    }else if(action==="disable-embedding"){
      await api('/api/model-catalog/embedding',{method:'PUT',body:{enabled:false}});toast("语义检索已关闭，已保存模型保留。");await bootstrap();
    }else if(action==="edit-embedding"){
      await load();const item=(catalog.models || []).find(item=>item.id===target.dataset.id);if(!item)throw new Error("模型不存在，请刷新。");
      openModal("嵌入模型参数","此处参数仅用于向量请求。地址与密钥在对应供应商中管理。",
        `<input type="hidden" name="id" value="${e(item.id)}">${field("name","显示名称",item.name || item.model,"text","","required")}${field("timeout_seconds","请求超时（秒）",item.timeout_seconds ?? 90,"number","5—600秒。","min=\"5\" max=\"600\"")}${field("input_price","输入价格 / 百万 Token",item.input_price || 0,"number","人民币估算单价。","min=\"0\" step=\"0.001\"")}`,"embedding-parameters",item);
    }else return false;return true;
  }
  async function handleSubmit(kind,form){
    const data=getForm(form);
    if(kind==="embedding-setup"){
      const model=String(data.model || "").trim();if(!model)throw new Error("请填写嵌入模型名称。");
      let sourceId=form.dataset.savedProvider || data.provider_id;
      if(!data.provider_id){
        if(!String(data.base_url || "").trim())throw new Error("请填写供应商 API 地址。");
        const source=await api(sourceId?`/api/model-providers/${key(sourceId)}`:'/api/model-providers',{method:sourceId?'PUT':'POST',body:{name:data.source_name || "嵌入模型服务",base_url:data.base_url,api_key:data.api_key || "",enabled:true}});
        sourceId=source.id;form.dataset.savedProvider=sourceId;
        const secret=$('[name="api_key"]',form);if(secret)secret.value="";
      }
      selectedSource=sourceId;
      const saved=await api('/api/model-catalog/models',{method:'POST',body:{provider_id:sourceId,model_type:'embedding',models:[{model,input_price:Number(data.input_price || 0),tools:false,vision:false}]}});
      const item=(saved.models || saved.added || []).find(item=>item.provider_id===sourceId && item.model===model);
      if(!item)throw new Error("模型已保存，但未找到模型编号，请刷新列表。");
      if(item.model_type==='chat')throw new Error("同名配置已标为对话模型，请确认填写的是嵌入型号。");
      if(item.model_type!=='embedding')await api(`/api/model-catalog/models/${key(item.id)}`,{method:'PATCH',body:{model_type:'embedding',tools:false,vision:false}});
      try{await test(item.id,true);}catch(error){throw new Error(`模型已保存，测试未通过，原检索设置未改变：${error.message}`);}
      closeModal();await bootstrap();
    }else if(kind==="embedding-parameters"){
      await api(`/api/model-catalog/models/${key(data.id)}`,{method:'PATCH',body:{name:data.name,input_price:Number(data.input_price),timeout_seconds:Number(data.timeout_seconds)}});
      closeModal();toast("嵌入模型参数已保存。");await bootstrap();
    }else return false;return true;
  }
  async function handleChange(target){
    if(target.name!=="provider_id" || !target.closest?.('form[data-form="embedding-setup"]'))return false;
    const form=target.closest('form');delete form.dataset.savedProvider;discoverySequence++;
    const panel=$('#embedding-new-source',form);if(panel)panel.hidden=!!target.value;
    const list=$('#embedding-model-options',form);if(list)list.innerHTML="";
    const info=$('#embedding-discovery',form);if(info)info.textContent="";return true;
  }
  function handleInput(target){
    if(["base_url","api_key"].includes(target.name) && target.closest?.('form[data-form="embedding-setup"]')){
      discoverySequence++;const form=target.closest('form');
      const options=$('#embedding-model-options',form);if(options)options.innerHTML="";
      const info=$('#embedding-discovery',form);if(info)info.textContent="连接信息已改变，请重新获取型号或手动填写。";
    }
    if(target.id!=="embedding-model-query")return;
    queryText=target.value;
    clearTimeout(queryTimer);queryTimer=setTimeout(async()=>{await renderPage();const input=$('#embedding-model-query');input?.focus();},250);
  }
  let queryTimer;
  return {active:()=>currentTab==="embedding",tabs,render,handleAction,handleSubmit,handleChange,handleInput};
})();
