"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const calls = [], nodes = new Map(); let modal = null;
const escaped = value => String(value ?? "").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
let responder = async () => ({});
const state={scope:"web:default",scopes:[{id:"web:default",name:"本地"}],settings:{model:{},computer:{}},personas:[],images:[],boot:{setup_required:false},page:"chat",filters:{}};
const sandbox={window:{ShioriQuickStart:{diagnosticsPanel:()=>"",modelSelection:()=>({provider_id:"default"})}},state,URLSearchParams,FormData,AbortController,EventSource:class{close(){}addEventListener(){}},
  e:escaped,configuredModel:()=>state.settings.model,query:params=>new URLSearchParams(params).toString(),rows:value=>Array.isArray(value)?value:value.items||[],
  heading:(...args)=>args.join(""),button:(text,action,style,symbol,attrs="")=>`<button data-action="${action}" ${attrs}>${escaped(text)}</button>`,
  icon:()=>"<svg></svg>",tag:value=>`<span>${escaped(value)}</span>`,notice:value=>`<p>${escaped(value)}</p>`,fmt:value=>String(value),empty:()=>"empty",renderMessage:()=>"",toolJSON:value=>JSON.stringify(value),
  scope:()=>state.scopes.find(item=>item.id===state.scope)||{},personaName:()=>"初音未来",toolRunCard:()=>"",
  field:(name,title,value)=>`<input name="${name}" value="${escaped(value)}">`,select:(name,title,options)=>`<select name="${name}">${options.map(([id,label])=>`<option value="${escaped(id)}">${escaped(label)}</option>`).join("")}</select>`,toggle:(name,title,hint,checked)=>`<input name="${name}" type="checkbox" ${checked?"checked":""}>`,textarea:(name,title,value)=>`<textarea name="${name}">${escaped(value)}</textarea>`,
  $:(selector)=>nodes.get(selector)||null,$$:()=>[],getForm:form=>form.values,modelPayload:form=>({...form.values}),
  api:async(path,options)=>{calls.push([path,options]);return responder(path,options);},toast:()=>{},
  openModal:(...args)=>{modal=args;},closeModal:()=>{modal=null;},confirmAction:async(...args)=>{modal=args;},
  bootstrap:async()=>{},navigate:async page=>{state.page=page;},renderPage:async()=>{},
  localStorage:{setItem:()=>{}},document:{getElementById:id=>nodes.get("#"+id)},
  renderPersonas:async()=>'<h1>人格</h1><button data-action="new-persona" >创造人格</button>',renderSettings:async()=>'<div class="settings-main"><form data-form="qq"></form></div>',
};
vm.createContext(sandbox);
for(const file of [...require('./ui_modules.cjs').experienceFactories,'experience.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static",file),"utf8"),sandbox,{filename:file});
const ui=sandbox.window.ShioriExperience;
const plain=value=>JSON.parse(JSON.stringify(value));

async function main(){
  const hostile=ui.markdown('# Guide\n<script>alert(1)</script>\n\n[x](javascript:alert)\n\n[ok](https://example.test/a)\n\n```html\n<img src=x onerror=alert(1)>\n```');
  assert.ok(!hostile.includes('<script>'));
  assert.ok(!hostile.includes('href="javascript:'));
  assert.ok(!hostile.includes('<img'));
  assert.match(hostile,/href="https:\/\/example.test\/a"/);
  assert.match(hostile,/rel="noopener noreferrer"/);
  assert.match(hostile,/&lt;script&gt;/);

  const corpus=ui.jobDetail({id:"j1",kind:"distillation",status:"completed",result:{mode:"corpus",personality:"<img onerror=evil>",artifact:null}});
  assert.match(corpus,/&lt;img onerror=evil&gt;/);
  assert.ok(!corpus.includes("下载 Skill 包"),"corpus must not promise a Skill artifact");
  assert.ok(!corpus.includes('data-action="job-approve"'));
  const character=ui.jobDetail({id:"j2",kind:"distillation",status:"completed",result:{mode:"character",personality:"personality",artifact:{id:"artifact"}}});
  assert.match(character,/\/api\/persona\/distill\/j2\/artifact/);
  const interrupted=ui.jobDetail({id:"j3",kind:"computer",status:"interrupted"});
  assert.match(interrupted,/已中断/);
  assert.ok(!interrupted.includes('data-action="job-cancel"'));
  assert.ok(!interrupted.includes('data-action="job-approve"'));
  assert.ok(!interrupted.includes('retry'));
  const events=ui.jobDetail({id:"j4",kind:"computer",status:"completed",events:[
    {type:"progress",data:{stage:"thinking",message:"正在处理第 1 步"}},
    {type:"tool_result",data:{tool:"read_file",result:{content:"<script>private file</script>",ok:true}}}
  ]});
  assert.match(events,/正在处理第 1 步/);
  assert.match(events,/read_file/);
  assert.match(events,/&lt;script&gt;private file&lt;\/script&gt;/);
  assert.ok(!events.includes("<script>"));

  assert.equal(ui.costLabel({cost:0}),"费用未知");
  assert.equal(ui.costLabel({cost:.001}),"¥ 0.0010");
  assert.equal(ui.costLabel({cost:0,cost_known:true}),"¥ 0.0000");
  state.settings.model={input_price:1};assert.equal(ui.costLabel({cost:0}),"¥ 0.0000");
  assert.equal(ui.costLabel({cost:0,cost_known:false}),"费用未知","historical unknown prices remain unknown after configuring a model price");state.settings.model={};

  const before=calls.length;
  await ui.handleAction("start-distill",{dataset:{}});
  assert.equal(calls.length,before,"opening distillation must not call research/model endpoints");
  assert.match(modal[2],/data-action="distill-character"/);
  await assert.rejects(ui.handleSubmit("distill-start",{values:{confirmed:false}}),/先确认/);
  await ui.handleAction("distill-character",{dataset:{}});
  assert.match(modal[2], /name="source_urls"/);
  nodes.set("#modal", {classList:{add(){}}});
  responder=async()=>({candidates:[],warnings:[],discovery_id:"discovery-fixture"});
  await ui.handleSubmit("distill-search",{values:{work:"作品",character:"角色",source_urls:"  https://wiki.example/character\r\nhttps://official.example/character\nhttps://wiki.example/character\n"}});
  assert.deepEqual(plain(calls.at(-1)[1].body),{work:"作品",character:"角色",source_urls:["https://wiki.example/character","https://official.example/character"]});
  const searched=calls.length;
  await assert.rejects(ui.handleSubmit("distill-search",{values:{work:"作品",character:"角色",source_urls:Array.from({length:7},(_,i)=>"https://example.test/"+i).join("\n")}}),/最多提供6个/);
  assert.equal(calls.length,searched);
  responder=async()=>({lookup_id:"lookup-fixture",query:"埃罗芒阿老师",characters:[{id:"bgm-22520",name:"和泉纱雾",works:["埃罗芒阿老师"],aliases:["和泉 紗霧"],description:"<script>bad</script>",url:"https://bgm.tv/character/22520"},{id:"bgm-22521",name:"和泉正宗",works:["埃罗芒阿老师"]}]});
  await ui.handleSubmit("distill-search",{values:{query:"埃罗芒阿老师"}});
  assert.equal(calls.at(-1)[0],"/api/persona/distill/character/lookup");
  assert.match(modal[2],/type="radio" name="character_id"/);
  assert.ok(!modal[2].includes('name="confirmed"'));
  assert.ok(!modal[2].includes('name="candidate_ids"'));
  assert.match(modal[2],/&lt;script&gt;/);
  const lookedUp=calls.length;
  await assert.rejects(ui.handleSubmit("distill-start",{values:{character_id:"forged"}}),/选择一个角色/);
  assert.equal(calls.length,lookedUp);
  await ui.handleSubmit("distill-start",{values:{character_id:"bgm-22520"}});
  assert.deepEqual(plain(calls.at(-1)),["/api/persona/distill/start",{method:"POST",body:{mode:"character",confirmed:true,name:"和泉纱雾",version:"",lookup_id:"lookup-fixture",character_id:"bgm-22520"}}]);
  await ui.handleSubmit("time-awareness",{values:{awareness_enabled:false,timezone:"Asia/Hong_Kong"}});
  assert.deepEqual(plain(calls.at(-1)),["/api/settings",{method:"PUT",body:{time:{awareness_enabled:false,timezone:"Asia/Hong_Kong"}}}]);

  responder=async()=>({id:"web:auto"});
  await ui.handleAction("new-chat",{dataset:{}});
  assert.equal(state.scope,"web:auto");
  const create=calls.at(-1);assert.equal(create[0],"/api/scopes");
  assert.deepEqual(plain(create[1].body),{name:"新对话",kind:"web",private_reply_policy:"always"});
  assert.ok(!("id" in create[1].body),"server creates internal chat IDs");

  const list={innerHTML:""};nodes.set("#model-list",list);
  const form={values:{base_url:"http://localhost:1234/v1",api_key:"new-unsaved-key",model:"unsaved-model"}};
  nodes.set('[name="model"]',{getAttribute:()=>"model-list"});
  responder=async()=>({models:[{id:'model"><script>evil</script>'}]});
  const listButton={dataset:{},closest:()=>form};
  await ui.handleAction("list-models",listButton);
  assert.equal(calls.at(-1)[0],"/api/models");assert.equal(calls.at(-1)[1].method,"POST");
  assert.equal(calls.at(-1)[1].body.api_key,"new-unsaved-key","discovery must use current form credentials");
  assert.match(list.innerHTML,/&lt;script&gt;/);
  assert.equal(listButton.disabled,false);

  responder=async()=>({id:"job",status:"awaiting_approval",approval:{id:"new",summary:"changed"}});
  await assert.rejects(ui.handleAction("job-approve",{dataset:{id:"job",approval:"old"}}),/审批已变化/);
  assert.equal(calls.at(-1)[0],"/api/jobs/job","stale approval must not call approve");
  responder=async()=>({id:"job",title:"Action",status:"awaiting_approval",approval:{id:"new",summary:"<b>action</b>",details:{command:"echo hi"}}});
  await ui.handleAction("job-approve",{dataset:{id:"job",approval:"new"}});
  assert.match(modal[2],/name="approval_id" value="new"/);
  assert.match(modal[2],/&lt;b&gt;action&lt;\/b&gt;/);

  const result={innerHTML:""},password={value:"test-secret"};nodes.set("#local-qq-result",result);nodes.set('[name="password"]',password);
  responder=async path=>path.endsWith("connect")?{requires_account:true,accounts:[{account_id:"123456",label:"A"},{account_id:"654321",label:"B"}]}:{};
  await ui.handleSubmit("local-qq",{values:{adapter:"snowluma",webui_url:"http://127.0.0.1:5099",password:"test-secret"}});
  assert.match(result.innerHTML,/selected_account/);
  assert.equal(password.value,"test-secret","multi-account selection retains credential until configuration completes");
  assert.ok(!result.innerHTML.includes("test-secret"));
  assert.ok(!calls.at(-1)[0].endsWith("status"),"account selection is not connection success");
  await fullScriptIntegration();
  console.log("PASS: frontend escaping, review state, admin/task actions, automatic sessions, model drafts, multi-account pairing");
}

async function fullScriptIntegration(){
  const elements=new Map(), listeners={}, storage=new Map();
  const element=selector=>{
    if(!elements.has(selector))elements.set(selector,{innerHTML:"",textContent:"",value:"",open:false,dataset:{},scrollHeight:1,
      classList:{add(){},remove(){},toggle(){}},close(){},setAttribute(){},addEventListener(){},focus(){},querySelector(){return null;},querySelectorAll(){return[];}});
    return elements.get(selector);
  };
  const settings={model:{},onebot:{},qq_official:{},memory:{},computer:{},identity:{name:"初音未来"}}, scopes=[{id:"web:default",name:"本地聊天"}];
  const browser={console,URL,URLSearchParams,AbortController,FormData,setTimeout,clearTimeout,EventSource:class{close(){}addEventListener(){}},
    location:{protocol:"http:",host:"127.0.0.1:18765",hash:"",pathname:"/",search:""},history:{replaceState(){}},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},sessionStorage:{getItem(){return"1";},setItem(){}},
    document:{body:{classList:{add(){},remove(){},toggle(){}}},querySelector:selector=>selector.includes("form")?null:element(selector),querySelectorAll(){return[];},addEventListener:(name,handler)=>{listeners[name]=handler;},getElementById:element},
    fetch:async url=>{assert.ok(url.startsWith("/"),"UI bootstrap must not send external requests");let data=[];
      if(url==="/api/bootstrap")data={settings,scopes,personas:[],version:"0.8.0",setup_required:false};
      else if(url==="/api/auth/status")data={authenticated:true,mode:"password",password_set:true,remote_enabled:false};
      else if(url==="/api/auth/access")data={remote_enabled:false,host:"0.0.0.0",port:8765};
      else if(url==="/api/settings")data=settings;
      else if(url==="/api/bot-profiles")data={default_id:'default',items:[{id:'default',name:'默认配置',values:{persona_id:'default'}}],scopes:[]};
      else if(url==="/api/qq/status"||url==="/api/providers/status")data={};
      else if(url==="/api/system")data={supported:false};
      else if(url.startsWith("/api/diagnostics"))data={checks:[],model:{configured:false},qq:{enabled:false}};
      return{ok:true,status:200,json:async()=>data,text:async()=>"# Local guide\nSafe local help"};},
    scrollTo(){},navigator:{},
  };
  browser.window=browser;vm.createContext(browser);
  for(const file of require('./ui_modules.cjs').integrationScripts){
    browser.document.currentScript={src:'http://127.0.0.1:18765/static/'+file+'?v=0.2.1-beta'};
    vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static",file),"utf8"),browser,{filename:file});
  }
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.match(element("#navigation").innerHTML,/运行概览/);
  assert.doesNotMatch(element("#content").innerHTML,/id="chat-mode"/);
  assert.match(element("#content").innerHTML,/开启电脑能力/);
  assert.match(element("#content").innerHTML,/本会话模型/);
  assert.ok(!element("#navigation").innerHTML.includes('data-page="models"'),'models are managed inside bot configuration');
  assert.match(element("#navigation").innerHTML,/机器人配置/);
  assert.match(element("#navigation").innerHTML,/运行日志/);
  assert.equal(element("#port-warning").hidden,true);
  vm.runInContext('state.boot.listener={port_changed:true,requested_port:8765,actual_port:8766,port_warning:"请求8765，实际8766。OneBot <script>bad</script>"}; renderNavigation();',browser);
  assert.equal(element("#port-warning").hidden,false);
  assert.match(element("#port-warning").textContent,/请求8765，实际8766/);
  assert.ok(!element("#port-warning").innerHTML.includes("<script>"),"startup warnings are text, not executable HTML");
  vm.runInContext('state.boot.listener={port_changed:false,port_warning:""}; renderNavigation();',browser);
  assert.equal(element("#port-warning").hidden,true);
  assert.equal(element("#port-warning").textContent,"");
  const personas=await vm.runInContext("window.ShioriExperience.renderPersonas()",browser);
  assert.match(personas,/蒸馏人格/);
  const settingsHTML=await vm.runInContext("window.ShioriExperience.renderSettings()",browser);
  assert.match(settingsHTML,/管理员账号/);
  assert.match(settingsHTML,/电脑任务/);
  assert.match(settingsHTML,/自动配对本机 QQ/);
  assert.match(settingsHTML,/允许私聊的 QQ 号/);
  assert.match(settingsHTML,/本机账户权限/);
  assert.match(settingsHTML,/不是沙箱/);
  assert.match(settingsHTML,/登录与远程管理/);
  assert.match(settingsHTML,/security\/webui-password.json/);
  const help=await vm.runInContext("window.ShioriExperience.renderHelp()",browser);
  assert.match(help,/Safe local help/);
  const localMessage=vm.runInContext('renderMessageBase({role:"user",user_id:"owner",scope_id:"web:default",content:"hello"})',browser);
  assert.match(localMessage,/shiori/);assert.ok(!localMessage.includes(">owner<"));
  const qqMessage=vm.runInContext('renderMessageBase({role:"user",user_id:"owner",scope_id:"group:123",metadata:{nickname:"QQ member"},content:"hello"})',browser);
  assert.match(qqMessage,/QQ member/);
  assert.ok(listeners.submit && listeners.click && listeners.change);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
