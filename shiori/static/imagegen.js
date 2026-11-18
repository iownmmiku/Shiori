"use strict";
window.ShioriImageGen=(()=>{
  const ui=window.ShioriExperience,oldAction=ui.handleAction,oldSubmit=ui.handleSubmit;
  const kinds={openai:"OpenAI 兼容图像接口",gemini:"Gemini 图像模型",novelai:"NovelAI",comfyui:"ComfyUI 工作流"};
  const endpoints={openai:"https://api.openai.com/v1",gemini:"https://generativelanguage.googleapis.com/v1beta",novelai:"https://image.novelai.net",comfyui:"http://127.0.0.1:8188"};
  const aspects=[["square","方图"],["portrait","竖图"],["landscape","横图"]];
  const mappings=[["prompt","正面提示词（必填）"],["negative_prompt","负面提示词"],["reference","参考图（LoadImage 的 image）"],["width","宽度"],["height","高度"],["seed","随机种子"]];
  const keys=["id","name","kind","endpoint","model","timeout_seconds","negative_prompt","natural_negative","reference","quality","steps","scale","sampler","strength","extra_body","workflow","workflow_fields","output_node"];
  const config=()=>state.imagegen || {services:[],personas:{},persona_list:[],usage:{counts:{}}};
  const services=()=>config().services || [];
  const plain=item=>Object.fromEntries(keys.filter(key=>item[key]!==undefined).map(key=>[key,item[key]]));
  const json=(text,label)=>{
    const value=String(text || "").trim();
    if(!value)return {};
    let parsed;
    try{parsed=JSON.parse(value);}catch{throw new Error(`${label}不是有效的 JSON。`);}
    if(!parsed || typeof parsed!=="object" || Array.isArray(parsed))throw new Error(`${label}须为 JSON 对象。`);
    return parsed;
  };
  const number=(value,label)=>{const result=Number(value);if(!Number.isFinite(result))throw new Error(`${label}须为数字。`);return result;};

  function serviceCard(item){
    const current=config().service_id===item.id;
    const facts=[kinds[item.kind] || item.kind,item.model || "工作流自带模型",item.api_key_set?"已保存密钥":"未填写密钥",item.supports_reference?"支持参考图改图":"不支持参考图"];
    return `<article class="record-card"><h3>${e(item.name)}${current?" · 当前使用":""}</h3><p class="help-caption">${e(facts.join(" · "))}</p><p class="help-caption">${e(item.endpoint)}</p><div class="record-actions">${current?"":button("设为当前","imagegen-use","primary small","",`data-id="${e(item.id)}"`)}${button("编辑","imagegen-edit","ghost small","",`data-id="${e(item.id)}"`)}${button("删除","imagegen-delete","ghost small","",`data-id="${e(item.id)}"`)}</div></article>`;
  }

  function usageText(usage){
    const counts=Object.values(usage?.counts || {}),total=counts.reduce((sum,value)=>sum+value,0);
    return `今天普通成员已画 ${total} 张（${counts.length} 人）· 排队 ${usage?.queued ?? 0} 张 · 正在画 ${usage?.running ?? 0} 张`;
  }

  function personaSection(){
    const list=config().persona_list || [],looks=config().personas || {};
    if(!list.length)return "";
    const rows=list.map(persona=>{
      const look=looks[persona.id] || {};
      return `<h3>${e(persona.name)}</h3>${textarea(`prompt__${persona.id}`,"外貌提示词",look.prompt || "","发型、发色、瞳色、脸型、身材、常穿的衣服等。NovelAI / ComfyUI 建议用英文标签，例如 1girl, long brown hair, amber eyes。",'maxlength="2000" rows="3"')}${textarea(`negative__${persona.id}`,"外貌负面提示词",look.negative_prompt || "","自拍时不想出现的特征，例如 short hair, glasses。",'maxlength="2000" rows="2"')}`;
    }).join("");
    return `<section class="panel settings-section"><h2>人格外貌</h2><p>角色画自己（自拍、发自己的照片）时，会自动把当前人格的外貌提示词加进画面描述，负面提示词并入服务的负面提示词，让每张图里的她长得一样。画别的东西时不会加。</p><form data-form="imagegen-personas">${rows}<div class="section-actions"><button type="submit" class="btn primary">保存人格外貌</button></div></form></section>`;
  }

  function testSection(){
    const result=state.imagegenTest;
    const preview=result?.image?`<figure class="imagegen-preview"><img src="${e(result.image)}" alt="测试生成的图片" style="max-width:100%;max-height:480px;border-radius:12px"><figcaption class="help-caption">用时 ${e(Math.round((result.elapsed_ms || 0)/100)/10)} 秒 · ${e(Math.round((result.bytes || 0)/1024))} KB</figcaption></figure>`:"";
    return `<section class="panel settings-section"><h2>测试生图</h2><p>用当前服务画一张，确认地址、密钥和模型能用。测试不占任何人的次数，但服务商照常计费。</p><form data-form="imagegen-test"><div class="form-grid">${textarea("prompt","画面描述",state.imagegenTestPrompt || "一只坐在窗台上晒太阳的橘猫，水彩风格","", 'maxlength="1500" rows="3" required')}${select("aspect","比例",aspects,state.imagegenTestAspect || "square")}</div><div class="section-actions"><button type="submit" class="btn primary">生成测试图</button></div></form>${preview}</section>`;
  }

  async function render(){
    state.imagegen=await api("/api/imagegen");
    const cfg=config(),list=services();
    const warning=cfg.warning?`<p class="help-caption" role="alert">${e(cfg.warning)}</p>`:"";
    const main=`<section class="panel settings-section"><h2>生图</h2>${warning}<p>在 QQ 群聊和私聊里，对方请角色画画、拍照、自拍或改图时，聊天模型可以调用画图工具；角色先按自己的意愿决定画不画，文字回复照常先发，图片在后台画好后单独发出，一般要几十秒。会话需要允许使用工具。对方消息里附了图片时，可以照着它改图（当前服务需支持参考图）。</p><p class="help-caption">${e(usageText(cfg.usage))}</p><form data-form="imagegen-settings"><div class="form-grid">${toggle("enabled","启用生图","关闭后聊天模型拿不到画图工具。",cfg.enabled===true)}${select("service_id","当前使用的服务",[["","未选择"],...list.map(item=>[item.id,item.name])],cfg.service_id || "")}${field("daily_per_user","每人每天最多张数",cfg.daily_per_user ?? 5,"number","按 QQ 账号计算，管理员不受限制；0 表示只有管理员能用。没画成的不算次数。",'min="0" max="1000" step="1" required')}</div><div class="section-actions"><button type="submit" class="btn primary">保存生图设置</button></div></form></section>`;
    const add=Object.entries(kinds).map(([kind,title])=>button("添加 "+title,"imagegen-add","ghost small","",`data-kind="${kind}"`)).join("");
    const cards=list.length?`<div class="record-grid">${list.map(serviceCard).join("")}</div>`:'<p class="help-caption">还没有生图服务。先添加一个，再在上面选为当前使用的服务。</p>';
    const servicePanel=`<section class="panel settings-section"><h2>生图服务</h2><p>支持 OpenAI 兼容的图像接口（gpt-image、DALL·E 及多数中转站的绘图模型）、Gemini 图像模型、NovelAI 和本机或局域网的 ComfyUI。密钥只保存在本机，页面不会回显。</p>${cards}<div class="record-actions">${add}</div><p class="help-caption">生图配置保存在数据目录的 imagegen 文件夹里，普通应用备份不包含它。</p></section>`;
    return main+servicePanel+personaSection()+(list.length?testSection():"");
  }

  function serviceForm(item){
    const kind=item.kind,saved=Boolean(item.id);
    let body=field("name","服务名称",item.name || kinds[kind],"text","",'maxlength="60" data-full')
      +field("endpoint","接口地址",item.endpoint || endpoints[kind],"url",kind==="openai"?"填到 /v1 为止，例如 https://api.openai.com/v1；会请求 /images/generations 和 /images/edits。":kind==="gemini"?"例如 https://generativelanguage.googleapis.com/v1beta；会请求 /models/模型:generateContent。":kind==="novelai"?"官方地址是 https://image.novelai.net。":"ComfyUI 的地址，例如 http://127.0.0.1:8188。",'required maxlength="2048" data-full')
      +field("api_key",kind==="comfyui"?"访问令牌（可选）":"API 密钥","","password",item.api_key_set?"已保存密钥；留空继续使用，页面不会回显。":kind==="comfyui"?"ComfyUI 前面有鉴权代理时才需要，会以 Bearer 方式发送。":"填写这个服务的密钥。",'autocomplete="new-password" spellcheck="false" data-full')
      +(item.api_key_set?toggle("clear_api_key","删除已保存的密钥","勾选并保存后删除；留空输入框不会删除密钥。",false):"")
      +field("model",kind==="comfyui"?"名称标记（可选）":"模型名称",item.model || (kind==="openai"?"gpt-image-1":kind==="novelai"?"nai-diffusion-4-5-full":""),"text",kind==="gemini"?"例如 gemini-2.5-flash-image。":kind==="comfyui"?"模型由工作流里的节点决定，这里只用来标记。":"",`maxlength="200" ${kind==="openai"||kind==="gemini"?"required":""}`)
      +field("timeout_seconds","等待上限（秒）",item.timeout_seconds ?? 180,"number","5—600 秒，超过就算失败并退回次数。",'min="5" max="600" step="1" required')
      +textarea("negative_prompt","负面提示词",item.negative_prompt || "",kind==="openai"||kind==="gemini"?"这类模型没有独立的负面字段，默认不发送；打开下面的开关才会写成「Avoid: …」附在描述后面。":"不想出现的内容，例如 lowres, bad anatomy, watermark。",'maxlength="2000" rows="2"');
    if(kind==="openai"||kind==="gemini")body+=toggle("natural_negative","把负面提示词写进描述","有的模型看到「不要画 X」反而会画出 X，按需开启。",item.natural_negative===true);
    if(kind==="openai")body+=select("quality","画质",[["","默认"],["auto","auto"],["low","low"],["medium","medium"],["high","high"],["standard","standard（DALL·E 3）"],["hd","hd（DALL·E 3）"]],item.quality || "");
    if(kind==="novelai")body+=`<div class="form-grid">${field("steps","采样步数",item.steps ?? 28,"number","",'min="1" max="100" step="1"')}${field("scale","提示词引导值",item.scale ?? 5,"number","",'min="0" max="100" step="0.1"')}${field("sampler","采样器",item.sampler || "k_euler_ancestral","text","",'maxlength="60"')}${field("strength","改图强度",item.strength ?? .6,"number","0—1，越大越不像原图。",'min="0" max="1" step="0.05"')}</div>`;
    if(kind!=="comfyui")body+=toggle("reference","支持参考图改图",kind==="openai"?"使用 /images/edits；DALL·E 3 和不少中转站不支持，不支持就关掉。":kind==="novelai"?"使用 img2img，只取第一张参考图。":"把对方发的图一起交给模型。",item.reference!==false)
      +`<details class="details"><summary>高级 · 附加请求参数</summary>${textarea("extra_body","附加请求参数（JSON）",item.extra_body && Object.keys(item.extra_body).length?JSON.stringify(item.extra_body,null,2):"","会合并进请求体，不能覆盖模型、提示词和参考图字段。",'rows="4" spellcheck="false"')}</details>`;
    else{
      const fields=item.workflow_fields || {};
      body+=textarea("workflow","API 工作流（JSON）",item.workflow && Object.keys(item.workflow).length?JSON.stringify(item.workflow,null,2):"","在 ComfyUI 里用「导出（API）」得到的 JSON，不是普通的工作流保存文件。",'rows="8" spellcheck="false" required')
        +`<p class="help-caption">把提示词等参数映射到工作流节点的输入，写成 节点ID.inputs.输入名，例如 6.inputs.text。没映射的输入沿用工作流里的值；映射了参考图才支持改图。</p><div class="form-grid">${mappings.map(([key,title])=>field("map_"+key,title,fields[key] || "","text","",`maxlength="200" ${key==="prompt"?"required":""} placeholder="${key==="prompt"?"6.inputs.text":""}"`)).join("")}${field("output_node","输出节点 ID（可选）",item.output_node || "","text","留空时取第一个有图片的输出。",'maxlength="40"')}</div>`;
    }
    openModal(saved?"编辑生图服务":"添加 "+kinds[kind],saved?kinds[kind]+"；改完可以在页面底部测试生图。":"保存后可以在页面底部测试生图；第一个添加的服务会自动设为当前使用。",body,"imagegen-service",item,saved?"保存服务":"添加服务");
  }

  async function saveServices(list,patch={}){
    state.imagegen=await api("/api/imagegen",{method:"PUT",body:{services:list,...patch}});
  }

  ui.handleAction=async(action,target)=>{
    if(action==="imagegen-add"){serviceForm({kind:target.dataset.kind});return true;}
    if(action==="imagegen-edit"||action==="imagegen-delete"||action==="imagegen-use"){
      const item=services().find(x=>x.id===target.dataset.id);
      if(!item)throw new Error("请刷新生图页面后重试。");
      if(action==="imagegen-edit"){serviceForm(item);return true;}
      if(action==="imagegen-use"){await api("/api/imagegen",{method:"PUT",body:{service_id:item.id}});toast(`已改用「${item.name}」`);await renderPage();return true;}
      const current=config().service_id===item.id;
      await confirmAction(`删除「${item.name}」？`,current?"这是当前使用的服务，删除后生图会关闭，直到选择另一个服务。":"删除后它保存的密钥也会一起删除。",async()=>{
        await saveServices(services().filter(x=>x.id!==item.id).map(plain),current?{service_id:"",enabled:false}:{});
        toast("生图服务已删除");await renderPage();
      },"删除服务");
      return true;
    }
    return oldAction(action,target);
  };

  ui.handleSubmit=async(kind,form)=>{
    if(kind==="imagegen-settings"){
      const data=getForm(form);
      await api("/api/imagegen",{method:"PUT",body:{enabled:data.enabled===true,service_id:data.service_id || "",daily_per_user:number(data.daily_per_user,"每人每天最多张数")}});
      toast("生图设置已保存");await renderPage();return true;
    }
    if(kind==="imagegen-service"){
      const data=getForm(form),record=state.modalRecord || {},serviceKind=record.kind;
      const item={...plain(record),kind:serviceKind,name:data.name,endpoint:data.endpoint,model:data.model || "",
        timeout_seconds:number(data.timeout_seconds,"等待上限"),negative_prompt:data.negative_prompt || ""};
      if(data.api_key)item.api_key=data.api_key;
      if(data.clear_api_key===true)item.clear_api_key=true;
      if(serviceKind==="openai"||serviceKind==="gemini")item.natural_negative=data.natural_negative===true;
      if(serviceKind==="openai")item.quality=data.quality || "";
      if(serviceKind==="novelai")Object.assign(item,{steps:number(data.steps,"采样步数"),scale:number(data.scale,"提示词引导值"),sampler:data.sampler,strength:number(data.strength,"改图强度")});
      if(serviceKind!=="comfyui")Object.assign(item,{reference:data.reference===true,extra_body:json(data.extra_body,"附加请求参数")});
      else{
        item.workflow=json(data.workflow,"API 工作流");
        item.workflow_fields=Object.fromEntries(mappings.map(([key])=>[key,String(data["map_"+key] || "").trim()]).filter(([,value])=>value));
        item.output_node=String(data.output_node || "").trim();
      }
      const others=services().filter(x=>x.id!==record.id).map(plain);
      const list=record.id?services().map(x=>x.id===record.id?item:plain(x)):[...others,item];
      const first=!config().service_id && !record.id;
      await saveServices(list);
      if(first){const added=services().at(-1);if(added)state.imagegen=await api("/api/imagegen",{method:"PUT",body:{service_id:added.id}});}
      closeModal();toast(record.id?"生图服务已保存":"生图服务已添加");await renderPage();return true;
    }
    if(kind==="imagegen-personas"){
      const data=getForm(form),personas={};
      for(const persona of config().persona_list || []){
        const prompt=String(data["prompt__"+persona.id] || "").trim(),negative=String(data["negative__"+persona.id] || "").trim();
        if(prompt||negative)personas[persona.id]={prompt,negative_prompt:negative};
      }
      await api("/api/imagegen",{method:"PUT",body:{personas}});
      toast("人格外貌已保存");await renderPage();return true;
    }
    if(kind==="imagegen-test"){
      const data=getForm(form);
      state.imagegenTestPrompt=data.prompt;state.imagegenTestAspect=data.aspect;
      toast("正在生成测试图，可能要几十秒…");
      state.imagegenTest=await api("/api/imagegen/test",{method:"POST",body:{prompt:data.prompt,aspect:data.aspect}});
      toast("测试图已生成");await renderPage();return true;
    }
    return oldSubmit(kind,form);
  };
  return {render};
})();
