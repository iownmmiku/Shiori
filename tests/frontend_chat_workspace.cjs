"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const plain=value=>JSON.parse(JSON.stringify(value));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
function fixture(){
  const calls=[],notices=[],timers=new Map(),nodes=new Map();let serial=0;
  const state={scope:"web:default",page:"chat",renderId:0,scopes:[{id:"web:default",name:"默认对话"},{id:"web:other",name:"另一对话"},{id:"qq:42",name:"QQ消息"}],images:[],draft:"",settings:{},boot:{},chatMode:"chat",sending:false};
  const runtime={attachments:[],attachmentScope:state.scope};
  const database={projects:[{id:"p1",name:"<写作>",description:"讨论文章"},{id:"p2",name:"学习",description:""}],conversations:[
    {id:"web:default",name:"日常聊天",project_id:null,pinned:false,archived:false,updated_at:"2026-10-30"},
    {id:"web:other",name:"文章构思",project_id:"p1",pinned:true,archived:false,updated_at:"2026-10-29"},
    {id:"web:archived",name:"旧对话",project_id:"p2",pinned:false,archived:true,updated_at:"2026-10-28"},
    {id:"qq:42",name:"QQ消息",project_id:null,pinned:false,archived:false}
  ]};
  const input={value:"",focus(){}};nodes.set("#chat-text",input);nodes.set("#attachment-chips",{innerHTML:""});nodes.set("#chat-conversation-list",{innerHTML:""});nodes.set("#chat-conversation-list-count",{textContent:""});
  const config={reply:async()=>({reply:"完成"}),release:async()=>({}),messages:async id=>({items:[{id,body:id}]}),modal:null,renders:0,closes:0,navigations:[],newScopeValues:{persona_id:"inherited-persona",model_id:"inherited-model",debounce_enabled:true,debounce_seconds:7,debounce_max_wait_seconds:21}};
  let ui;
  const e=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const render=async()=>{config.renders++;state.renderId++;input.value=state.draft;};
  const sandbox={window:{},state,AbortController,Date,URLSearchParams,TextDecoder,
    localStorage:{setItem(){}},setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id),
    $:selector=>nodes.get(selector),e,scope:()=>state.scopes.find(item=>item.id===state.scope)||{},
    rows:value=>Array.isArray(value)?value:value?.items||[],query:value=>new URLSearchParams(value).toString(),icon:name=>`<svg data-icon="${e(name)}"></svg>`,
    button:(text,action,style="",glyph="",extra="")=>`<button data-action="${e(action)}" ${extra}>${e(text)}</button>`,
    notice:()=>"",heading:()=>"",personaName:()=>"初音未来",renderMessage:item=>`<p>${e(item.body)}</p>`,toolRunCard:()=>"",
    field:(name,label,value)=>`<input name="${e(name)}" value="${e(value)}">`,textarea:(name,label,value)=>`<textarea name="${e(name)}">${e(value)}</textarea>`,select:()=>"",
    openModal:(...args)=>{config.modal=args;state.modalKind=args[3]||"";nodes.set("#modal",{open:true});nodes.set("#modal-content",{firstElementChild:{}});},
    closeModal:()=>{config.closes++;config.modal=null;state.modalKind=null;if(nodes.has("#modal"))nodes.get("#modal").open=false;},getForm:form=>form,
    toast:(...args)=>notices.push(args),renderPage:render,bootstrap:render,navigate:async page=>{config.navigations.push(page);state.draft=input.value;state.page=page;await render();},
    api:async(url,options={})=>{
      const body=options.body||{};calls.push([url,options.method||"GET",plain(body)]);
      if(config.intercept){const override=await config.intercept(url,options);if(override?.handled)return override.value;}
      if(url==="/api/chat/workspace")return plain(database);
      if(url==="/api/scopes")return database.conversations.map(item=>({...plain(item),kind:item.id.startsWith("web:")?"web":"group",...(item.id.startsWith("web:new")?config.newScopeValues:{})}));
      if(url==="/api/chat")return config.reply(options);
      if(url==="/api/chat/attachments/release")return config.release(options);
      if(url.startsWith("/api/messages?"))return config.messages(new URLSearchParams(url.split("?")[1]).get("scope_id"));
      if(url==="/api/chat/conversations"&&options.method==="POST"){
        const item={id:"web:new"+serial++,name:body.name,project_id:body.project_id,pinned:false,archived:false};database.conversations.push(item);return plain(item);
      }
      if(url.startsWith("/api/chat/conversations/")&&options.method==="PATCH"){
        const item=database.conversations.find(item=>item.id===decodeURIComponent(url.split("/").pop()));Object.assign(item,body);return plain(item);
      }
      if(url==="/api/chat/projects"&&options.method==="POST"){
        const item={id:"p"+serial++,...body};database.projects.push(item);return plain(item);
      }
      if(url.startsWith("/api/chat/projects/")&&options.method==="PATCH"){
        const item=database.projects.find(item=>item.id===decodeURIComponent(url.split("/").pop()));Object.assign(item,body);return plain(item);
      }
      if(url.startsWith("/api/chat/projects/")&&options.method==="DELETE"){
        const id=decodeURIComponent(url.split("/").pop());database.projects=database.projects.filter(item=>item.id!==id);for(const item of database.conversations)if(item.project_id===id)item.project_id=null;return{};
      }
      return {items:[]};
    }
  };
  vm.createContext(sandbox);
  for(const file of ["chat-projects.js","experience-chat.js"])vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static",file),"utf8"),sandbox);
  ui=sandbox.window.ShioriExperienceChat.create(runtime,{inactive:()=>true,jobDetail:()=>""});
  const draft=(text,attachments=[],images=[])=>{state.draft=text;input.value=text;runtime.attachments=attachments;runtime.attachmentScope=state.scope;state.images=images;};
  const action=(name,data={})=>ui.handleAction(name,{dataset:data});
  return{ui,state,runtime,database,config,nodes,input,calls,timers,notices,draft,action,openModal:kind=>sandbox.openModal("测试弹窗","","",kind)};
}
async function main(){
  {const f=fixture(),html=await f.ui.renderChat();
    assert.equal(f.ui.handleAction("attachment-reuse",{dataset:{id:"a",scope:"web:default"}}),false,"unrelated actions must not introduce an await before their owner scope is captured");
    assert.equal(f.ui.handleAction("scope-chat",{dataset:{id:"qq:42"}}),false,"QQ scope changes remain owned by the monitor handler");
    assert.equal(f.ui.handleSubmit("model",{}),false,"unrelated forms keep the synchronous dispatch path");
    assert.ok(html.includes("chat-workspace-standalone"));assert.ok(html.includes("&lt;写作&gt;"));assert.ok(!html.includes("QQ消息"));
    assert.ok(html.includes("返回机器人管理"));assert.ok(html.includes('id="chat-mode"'));assert.ok(html.includes('data-action="attachment-options"'));
    assert.ok(html.indexOf("文章构思")<html.indexOf("日常聊天"),"pinned conversations precede recent unpinned conversations");
    assert.equal(f.state.scopes[0].name,"日常聊天");
    f.ui.handleInput({id:"chat-conversation-search",value:"写作"});assert.ok(f.nodes.get("#chat-conversation-list").innerHTML.includes("文章构思"));assert.ok(!f.nodes.get("#chat-conversation-list").innerHTML.includes("日常聊天"));
    f.ui.handleInput({id:"chat-conversation-search",value:"不存在"});assert.equal(f.nodes.get("#chat-conversation-list-count").textContent,"0");
    f.ui.handleInput({id:"chat-conversation-search",value:""});await f.action("chat-filter",{view:"archived"});
    const archived=f.ui.sessionSidebar();assert.ok(archived.includes("旧对话"));assert.ok(!archived.includes("文章构思"));
  }
  {const f=fixture();await f.ui.renderChat();const A={id:"a",name:"原资料"},B={id:"b",name:"新资料"};
    f.draft("未发送的原草稿",[A],[{data:"image-a"}]);await f.action("scope-chat",{id:"web:other"});
    assert.equal(f.state.draft,"");assert.deepEqual(plain(f.runtime.attachments),[]);
    f.draft("另一个草稿",[B]);await f.action("scope-chat",{id:"web:default"});
    assert.equal(f.input.value,"未发送的原草稿");assert.deepEqual(plain(f.runtime.attachments),[A]);assert.equal(f.state.images.length,1);
    await f.action("scope-chat",{id:"web:other"});assert.equal(f.state.draft,"另一个草稿");assert.deepEqual(plain(f.runtime.attachments),[B]);
    assert.ok(!f.calls.some(([url])=>url.endsWith("/release")),"switching keeps every parked draft's attachments alive");
    await f.action("chat-filter",{view:"project",id:"p1"});await f.action("new-chat");
    assert.equal(f.calls.find(([url,method])=>url==="/api/chat/conversations"&&method==="POST")[2].project_id,"p1");
    assert.equal(f.state.draft,"");await f.action("scope-chat",{id:"web:other"});assert.equal(f.state.draft,"另一个草稿");
  }
  {const f=fixture();await f.ui.renderChat();f.draft("管理操作中的草稿");
    await f.ui.handleSubmit("chat-conversation-save",{id:"web:other",name:"  新名称  ",project_id:"p2",pinned:"false"});
    assert.equal(f.database.conversations[1].name,"新名称");assert.equal(f.database.conversations[1].project_id,"p2");assert.equal(f.database.conversations[1].pinned,false);assert.equal(f.state.draft,"管理操作中的草稿");
    await f.action("chat-conversation-archive",{id:"web:other",archived:"true"});assert.equal(f.database.conversations[1].archived,true);
    await f.action("chat-conversation-archive",{id:"web:other",archived:"false"});assert.equal(f.database.conversations[1].archived,false);
    await f.action("chat-project-delete",{id:"p2"});assert.equal(f.database.conversations.length,4);assert.equal(f.database.conversations[1].project_id,null);
    assert.ok(!f.calls.some(([url])=>/messages|memories|settings/.test(url)&&f.calls.find(call=>call[0]===url)?.[1]!=="GET"));
  }
  {const f=fixture();await f.ui.renderChat();
    await f.ui.handleSubmit("chat-project-save",{name:"新项目",description:"仅分类"});
    const created=f.database.projects.at(-1);assert.equal(created.name,"新项目");
    await f.ui.handleSubmit("chat-project-save",{id:created.id,name:"项目改名",description:"整理文章"});assert.equal(f.database.projects.at(-1).name,"项目改名");
    await f.action("new-chat");assert.equal(f.database.conversations.at(-1).project_id,created.id);
    const fullScope=f.state.scopes.find(item=>item.id===f.state.scope);assert.equal(fullScope.persona_id,"inherited-persona");assert.equal(fullScope.model_id,"inherited-model");
    assert.equal(fullScope.debounce_seconds,7);assert.ok(f.calls.some(([url])=>url==="/api/scopes"));
    f.draft("首条消息采用继承的防抖设置");await f.ui.sendChat();assert.equal(f.state.debouncePending,true);assert.equal(f.calls.filter(([url])=>url==="/api/chat").length,0);f.ui.parkDraft();
    assert.equal(await f.action("scope-chat",{id:"qq:42"}),false);assert.equal(f.ui.activateScope("qq:42"),false);
  }
  {const f=fixture();f.state.settings.debounce={enabled:true,seconds:3,max_wait_seconds:30};f.draft("已收集的一句",[{id:"a"}]);await f.ui.sendChat();f.draft("编辑中的一句",[{id:"b"}]);
    f.ui.activateScope("web:other");assert.equal(f.timers.size,0);assert.equal(f.state.debouncePending,false);
    f.ui.activateScope("web:default");assert.equal(f.state.draft,"已收集的一句\n编辑中的一句");assert.deepEqual(plain(f.runtime.attachments),[{id:"a"},{id:"b"}]);
    assert.ok(!f.calls.some(([url])=>url==="/api/chat"));
  }
  for(const destination of ["qqChats","overview"]){const f=fixture();f.state.settings.debounce={enabled:true,seconds:3,max_wait_seconds:30};
    f.draft("收集中的内容",[{id:"a"}]);await f.ui.sendChat();f.draft("准备补充",[{id:"b"}],[{data:"image"}]);
    f.ui.parkDraft();f.state.page=destination;if(destination==="qqChats")f.state.scope="qq:42";
    assert.equal(f.timers.size,0);assert.equal(f.state.draft,"");
    f.ui.activateScope("web:default");f.state.page="chat";assert.equal(f.state.draft,"收集中的内容\n准备补充");assert.equal(f.state.images.length,1);assert.deepEqual(plain(f.runtime.attachments),[{id:"a"},{id:"b"}]);
  }
  for(const failed of [false,true])for(const returnBeforeCompletion of [false,true]){
    const f=fixture(),release=deferred(),A={id:"a",name:"原会话要移除的资料"},C={id:"c",name:"原会话要保留的资料"},B={id:"b",name:"新会话资料"};
    f.config.release=()=>release.promise;f.draft("原草稿",[A,C]);
    const removing=f.ui.removeAttachment(0),outcome=failed?assert.rejects(removing,/释放失败/):removing;
    assert.deepEqual(f.calls.at(-1)[2],{scope_id:"web:default",ids:["a"]});
    assert.deepEqual(plain(f.runtime.attachments),[A,C],"pending release keeps the visible selection");
    f.ui.activateScope("web:other");f.draft("新草稿",[B]);f.nodes.get("#attachment-chips").innerHTML="other-scope-chips";
    if(returnBeforeCompletion)f.ui.activateScope("web:default");
    if(failed)release.reject(new Error("释放失败"));else release.resolve({deleted:["a"]});await outcome;
    if(!returnBeforeCompletion){
      assert.deepEqual(plain(f.runtime.attachments),[B],"a late completion cannot splice the new scope's index 0");
      assert.equal(f.nodes.get("#attachment-chips").innerHTML,"other-scope-chips","a late completion cannot repaint another scope's chips");
      f.ui.activateScope("web:default");
    }
    assert.equal(f.state.draft,"原草稿");assert.deepEqual(plain(f.runtime.attachments),failed?[A,C]:[C],"returning to the owner restores only the successfully retained selection");
    f.ui.activateScope("web:other");assert.equal(f.state.draft,"新草稿");assert.deepEqual(plain(f.runtime.attachments),[B]);
  }
  {const f=fixture(),A={id:"a",name:"收集批次仍在使用"};f.state.settings.debounce={enabled:true,seconds:3,max_wait_seconds:30};
    f.draft("收集中的内容",[A]);await f.ui.sendChat();f.draft("另一条待写",[A]);
    const removing=f.ui.removeAttachment(0);f.ui.activateScope("web:other");await removing;f.ui.activateScope("web:default");
    assert.deepEqual(plain(f.runtime.attachments),[A],"parking a batch retains its shared reference even when a duplicate composer selection was removed");
    assert.equal(f.calls.filter(([url])=>url.endsWith("/release")).length,0);
  }
  {const f=fixture(),started=deferred(),response=deferred();f.config.reply=()=>{started.resolve();return response.promise;};f.draft("发送中的原消息");
    const sending=f.ui.sendChat();await started.promise;f.ui.activateScope("web:other");f.draft("另一会话仍在编辑");
    const html=await f.ui.renderChat();assert.ok(html.includes("另一对话正在回复"));assert.ok(!/<textarea[^>]+disabled/.test(html));
    response.reject(new Error("模拟断流"));await sending;assert.equal(f.state.draft,"另一会话仍在编辑");f.ui.activateScope("web:default");assert.equal(f.state.draft,"发送中的原消息");
    assert.equal(f.calls.filter(([url])=>url==="/api/chat").length,1);
  }
  {const f=fixture(),started=deferred(),response=deferred();f.config.reply=()=>{started.resolve();return response.promise;};f.draft("离开时仍在发送");
    const sending=f.ui.sendChat();await started.promise;f.ui.parkDraft();f.state.page="overview";response.reject(new Error("模拟失败"));await sending;
    f.ui.activateScope("web:default");f.state.page="chat";assert.equal(f.state.draft,"离开时仍在发送");
  }
  {const f=fixture(),first=deferred();f.config.messages=id=>id==="web:default"?first.promise:Promise.resolve({items:[{id,body:"正确的新会话"}]});
    const oldRender=f.ui.renderChat();f.ui.activateScope("web:other");const latest=await f.ui.renderChat();
    assert.ok(latest.includes("正确的新会话"));first.resolve({items:[{id:"old",body:"迟到的旧会话"}]});assert.equal(await oldRender,"");assert.equal(f.state.messages[0].body,"正确的新会话");
  }
  for(const stage of ["create","scopes"])for(const destination of ["other","qq","away-return"]){
    const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();f.draft("创建前的草稿",[{id:"original"}]);
    f.config.intercept=(url,options)=>{
      if(stage==="create"&&url==="/api/chat/conversations"&&options.method==="POST"||stage==="scopes"&&url==="/api/scopes"){
        const staleScopes=f.database.conversations.map(item=>({...plain(item),...f.config.newScopeValues}));started.resolve();
        return gate.promise.then(()=>stage==="scopes"?{handled:true,value:staleScopes}:undefined);
      }
    };
    const creating=f.action("new-chat");await started.promise;
    const duplicate=f.action("new-chat");
    assert.equal(f.calls.filter(([url,method])=>url==="/api/chat/conversations"&&method==="POST").length,1,"double clicks share a single pending creation");
    if(destination==="qq"){
      f.ui.parkDraft();f.state.page="qqChats";f.state.scope="qq:42";f.state.renderId++;
    }else{
      await f.action("scope-chat",{id:"web:other"});f.draft("用户后来编辑的草稿",[{id:"later"}]);
      if(destination==="away-return")await f.action("scope-chat",{id:"web:default"});
    }
    f.state.scopes=f.state.scopes.map(item=>item.id===f.state.scope?{...item,model_id:"newer-current-model"}:item);
    const expectedScope=f.state.scope,expectedDraft=f.state.draft,navigations=f.config.navigations.length;
    gate.resolve();await Promise.all([creating,duplicate]);
    assert.equal(f.state.scope,expectedScope,"late creation cannot steal the current scope");assert.equal(f.state.draft,expectedDraft);
    assert.equal(f.config.navigations.length,navigations,"late POST/scopes results cannot navigate after leaving and returning");
    assert.equal(f.state.scopes.find(item=>item.id===expectedScope).model_id,"newer-current-model","late scope lists cannot overwrite active configuration");
    const created=f.database.conversations.find(item=>item.id.startsWith("web:new")),full=f.state.scopes.find(item=>item.id===created.id);
    assert.equal(full.persona_id,"inherited-persona");assert.equal(full.model_id,"inherited-model");assert.equal(full.debounce_seconds,7);
  }
  {const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();
    f.config.intercept=(url,options)=>{if(url==="/api/chat/conversations"&&options.method==="POST"){started.resolve();return gate.promise;}};
    const creating=f.action("new-chat");await started.promise;f.openModal("unrelated");const laterModal=f.config.modal;gate.resolve();await creating;
    assert.equal(f.state.scope,"web:default");assert.equal(f.config.modal,laterModal);assert.equal(f.config.navigations.length,0);
  }
  const mutations=[
    {kind:"chat-project-save",form:{name:"创建中的项目",description:""},url:"/api/chat/projects",method:"POST"},
    {kind:"chat-project-save",form:{id:"p1",name:"修改后的项目",description:""},url:"/api/chat/projects/p1",method:"PATCH"},
    {kind:"chat-conversation-save",form:{id:"web:other",name:"修改后的对话",project_id:"p2",pinned:"true"},url:"/api/chat/conversations/web%3Aother",method:"PATCH"},
    {action:"chat-project-delete",data:{id:"p1"},url:"/api/chat/projects/p1",method:"DELETE"},
    {action:"chat-conversation-archive",data:{id:"web:other",archived:"true"},url:"/api/chat/conversations/web%3Aother",method:"PATCH"}
  ];
  for(const operation of mutations)for(const stage of ["write","refresh"]){
    const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();f.openModal(operation.kind||"chat-project-save");
    f.config.intercept=(url,options)=>{
      if(stage==="write"&&url===operation.url&&options.method===operation.method||stage==="refresh"&&url==="/api/chat/workspace"){
        started.resolve();return gate.promise;
      }
    };
    const perform=()=>operation.kind?f.ui.handleSubmit(operation.kind,operation.form):f.action(operation.action,operation.data);
    const saving=perform();await started.promise;const duplicate=perform();
    assert.equal(f.calls.filter(([url,method])=>url===operation.url&&method===operation.method).length,1,"duplicate project/conversation mutations are suppressed");
    // Replace with the same modal kind: checking only modalKind would close it.
    f.openModal(operation.kind||"chat-project-save");const replacement=f.config.modal,renders=f.config.renders;
    f.draft("等待期间继续编辑的草稿");gate.resolve();await Promise.all([saving,duplicate]);
    assert.equal(f.config.modal,replacement);assert.equal(f.config.closes,0);assert.equal(f.config.renders,renders);
    assert.equal(f.state.draft,"等待期间继续编辑的草稿");assert.equal(f.state.scope,"web:default");
    assert.ok(f.ui.sessionSidebar().includes('id="chat-conversation-list-title">全部对话'),"late project save cannot switch the user's list filter");
  }
  {const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();f.openModal("chat-project-save");
    f.config.intercept=(url,options)=>{if(url==="/api/chat/projects"&&options.method==="POST"){started.resolve();return gate.promise;}};
    const saving=f.ui.handleSubmit("chat-project-save",{name:"失败的项目"});await started.promise;f.openModal("newer-form");const replacement=f.config.modal;
    gate.reject(new Error("保存失败"));assert.equal(await saving,true);assert.equal(f.config.modal,replacement);assert.ok(f.notices.some(([message])=>message==="保存失败"));
  }
  {const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();let delayed=false;
    f.config.intercept=url=>{if(url==="/api/chat/workspace"&&!delayed){delayed=true;const stale=plain(f.database);started.resolve();return gate.promise.then(()=>({handled:true,value:stale}));}};
    const oldRead=f.ui.renderChat();await started.promise;
    await f.ui.handleSubmit("chat-conversation-save",{id:"web:other",name:"写入后新名称",project_id:"p1",pinned:"true"});
    gate.resolve();await oldRead;
    assert.ok(f.ui.sessionSidebar().includes("写入后新名称"));assert.ok(!f.ui.sessionSidebar().includes("文章构思"),"a pre-write workspace receipt cannot replace fresh mutation data");
  }
  for(const stage of ["workspace","scopes"]){
    const f=fixture(),started=deferred(),gate=deferred();let delayed=false;
    f.config.intercept=url=>{
      if(!delayed&&url===(stage==="workspace"?"/api/chat/workspace":"/api/scopes")){
        delayed=true;const snapshot=stage==="workspace"?plain(f.database):plain(f.database.conversations);started.resolve();return gate.promise.then(()=>({handled:true,value:snapshot}));
      }
    };
    const old=f.ui.renderChat();await started.promise;f.state.authGeneration=1;f.state.authenticated=false;f.ui.close();f.state.scopes=[{id:"web:fresh",name:"新登录",model_id:"fresh-model"}];
    const callsBefore=f.calls.length;gate.resolve();await old;
    assert.equal(f.calls.length,callsBefore,"old workspace receipts cannot start more reads after auth teardown");
    assert.deepEqual(plain(f.state.scopes),[{id:"web:fresh",name:"新登录",model_id:"fresh-model"}],"old workspace/scopes receipts never merge into the next auth generation");
  }
  for(const stage of ["create","scopes"]){
    const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();let delayed=false;
    f.config.intercept=(url,options)=>{
      if(!delayed&&(stage==="create"&&url==="/api/chat/conversations"&&options.method==="POST"||stage==="scopes"&&url==="/api/scopes")){
        delayed=true;const stale=f.database.conversations.map(item=>({...plain(item),...f.config.newScopeValues}));started.resolve();return gate.promise.then(()=>stage==="scopes"?{handled:true,value:stale}:undefined);
      }
    };
    const old=f.action("new-chat");await started.promise;f.state.authGeneration=1;f.state.authenticated=false;f.ui.close();f.state.scopes=[];
    const reads=f.calls.filter(([url])=>url==="/api/scopes").length,notices=f.notices.length;
    gate.resolve();await old;assert.equal(f.calls.filter(([url])=>url==="/api/scopes").length,reads);assert.equal(f.config.navigations.length,0);assert.equal(f.notices.length,notices);assert.deepEqual(plain(f.state.scopes),[]);
  }
  {const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();let delayed=false;
    f.config.intercept=(url,options)=>{if(!delayed&&url==="/api/chat/conversations"&&options.method==="POST"){delayed=true;started.resolve();return gate.promise;}};
    const old=f.action("new-chat");await started.promise;f.state.authGeneration=1;f.ui.close();f.state.authenticated=true;
    await f.action("new-chat");const newerScope=f.state.scope;assert.ok(newerScope.startsWith("web:new"),"a prior session's pending mutation lock cannot block a new login");
    const scopes=plain(f.state.scopes);gate.resolve();await old;assert.equal(f.state.scope,newerScope);assert.deepEqual(plain(f.state.scopes),scopes);
  }
  {const f=fixture(),started=deferred(),gate=deferred();await f.ui.renderChat();f.openModal("chat-project-save");
    f.config.intercept=(url,options)=>{if(url==="/api/chat/projects"&&options.method==="POST"){started.resolve();return gate.promise;}};
    const old=f.ui.handleSubmit("chat-project-save",{name:"退出前项目"});await started.promise;f.state.authGeneration=1;f.ui.close();f.state.authenticated=true;f.openModal("fresh-login-modal");
    const modal=f.config.modal,reads=f.calls.length,notices=f.notices.length;gate.resolve();await old;
    assert.equal(f.config.modal,modal);assert.equal(f.calls.length,reads);assert.equal(f.notices.length,notices,"a stale write cannot refresh or toast into a new auth session");
  }
  console.log("PASS: project organization, escaped/sidebar-only local data, scoped drafts, parked collection, streaming switch and stale render guards");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
