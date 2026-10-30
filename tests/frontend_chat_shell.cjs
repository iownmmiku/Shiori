"use strict";
// Real shell navigation plus the real draft factory. Only transport and DOM
// mounting are substituted, so leaving Chat exercises the production handoff.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const root=path.join(__dirname,"../shiori/static"),source=name=>fs.readFileSync(path.join(root,name),"utf8"),app=source("app.js");
const tick=()=>new Promise(resolve=>setImmediate(resolve)),plain=value=>JSON.parse(JSON.stringify(value));
const part=(start,end,text=app)=>{const a=text.indexOf(start),b=text.indexOf(end,a);assert.ok(a>=0&&b>a,`${start} exists`);return text.slice(a,b);};
function fixture({pathname="/chat",page="chat",scope="web:a"}={}){
  const nodes=new Map(),storage=new Map(),calls=[],lifecycle=[],timers=new Map();let serial=0,gate=null;
  const element=()=>({value:"",innerHTML:"",textContent:"",scrollTop:0,scrollHeight:200,focus(){},classList:{remove(){},toggle(){}}});
  const main=element();nodes.set("#content",main);nodes.set("#chat-text",element());
  let content="";Object.defineProperty(main,"innerHTML",{get:()=>content,set:value=>{content=value;nodes.delete("#chat-text");if(value.includes("CHAT_MOUNT")){const input=element();input.value=state.draft;nodes.set("#chat-text",input);}}});
  const scopes=[{id:"web:a",name:"本地A"},{id:"web:b",name:"本地B"},{id:"qq:group:42",name:"QQ群"},{id:"qqofficial:private:user",name:"QQ私聊"}];
  const state={page,scope,scopes,settings:{},boot:{},personas:[],draft:"",images:[],messages:[],filters:{},renderId:0,authenticated:true,sending:false};
  const location={pathname,search:"?keep=1"},runtime={attachments:[],attachmentScope:scope};
  const noop=()=>{};
  const sandbox={state,console,AbortController,URLSearchParams,Date,Map,Set,
    document:{body:{classList:{toggle(){}}}},
    $:selector=>{if(selector==="#chat-text")return nodes.get(selector)||null;if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);},$$:()=>[],
    e:String,icon:()=>"",configuredModel:()=>({}),heading:()=>"",button:()=>"",toast:noop,updateAdapterFields:noop,renderImagePreviews:noop,
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,String(value))},sessionStorage:{getItem:()=>null},
    setTimeout:callback=>{timers.set(++serial,callback);return serial;},clearTimeout:id=>timers.delete(id),
    query:value=>new URLSearchParams(value).toString(),rows:value=>value.items||[],scope:()=>({}),renderMessage:message=>message.content,
    api:async(url,options={})=>{calls.push({url,options});return {settings:state.settings,scopes,personas:[]};},
    window:{location,history:{replaceState(_value,_title,url){const next=new URL(url,"http://localhost");location.pathname=next.pathname;location.search=next.search;}},scrollTo:noop,
      ShioriAssets:{ensure:async page=>{lifecycle.push("ensure:"+page);if(gate)return gate;}},
      ShioriBackup:{close:()=>lifecycle.push("backup:close")},
      ShioriQQMonitor:{close:()=>lifecycle.push("qq:close"),render:async()=>{lifecycle.push("qq:render");return "QQ_MOUNT";},afterRender:()=>lifecycle.push("qq:after")},
      ShioriPages:{renderModels:async()=>"MODELS",renderLogs:async()=>"LOGS",afterRender:noop,close:()=>lifecycle.push("pages:close")},ShioriFeatures:{afterRender:noop},ShioriAuth:{requireLogin:()=>lifecycle.push("auth")},
      ShioriUpdates:{close:()=>lifecycle.push("updates:close"),afterRender:noop}}};
  for(const name of ["renderMemory","renderCulture","renderKnowledge","renderPersonaKnowledge","renderTasks","renderTools","renderGroups","renderDiagnostics"])sandbox[name]=async()=>name;
  vm.createContext(sandbox);vm.runInContext(source("experience-chat.js"),sandbox);
  const chat=sandbox.window.ShioriExperienceChat.create(runtime,{inactive:()=>true,jobDetail:()=>""});
  sandbox.window.ShioriExperience={saveChatDraft:()=>{lifecycle.push("chat:save");chat.saveDraft();},parkChatDraft:()=>{lifecycle.push("chat:park");chat.parkDraft();},activateChatScope:id=>{lifecycle.push("chat:activate:"+id);return chat.activateScope(id);},
    renderChat:async()=>"CHAT_MOUNT",renderOverview:async()=>"OVERVIEW",renderPersonas:async()=>"PERSONAS",renderSettings:async()=>"SETTINGS",renderJobs:async()=>"JOBS",renderHelp:async()=>"HELP",afterRender:noop,close:()=>{lifecycle.push("chat:close");chat.close();}};
  vm.runInContext(part("const pageNames =","const state ="),sandbox);
  vm.runInContext(part('if (window.location.pathname','const labels ='),sandbox);
  vm.runInContext(part("function renderNavigation()","function imageURL("),sandbox);
  vm.runInContext(part("function showAuth()","// 0.2:"),sandbox);
  vm.runInContext(part("  async function refreshMessages()","  async function showJob(",source("experience.js")),sandbox);
  return {sandbox,state,chat,runtime,nodes,storage,calls,lifecycle,timers,main,location,setGate:value=>{gate=value;}};
}
async function run(){
  // A draft with both image and reference survives Chat -> QQ -> Chat.
  let f=fixture();f.nodes.get("#chat-text").value="本地未发草稿";f.state.images=[{name:"picture",data:"data:image/png;base64,AA"}];f.runtime.attachments=[{id:"attachment-a",name:"资料"}];
  await f.sandbox.navigate("qqChats");assert.equal(f.state.page,"qqChats");assert.equal(f.state.scope,"qq:group:42");assert.equal(f.storage.get("shiori-local-scope"),"web:a");assert.equal(f.state.draft,"");
  assert.ok(f.lifecycle.indexOf("chat:park")<f.lifecycle.indexOf("qq:render"));assert.match(f.main.innerHTML,/QQ_MOUNT/);assert.equal(f.location.pathname,"/");
  f.state.scope="qqofficial:private:user";
  await f.sandbox.navigate("chat");assert.equal(f.state.scope,"web:a");assert.equal(f.state.draft,"本地未发草稿");assert.equal(f.nodes.get("#chat-text").value,"本地未发草稿");assert.equal(f.state.images[0].name,"picture");assert.equal(f.runtime.attachments[0].id,"attachment-a");
  assert.equal(f.storage.get("shiori-qq-scope"),"qqofficial:private:user");assert.equal(f.location.pathname,"/chat");assert.equal(f.location.search,"?keep=1");
  await f.sandbox.navigate("overview");await f.sandbox.navigate("qqChats");assert.equal(f.state.scope,"qqofficial:private:user");await f.sandbox.navigate("chat");assert.equal(f.state.draft,"本地未发草稿");

  // A not-yet-dispatched debounce batch is parked, never sent by a timer after
  // the user has moved to the read-only QQ monitor.
  f=fixture();f.state.settings={debounce:{enabled:true,seconds:3,max_wait_seconds:30}};f.nodes.get("#chat-text").value="收集中留言";
  await f.chat.sendChat();assert.equal(f.state.debouncePending,true);assert.equal(f.timers.size,1);
  await f.sandbox.navigate("qqChats");assert.equal(f.state.debouncePending,false);assert.equal(f.timers.size,0);assert.ok(!f.calls.some(call=>call.url==="/api/chat"));
  await f.sandbox.navigate("chat");assert.match(f.state.draft,/收集中留言/);

  // /chat is an explicit local route even when storage last selected QQ.
  f=fixture({pathname:"/chat/",page:"qqChats",scope:"qq:group:42"});assert.equal(f.state.page,"chat");
  f.storage.set("shiori-local-scope","web:b");await f.sandbox.bootstrap();assert.equal(f.state.scope,"web:b");assert.match(f.main.innerHTML,/CHAT_MOUNT/);

  // Lazy QQ completion after another navigation never mounts or activates its
  // controller. The current page has already closed the previous monitor.
  f=fixture();let release;f.setGate(new Promise(resolve=>{release=resolve;}));
  const pending=f.sandbox.navigate("qqChats");await tick();f.setGate(null);await f.sandbox.navigate("overview");release();await pending;
  assert.doesNotMatch(f.main.innerHTML,/QQ_MOUNT/);assert.match(f.main.innerHTML,/OVERVIEW/);assert.equal(f.lifecycle.filter(item=>item==="qq:render").length,0);assert.ok(f.lifecycle.filter(item=>item==="qq:close").length>=2);
  const beforeAuth=f.lifecycle.length;f.sandbox.showAuth();assert.equal(f.state.authenticated,false);assert.equal(f.main.innerHTML,"");
  for(const step of ["backup:close","qq:close","pages:close","chat:close","updates:close","auth"])assert.ok(f.lifecycle.slice(beforeAuth).includes(step));

  // Both an already pending bootstrap and a late caller's ordinary refresh are
  // invalid after logout. Only the explicit Auth-ready entry may authenticate.
  for(const relogin of [false,true]){
    f=fixture();let finish,requests=0;const scopes=plain(f.state.scopes);
    f.sandbox.api=async()=>{requests++;return new Promise(resolve=>{finish=resolve;});};
    const old=f.sandbox.bootstrap();f.sandbox.showAuth();const generation=f.state.authGeneration;
    await f.sandbox.bootstrap();assert.equal(requests,1,"old finally callbacks cannot start a bootstrap after logout");
    if(relogin){f.sandbox.api=async()=>({settings:{newLogin:true},scopes,personas:[]});await f.sandbox.bootstrap(false,true);assert.equal(f.state.authenticated,true);}
    finish({settings:{staleLogin:true},scopes:[{id:"web:old-account",name:"旧数据"}],personas:[]});await old;
    assert.equal(f.state.authGeneration,generation);assert.equal(f.state.authenticated,relogin);assert.ok(!f.state.settings.staleLogin);assert.ok(!f.state.scopes.some(item=>item.id==="web:old-account"));
    if(relogin)assert.match(f.main.innerHTML,/CHAT_MOUNT/);else assert.equal(f.main.innerHTML,"");
  }
  f=fixture();let finishMount;f.setGate(new Promise(resolve=>{finishMount=resolve;}));const mounting=f.sandbox.navigate("qqChats");await tick();f.sandbox.showAuth();finishMount();await mounting;
  assert.equal(f.main.innerHTML,"");assert.equal(f.state.authenticated,false);assert.ok(!f.lifecycle.includes("qq:render"));assert.ok(!f.lifecycle.includes("qq:after"));

  f=fixture();f.nodes.get("#chat-text").value="前次登录的草稿";f.runtime.attachments=[{id:"private-attachment"}];f.state.images=[{data:"private-image"}];
  f.chat.activateScope("web:b");f.sandbox.window.ShioriAppLifecycle.leaveAuthenticated();f.state.authenticated=true;f.chat.activateScope("web:a");
  assert.equal(f.state.draft,"");assert.equal(f.state.images.length,0);assert.equal(f.runtime.attachments.length,0,"auth close clears parked drafts while normal navigation tests above retain them");

  // A response from an old authenticated generation cannot revoke a newer
  // login with a late 401 or expose JSON whose body finishes after logout.
  for(const phase of ["headers","json"]){
    f=fixture();let finish;f.sandbox.FormData=class{};
    f.sandbox.fetch=phase==="headers"?()=>new Promise(resolve=>{finish=resolve;}):async()=>({ok:true,status:200,json:()=>new Promise(resolve=>{finish=resolve;})});
    vm.runInContext(part("async function api(","let fieldId ="),f.sandbox);
    const receipt=f.sandbox.api("/api/messages"),rejected=assert.rejects(receipt,error=>error.name==="AbortError");await tick();
    f.sandbox.window.ShioriAppLifecycle.leaveAuthenticated();f.state.authenticated=true;const authEvents=f.lifecycle.filter(item=>item==="auth").length;
    finish(phase==="headers"?{ok:false,status:401,json:async()=>({detail:"old session expired"})}:{items:["old account data"]});await rejected;
    assert.equal(f.state.authenticated,true);assert.equal(f.lifecycle.filter(item=>item==="auth").length,authEvents);
  }

  // A late local task refresh must not replace the QQ source-dialog history,
  // or a newer render of the same local conversation.
  for(const change of ["page","scope","render"]){
    f=fixture();let resolve;f.sandbox.api=()=>new Promise(done=>{resolve=done;});const refresh=f.sandbox.refreshMessages();
    if(change==="page")f.state.page="qqChats";if(change==="scope")f.state.scope="web:b";if(change==="render")f.state.renderId++;
    f.state.messages=[{id:"current",content:"current"}];resolve({items:[{id:"stale",content:"stale"}]});await refresh;assert.deepEqual(plain(f.state.messages),[{id:"current",content:"current"}]);
  }
  // Reading a local picture may outlive navigation. Never append it to the
  // newly selected QQ/local conversation while the FileReader is pending.
  for(const change of ["page","scope","unchanged"]){
    f=fixture();let reader;
    f.sandbox.FileReader=class{constructor(){reader=this;}readAsDataURL(){}};
    vm.runInContext('async function readImage(event){'+part('  if (event.target.id === "image-input") {','  if (event.target.id === "note-input"')+'}',f.sandbox);
    const target={id:"image-input",value:"file",files:[{name:"demo.png",type:"image/png",size:20}]};
    const reading=f.sandbox.readImage({target});
    if(change==="page")f.state.page="qqChats";
    if(change==="scope")f.state.scope="web:b";
    reader.result="data:image/png;base64,AA";reader.onload();await reading;
    assert.equal(f.state.images.length,change==="unchanged"?1:0);assert.equal(target.value,"");
  }
  console.log("Chat shell: parked drafts and debounce, QQ/local scope restoration, /chat route, lazy lifecycle, and stale task refresh passed");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
