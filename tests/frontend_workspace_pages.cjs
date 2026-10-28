"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const calls=[],nodes=new Map(),timers=new Map();let timerId=0,modal=null,copied="",nextLog=0;
const escape=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const state={page:"models",scope:"web:test",scopes:[{id:"web:test",model_id:""}],authenticated:true,settings:{},sending:false};
const catalog={providers:[{id:"p1",name:'Provider <script>bad</script>',base_url:"https://models.example/v1",api_key_set:true,enabled:true}],models:[{id:"m1",provider_id:"p1",name:"Chat",model:"chat-a",enabled:true,effective_enabled:true,tools:true}],routing:{chat:"m1"}};
let responder=async(url,options)=>{
  if(url==="/api/model-providers" && options?.method==="POST")return{id:"p2"};
  if(url==="/api/model-providers")return catalog;
  if(url==="/api/model/test")return{ok:true};
  if(url.endsWith("/discover"))return{models:['chat-a','other<script>x</script>']};
  if(url.startsWith("/api/logs?")){nextLog++;return{lines:[{id:String(nextLog),time:"10:00:00",level:"ERROR",message:'<script>secret-key-placeholder</script>'}],cursor:String(nextLog)};}
  if(url==="/api/model-catalog/models")return{models:[{id:"m-new",provider_id:options.body.provider_id,model:options.body.models[0],enabled:true}],added:[{id:"m-new"}]};
  return{};
};
const sandbox={window:{},state,URLSearchParams,AbortController,document:{hidden:false},routeNames:{chat:"聊天",memory:"记忆"},
  setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
  e:escape,api:async(url,options)=>{calls.push([url,options]);return responder(url,options);},query:params=>new URLSearchParams(params).toString(),
  heading:(a,b,c,d="")=>a+b+c+d,icon:()=>"",tag:text=>`<small>${escape(text)}</small>`,notice:text=>`<p>${escape(text)}</p>`,empty:(a,b,c,d,f)=>b+c+`<button data-action="${d}">${f}</button>`,number:value=>String(value||0),fmt:value=>String(value),
  button:(text,action,style,symbol,attrs="")=>`<button data-action="${action}" ${attrs}>${escape(text)}</button>`,field:(name,title,value)=>`<input name="${name}" value="${escape(value)}">`,toggle:()=>"",select:(name,title,options)=>`<select name="${name}">${options.map(([id,label])=>`<option value="${escape(id)}">${escape(label)}</option>`).join("")}</select>`,textarea:()=>"",
  $:selector=>nodes.get(selector)||null,$$:(selector,form)=>selector.includes(":checked") ? (form.checked || []) : [],getForm:form=>form.values,
  openModal:(...args)=>{modal=args;state.modalKind=args[3];state.modalRecord=args[4];},closeModal:()=>{modal=null;},confirmAction:async(...args)=>{modal=args;},
  bootstrap:async()=>{},navigate:async page=>state.page=page,renderPage:async()=>{},toast:()=>{},scope:()=>state.scopes[0],navigator:{clipboard:{writeText:async text=>copied=text}},
};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static/workspace-pages.js"),"utf8"),sandbox);const ui=sandbox.window.ShioriPages;
const plain=value=>JSON.parse(JSON.stringify(value));
async function main(){
  assert.equal(ui.recommendChatModel(['text-embedding-3-small','gpt-chat-mini','gpt-reasoning-preview']),'gpt-chat-mini');
  assert.equal(ui.recommendChatModel(['bge-large','reranker','whisper','tts-1']), '');
  assert.equal(ui.recommendChatModel(['model-a','model-b'],'model-b'),'model-b','an explicit valid choice is preserved');
  assert.equal(ui.recommendChatModel(['deepseek-reasoner','deepseek-chat'],'deepseek-chat'),'deepseek-chat');
  const html=await ui.renderModels();assert.match(html,/模型供应商/);assert.match(html,/chat-a/);assert.match(html,/&lt;script&gt;bad/);assert.ok(!html.includes("<script>"));
  assert.match(await ui.chatModelControl(),/继承全局聊天模型/);
  await ui.handleAction("edit-catalog-model",{dataset:{id:"m1"}});
  assert.match(modal[2],/name="timeout_seconds" value="90"/);
  await ui.handleSubmit("catalog-model",{values:{id:"m1",name:"Chat",temperature:"0.7",input_price:"0",output_price:"0",vision:false,tools:true,timeout_seconds:"240"}});
  assert.equal(calls.at(-1)[0],"/api/model-catalog/models/m1");assert.equal(calls.at(-1)[1].body.timeout_seconds,240);
  await ui.handleChange({id:"chat-model-id",value:"m1"});assert.equal(calls.at(-1)[0],"/api/scopes/web%3Atest");assert.deepEqual(plain(calls.at(-1)[1].body),{model_id:"m1"});
  await ui.handleChange({id:"chat-model-id",value:""});assert.deepEqual(plain(calls.at(-1)[1].body),{model_id:""});
  const discovery={innerHTML:""};nodes.set("#source-discovery",discovery);
  await ui.handleAction("edit-model-source",{dataset:{id:"p1"}});
  const form={values:{id:"p1",name:"Provider",base_url:"https://unsaved.example/v1",api_key:"fixture-unsaved-secret",enabled:true},checked:[],closest(){return this;}};
  await ui.handleAction("fetch-source-models",{dataset:{},closest:()=>form});
  const request=calls.at(-1);assert.equal(request[0],"/api/model-providers/p1/discover");assert.equal(request[1].body.base_url,"https://unsaved.example/v1");assert.equal(request[1].body.api_key,"fixture-unsaved-secret");
  assert.ok(!discovery.innerHTML.includes("fixture-unsaved-secret"));assert.match(discovery.innerHTML,/other&lt;script&gt;/);assert.ok(!discovery.innerHTML.includes("<script>"));
  ui.handleInput({name:"api_key",closest:()=>form});assert.ok([...timers.values()].some(item=>item.delay===700),"complete connection drafts schedule automatic model discovery");
  await assert.rejects(ui.handleAction("delete-model-source",{dataset:{id:"p1"}}),/先移除/);
  await ui.handleAction("toggle-catalog-model",{dataset:{id:"m1"}});assert.equal(calls.at(-1)[0],"/api/model-catalog/models/m1");assert.deepEqual(plain(calls.at(-1)[1].body),{enabled:false});
  await ui.handleAction("route-chat-model",{dataset:{id:"m1"}});assert.equal(calls.at(-1)[0],"/api/model-catalog/routing");assert.deepEqual(plain(calls.at(-1)[1].body),{chat:"m1"});
  await ui.handleAction("route-chat-model",{dataset:{id:"default"}});assert.deepEqual(plain(calls.at(-1)[1].body),{chat:""},"the original default model is represented by an empty global route");
  await ui.handleSubmit("catalog-routing",{values:{chat:"default",memory:"m1"}});assert.deepEqual(plain(calls.at(-1)[1].body),{chat:"",memory:"m1"},"task routing normalizes the original default model");
  sandbox.routeNames.distillation="人格生成与蒸馏";
  const analysisRoutes=await ui.renderModels();assert.match(analysisRoutes,/name="distillation"/);assert.match(analysisRoutes,/继承日常对话模型/);
  await ui.handleSubmit("catalog-routing",{values:{chat:"default",memory:"",distillation:"m1"}});
  assert.deepEqual(plain(calls.at(-1)[1].body),{chat:"",memory:"",distillation:"m1"},"persona generation saves its own model route");
  const qq=ui.qqIngress({message_events_received:0,heartbeat_events_received:3,diagnostic:"只有心跳，尚未收到消息",last_message_result:{reason:"<img>"}});assert.match(qq,/只有心跳/);assert.match(qq,/心跳 3/);
  assert.ok(!ui.qqIngress({message_events_received:1,diagnostic:"<script>bad</script>"}).includes("<script>"));
  const rejected=ui.qqIngress({message_events_received:1,last_message_result:{code:"private_not_allowed",stage:"blocked",user_id:"123456",scope_id:"qq:private:123456"}});
  assert.match(rejected,/最近发送人：123456/);assert.match(rejected,/黑名单/);assert.match(rejected,/白名单/);assert.match(rejected,/刷新接收状态/);
  assert.match(ui.qqIngress({message_events_received:1,last_message_result:{code:"sent",stage:"sent"}}),/平台已确认送达/);

  let completed=false;await ui.startQuickModel(async()=>{completed=true;});
  assert.equal(modal[0],"连接聊天模型");assert.equal(modal[5],"测试并用于聊天 →");
  const quickForm={values:{id:"",name:"New Provider",base_url:"https://new.example/v1",api_key:"fixture-new-secret",enabled:"true"},checked:[],closest(){return this;}};
  const beforeMissing=calls.length;await assert.rejects(ui.handleSubmit("model-source",quickForm),/请选择一个聊天模型/);assert.equal(calls.length,beforeMissing,"a missing model is reported before any source is saved");
  await ui.handleAction("fetch-source-models",{dataset:{},closest:()=>quickForm});
  assert.match(discovery.innerHTML,/type="radio" name="quick_chat_model"/);assert.ok(!discovery.innerHTML.includes("select-all-discovered"),"quick setup chooses one chat model");
  quickForm.values.quick_chat_model="chat-a";quickForm.values.manual_model="manual-chat";
  const sourceName={value:"Old"},sourceUrl={value:"https://old.example/v1"},sourceKey={value:"fixture-new-secret",focus(){}};
  nodes.set('[name="name"]',sourceName);nodes.set('[name="base_url"]',sourceUrl);nodes.set('[name="api_key"]',sourceKey);
  await ui.handleAction("source-preset",{dataset:{provider:"deepseek"},closest:()=>quickForm});
  assert.equal(sourceName.value,"DeepSeek");assert.equal(sourceUrl.value,"https://api.deepseek.com/v1");assert.equal(sourceKey.value,"","changing provider presets removes a draft key for the previous service");
  const beforeSave=calls.length;await ui.handleSubmit("model-source",quickForm);
  const savedCalls=calls.slice(beforeSave);
  assert.equal(savedCalls[0][0],"/api/model-providers");assert.equal(savedCalls[0][1].body.enabled,true);
  assert.deepEqual(plain(savedCalls[1][1].body),{provider_id:"p2",model_type:"chat",models:["manual-chat"]},"manual name is the explicit fallback choice");
  assert.equal(savedCalls[2][0],"/api/model/test");assert.deepEqual(plain(savedCalls[2][1].body),{provider_id:"m-new"});
  assert.equal(savedCalls[3][0],"/api/model-catalog/routing");assert.deepEqual(plain(savedCalls[3][1].body),{chat:"m-new"},"quick setup binds only after a successful model test and preserves other routes");
  assert.equal(completed,true,"saving a model returns to the next quick-start step");

  await ui.startQuickModel();
  const manualNode={value:""};nodes.set('[name="manual_model"]',manualNode);
  const recommendedForm={dataset:{},values:{id:"",name:"DeepSeek",base_url:"https://api.deepseek.com/v1",api_key:"fixture-key"},checked:[]};
  await ui.handleAction("source-preset",{dataset:{provider:"deepseek"},closest:()=>recommendedForm});
  assert.equal(manualNode.value,'deepseek-chat','a provider hint can be tested even when its model directory is unavailable');
  const originalResponder=responder;
  responder=async(url,options)=>url.endsWith('/discover') ? {models:['text-embedding-3-small','deepseek-reasoner','deepseek-chat']} : originalResponder(url,options);
  await ui.handleAction("fetch-source-models",{dataset:{},closest:()=>recommendedForm});
  assert.match(discovery.innerHTML,/value="deepseek-chat" checked/);
  assert.match(discovery.innerHTML,/已推荐 deepseek-chat/);
  assert.equal(manualNode.value,'','a directory-confirmed recommendation replaces the unverified preset hint');
  recommendedForm.values.quick_chat_model='deepseek-reasoner';
  await ui.handleAction("fetch-source-models",{dataset:{},closest:()=>recommendedForm});
  assert.match(discovery.innerHTML,/value="deepseek-reasoner" checked/,'refresh preserves a model deliberately chosen by the user');
  let finishStale;
  responder=async()=>new Promise(resolve=>{finishStale=resolve;});
  const fetching=ui.handleAction("fetch-source-models",{dataset:{},closest:()=>recommendedForm});
  ui.handleInput({name:'api_key',value:'new-fixture-key',closest:()=>recommendedForm});
  finishStale({models:['stale-chat']});await fetching;
  assert.ok(!discovery.innerHTML.includes('stale-chat'),'old credentials cannot produce a visible recommendation after the draft changes');
  responder=async(url,options)=>url==='/api/model/test' ? {ok:false} : originalResponder(url,options);
  recommendedForm.values.quick_chat_model='deepseek-chat';
  const failedStart=calls.length;
  await assert.rejects(ui.handleSubmit('model-source',recommendedForm),/测试未通过/);
  assert.ok(!calls.slice(failedStart).some(([url])=>url==='/api/model-catalog/routing'),'failed validation keeps the previous default');
  responder=originalResponder;

  ui.close();state.page="logs";const feed={innerHTML:"",scrollTop:0,scrollHeight:20};nodes.set("#runtime-log-feed",feed);nodes.set("#runtime-log-status",{textContent:""});
  const logs=await ui.renderLogs();assert.match(logs,/暂停更新/);assert.match(logs,/api\/logs\/export/);assert.match(logs,/&lt;script&gt;/);assert.ok(!logs.includes("<script>"));
  ui.afterRender();const poll=[...timers.values()].find(item=>item.delay===2000);assert.ok(poll,"logs page schedules two-second incremental polling");await poll.fn();
  assert.ok(calls.at(-1)[0].includes("after=1"));assert.match(feed.innerHTML,/data-log-id="2"/);
  await ui.handleAction("copy-runtime-logs",{dataset:{}});assert.match(copied,/ERROR/);
  await ui.handleAction("toggle-log-tail",{dataset:{},textContent:""});assert.ok(![...timers.values()].some(item=>item.delay===2000),"paused logs stop polling");
  state.page="chat";ui.afterRender();assert.equal(timers.size,0,"leaving logs closes its only timer");
  state.page="logs";await ui.handleChange({id:"runtime-log-level",value:"ERROR"});
  const filtered=await ui.renderLogs();assert.match(filtered,/href="\/api\/logs\/export\?level=ERROR&amp;q="/);assert.ok(!filtered.includes("${e(query"),"export links contain the real current filters");
  console.log("Model catalog and runtime-log UI checks passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
