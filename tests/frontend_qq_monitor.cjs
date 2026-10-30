"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const root = path.join(__dirname,"../shiori/static"), tick = () => new Promise(resolve => setImmediate(resolve));
const escape = value => String(value ?? "").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const scopeA="qq:group:123", scopeB="qqofficial:private:abc", scopeC="qqofficial:group:xyz";
const msg = (id,scope=scopeA,role="user",metadata={}) => ({id,scope_id:scope,user_id:"qq-user",role,content:id,created_at:"2026-10-30T09:00:00Z",metadata});
const chat = (id,name,kind="group",last=null,total=0) => ({id,name,kind,last_message:last,last_at:last?.created_at || null,message_count:total,settings:{}});
let sandbox, ui, calls, timers, nodes, listeners, chats, histories, deliveries, override, navigations, configs;
const node = (html="") => ({innerHTML:html,value:"",scrollTop:0,scrollHeight:800,clientHeight:300,querySelectorAll:()=>[],setAttribute(){},classList:{toggle(){}}});
function setup(){
  calls=[];timers=new Map();nodes=new Map();listeners=new Map();navigations=[];configs=[];override=null;
  chats=[chat(scopeA,'<img src=x onerror="evil()">',"group",msg("<script>preview</script>"),11),chat(scopeB,"官方私聊","private"),chat(scopeC,"官方测试群")];
  histories=new Map([[scopeA,{items:[msg("m2"),msg("m3",scopeA,"assistant",{output_part:{index:1,total:3}})],has_more:true,next_before:"m2"}],[scopeB,{items:[msg("official",scopeB)],has_more:false,next_before:"official"}],[scopeC,{items:[],has_more:false,next_before:null}]]);
  deliveries=[{scope_id:scopeA,assistant_message_id:"m3",parts:[{text:"<img onerror=evil()>",status:"confirmed"},{text:"unknown",status:"unknown"},{text:"never sent",status:"pending"},{text:"failed",status:"failed"}]}];
  let timerId=0;
  sandbox={window:{},state:{page:"qqChats",authenticated:true,scope:"web:default",scopes:chats,personas:[],messages:[]},AbortController,URLSearchParams,Map,Set,Date,
    document:{hidden:false,addEventListener:(name,callback)=>listeners.set(name,callback)},
    setTimeout:(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id),
    $:selector=>nodes.get(selector)||null,$$:()=>[],e:escape,icon:()=>"",fmt:value=>String(value),personaName:()=>"角色",scope:()=>({persona_id:"p"}),imageURL:()=>"",
    query:params=>new URLSearchParams(Object.entries(params).filter(([,v])=>v!==undefined&&v!==null&&v!=="")).toString(),
    api:async(url,options={})=>{
      calls.push({url,options});
      if(override){const intercepted=override(url,options);if(intercepted!==undefined)return intercepted;}
      const parsed=new URL(url,"http://localhost");
      if(parsed.pathname==="/api/qq/chats")return {items:structuredClone(chats),total:chats.length};
      if(parsed.pathname==="/api/delivery")return structuredClone(deliveries.filter(row=>row.scope_id===parsed.searchParams.get("scope_id")));
      const match=parsed.pathname.match(/^\/api\/qq\/chats\/([^/]+)\/messages$/);assert.ok(match,`unexpected API ${url}`);
      const id=decodeURIComponent(match[1]);
      if(parsed.searchParams.has("after"))return {items:[],has_more:false,next_before:null};
      if(parsed.searchParams.has("before"))return {items:[msg("m1",id)],has_more:false,next_before:"m1"};
      return structuredClone(histories.get(id)||{items:[],has_more:false});
    },navigate:async page=>{navigations.push(page);sandbox.state.page=page;},scopeModal:item=>configs.push(item)};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(root,"ui-core.js"),"utf8"),sandbox);
  vm.runInContext(fs.readFileSync(path.join(root,"chat-page.js"),"utf8"),sandbox);
  vm.runInContext(fs.readFileSync(path.join(root,"qq-monitor.js"),"utf8"),sandbox);ui=sandbox.window.ShioriQQMonitor;
}
function mount(html){for(const selector of ["#qq-monitor","#qq-monitor-list","#qq-monitor-header","#qq-monitor-status","#qq-monitor-feed","#global-scope"])nodes.set(selector,node());nodes.get("#qq-monitor").innerHTML=html;ui.afterRender();}
function html(selector="#qq-monitor-feed"){return nodes.get(selector)?.innerHTML||"";}
const action=(name,data={})=>ui.handleAction("qq-monitor-"+name,{dataset:data});
function timer(){const value=[...timers.values()].find(row=>row.delay===5000);assert.ok(value,"monitor poll scheduled");return value.callback;}
async function run(){
  setup();let initial=await ui.render();mount(initial);
  assert.equal(sandbox.state.scope,scopeA);assert.ok(calls.every(call=>(call.options.method||"GET")==="GET"));
  assert.match(initial,/&lt;img src=x onerror=&quot;evil\(\)&quot;&gt;/);assert.match(initial,/&lt;script&gt;preview&lt;\/script&gt;/);
  assert.match(initial,/官方私聊/);assert.match(initial,/官方测试群/);assert.match(initial,/11 条/);assert.match(initial,/暂无消息时间/);assert.match(initial,/暂无消息/);
  assert.match(initial,/只读监控/);assert.doesNotMatch(initial,/data-form="chat"|chat-composer|delivery-resume|clear-messages|type="submit"/);
  assert.match(initial,/已确认送达/);assert.match(initial,/送达未知/);assert.match(initial,/尚未发送/);assert.match(initial,/发送失败/);assert.match(initial,/分段 1 \/ 3/);
  assert.match(initial,/&lt;img onerror=evil\(\)&gt;/);assert.equal(sandbox.state.messages.length,2);timer();
  assert.match(initial,/aria-label="QQ 发送者：qq-user">群友<\/div>/);assert.doesNotMatch(initial,/<div class="message-avatar">你<\/div>/);

  // Search and type filters patch only the list; typed search survives polling.
  const callCount=calls.length;
  assert.equal(ui.handleInput({id:"qq-monitor-search",value:"官方"}),true);
  assert.match(html("#qq-monitor-list"),/官方私聊/);assert.doesNotMatch(html("#qq-monitor-list"),/&lt;script&gt;preview/);
  await action("kind",{kind:"private"});assert.match(html("#qq-monitor-list"),/官方私聊/);assert.doesNotMatch(html("#qq-monitor-list"),/官方测试群/);assert.equal(calls.length,callCount);
  ui.handleInput({id:"qq-monitor-search",value:"absent"});assert.match(html("#qq-monitor-list"),/没有匹配/);
  ui.handleInput({id:"qq-monitor-search",value:""});await action("kind",{kind:"all"});

  // Earlier pages prepend, do not duplicate, and preserve the reading position.
  const feed=nodes.get("#qq-monitor-feed");feed.scrollTop=130;
  await action("older");assert.ok(calls.some(call=>call.url.includes("before=m2")));assert.deepEqual(Array.from(sandbox.state.messages,message=>message.id),["m1","m2","m3"]);
  assert.match(html(),/已到最早记录/);assert.equal(feed.scrollTop,130);

  // History browsing keeps its reading window while refreshing the list and
  // receipts; returning to latest resumes ordered incremental catch-up.
  const historyCalls=calls.filter(call=>call.url.includes("/messages?")).length;
  await action("refresh");assert.equal(calls.filter(call=>call.url.includes("/messages?")).length,historyCalls);assert.match(html(),/正在浏览历史/);
  await action("latest");assert.doesNotMatch(html(),/正在浏览历史/);
  override=url=>{const p=new URL(url,"http://localhost");const after=p.searchParams.get("after");
    if(after==="m3")return {items:[msg("m4")],has_more:true,next_after:"m4"};
    if(after==="m4")return {items:[msg("m4"),msg("m5")],has_more:false,next_after:"m5"};};
  await action("refresh");assert.deepEqual(Array.from(sandbox.state.messages,message=>message.id),["m2","m3","m4","m5"]);assert.match(html(),/m5/);
  assert.ok(calls.some(call=>call.url.includes("after=m4")));override=null;

  // An official private conversation has its own feed and no fabricated receipts.
  await action("select",{scope:scopeB});assert.equal(sandbox.state.scope,scopeB);assert.match(html(),/official/);assert.doesNotMatch(html(),/m3|已确认送达/);
  assert.match(html(),/aria-label="QQ 发送者：qq-user">用户<\/div>/);
  await action("settings",{scope:scopeB});assert.equal(configs.at(-1).id,scopeB);
  await action("select",{scope:"web:default"});assert.equal(sandbox.state.scope,scopeB);
  await action("logs");assert.deepEqual(navigations,["logs"]);ui.afterRender();assert.equal(timers.size,0);

  // Missing receipts stay explicitly unconfirmed; a blocked/failed metadata
  // status is shown exactly, never inferred from the presence of content.
  setup();deliveries=[];histories.set(scopeA,{items:[msg("plain",scopeA,"assistant"),msg("blocked",scopeA,"assistant",{delivery_blocked:true}),msg("bad",scopeA,"assistant",{delivery_status:"failed"})],has_more:false});
  initial=await ui.render();mount(initial);assert.match(initial,/无送达回执/);assert.match(initial,/发送已阻止/);assert.match(initial,/发送失败/);assert.doesNotMatch(initial,/已确认送达/);

  // QQ senders are identified independently from the local administrator.
  // Altering the avatar must not rewrite “你” in escaped message content.
  setup();histories.set(scopeA,{items:[{...msg("nick",scopeA,"user",{nickname:'演示成员"<script>'}),content:'你说：<div class="message-avatar">你</div>'}],has_more:false});
  initial=await ui.render();mount(initial);
  assert.match(initial,/aria-label="QQ 发送者：演示成员&quot;&lt;script&gt;">演<\/div>/);
  assert.match(initial,/你说：&lt;div class=&quot;message-avatar&quot;&gt;你&lt;\/div&gt;/);assert.doesNotMatch(initial,/<script>/);

  // List pagination reaches older scopes, without trusting a local Web row.
  setup();override=url=>{const p=new URL(url,"http://localhost");if(p.pathname==="/api/qq/chats")return p.searchParams.has("offset")?{items:[chats[1],chat("web:default","不属于QQ")],has_more:false}:{items:[chats[0]],has_more:true};};
  initial=await ui.render();mount(initial);assert.ok(calls.some(call=>call.url.includes("offset=1")));assert.match(initial,/官方私聊/);assert.doesNotMatch(initial,/不属于QQ/);override=null;

  // Late poll responses cannot replace the next selected scope or repopulate
  // shared source-dialog messages with the former conversation.
  let release;
  override=url=>url.includes("after=m3")?new Promise(resolve=>{release=resolve;}):undefined;
  const old=action("refresh");await tick();await action("select",{scope:scopeB});
  const beforeResolve=html();release({items:[msg("STALE_A")],has_more:false});await old;
  assert.equal(html(),beforeResolve);assert.equal(sandbox.state.scope,scopeB);assert.deepEqual(Array.from(sandbox.state.messages,message=>message.id),["official"]);assert.ok(calls.find(call=>call.url.includes("after=m3")).options.signal.aborted);

  // Manual pause stops the timer and invalidates in-flight work. Resume reads
  // new data immediately; page visibility independently stops all background I/O.
  override=null;await ui.handleChange({id:"qq-monitor-auto",checked:false});assert.equal(timers.size,0);assert.match(html("#qq-monitor-status"),/自动刷新已暂停/);
  const pausedCalls=calls.length;listeners.get("visibilitychange")();await tick();assert.equal(calls.length,pausedCalls);
  await ui.handleChange({id:"qq-monitor-auto",checked:true});assert.ok(calls.length>pausedCalls);timer();
  sandbox.document.hidden=true;listeners.get("visibilitychange")();assert.equal(timers.size,0);assert.match(html("#qq-monitor-status"),/页面隐藏/);
  const hiddenCalls=calls.length;await action("refresh");assert.equal(calls.length,hiddenCalls);
  sandbox.document.hidden=false;listeners.get("visibilitychange")();await tick();assert.ok(calls.length>hiddenCalls);timer();

  // Hiding during the initial await must still produce a page shell, so the
  // visibility-resume request has a mounted target and cannot strand a blank page.
  setup();let releaseInitial;
  override=url=>url.startsWith("/api/qq/chats?")?new Promise(resolve=>{releaseInitial=resolve;}):undefined;
  const opening=ui.render();await tick();sandbox.document.hidden=true;listeners.get("visibilitychange")();
  releaseInitial({items:chats});initial=await opening;assert.match(initial,/id="qq-monitor"/);mount(initial);assert.equal(timers.size,0);
  override=null;sandbox.document.hidden=false;listeners.get("visibilitychange")();await tick();assert.match(html(),/m3/);timer();

  // Leaving or signing out makes queued callbacks harmless, even if the shell
  // closes before a network implementation acknowledges AbortController.
  const queued=timer();ui.close();assert.equal(timers.size,0);assert.equal(sandbox.state.messages.length,0);const closedCalls=calls.length;await queued();assert.equal(calls.length,closedCalls);
  setup();mount(await ui.render());const authTimer=timer();sandbox.state.authenticated=false;await authTimer();assert.equal(calls.length,3);ui.afterRender();assert.equal(timers.size,0);

  // Partial endpoint failures keep successful messages, surface errors, and
  // can be retried with the page still usable.
  setup();override=url=>url.startsWith("/api/delivery")?Promise.reject(new Error("<script>receipt failure</script>")):undefined;
  initial=await ui.render();mount(initial);assert.match(initial,/m3/);assert.match(initial,/分段回执读取失败/);assert.match(initial,/&lt;script&gt;receipt failure/);assert.doesNotMatch(initial,/已确认送达/);
  override=url=>url.includes("/messages?")?Promise.reject(new Error("network failure")):undefined;
  await action("refresh");assert.match(html(),/network failure/);assert.match(html(),/m3/);assert.match(html("#qq-monitor-status"),/刷新失败/);
  override=null;await action("refresh");assert.doesNotMatch(html(),/network failure/);

  // Preview projections use an image count, without downloading original
  // base64 attachments. A clear in another tab invalidates the old cursor and
  // replaces stale history with the server's new latest page.
  setup();chats[0].last_message={...msg("image-preview"),content:"",metadata:{image_count:1}};
  initial=await ui.render();mount(initial);assert.match(initial,/\[图片\]/);
  override=url=>url.includes("after=m3")?Promise.reject(new Error("消息游标不存在于当前会话")):undefined;
  histories.set(scopeA,{items:[msg("after-clear")],has_more:false,next_before:"after-clear"});
  await action("refresh");assert.deepEqual(Array.from(sandbox.state.messages,message=>message.id),["after-clear"]);assert.doesNotMatch(html(),/m3|游标不存在/);
  override=null;await action("refresh");assert.ok(calls.some(call=>call.url.includes("after=after-clear")));

  // A missing before cursor follows the same recovery path, without merging
  // a fresh conversation into already deleted old messages.
  setup();mount(await ui.render());override=url=>url.includes("before=m2")?Promise.reject(new Error("消息游标不存在于当前会话")):undefined;
  histories.set(scopeA,{items:[],has_more:false,next_before:null});await action("older");assert.equal(sandbox.state.messages.length,0);assert.match(html(),/这个会话还没有消息/);

  // A live monitor remains bounded over long runs. Historical paging trims the
  // newer edge, and background refresh cannot displace the historical window.
  setup();mount(await ui.render());let nextId=10;
  override=url=>{const p=new URL(url,"http://localhost");if(p.searchParams.has("after"))return {items:Array.from({length:80},()=>msg("live-"+nextId++)),has_more:false};};
  for(let index=0;index<9;index++)await action("refresh");
  assert.equal(sandbox.state.messages.length,500);assert.equal((html().match(/data-qq-message=/g)||[]).length,500);
  const oldest=sandbox.state.messages[0].id;
  override=url=>{const p=new URL(url,"http://localhost");if(p.searchParams.has("before")){assert.equal(p.searchParams.get("before"),oldest);return {items:Array.from({length:80},(_,index)=>msg("older-"+index)),has_more:true,next_before:"older-0"};}};
  await action("older");assert.equal(sandbox.state.messages.length,500);assert.equal(sandbox.state.messages[0].id,"older-0");assert.match(html(),/回到最新/);
  const parked=Array.from(sandbox.state.messages,message=>message.id),oldMessageCalls=calls.filter(call=>call.url.includes("/messages?")).length;
  await action("refresh");assert.deepEqual(Array.from(sandbox.state.messages,message=>message.id),parked);assert.equal(calls.filter(call=>call.url.includes("/messages?")).length,oldMessageCalls);
  override=null;await action("latest");assert.deepEqual(Array.from(sandbox.state.messages,message=>message.id),["m2","m3"]);

  // Configuration uses the refreshed authoritative QQ snapshot instead of a
  // bootstrap record which may have been superseded in another tab.
  sandbox.state.scopes=sandbox.state.scopes.map(row=>({...row,name:"旧名称",enabled:true}));
  chats[0]={...chats[0],name:"已更新的名称",enabled:false,persona_id:"new-persona"};
  await action("refresh");await action("settings",{scope:scopeA});assert.equal(configs.at(-1).name,"已更新的名称");assert.equal(configs.at(-1).enabled,false);
  assert.equal(sandbox.state.scopes.find(row=>row.id===scopeA).persona_id,"new-persona");

  // Closing an already inactive monitor never clears local Chat's own array.
  sandbox.state.page="chat";sandbox.state.messages=[msg("local", "web:default")];ui.close();assert.equal(sandbox.state.messages[0].id,"local");

  // An already dispatched QQ fetch cannot repopulate history after the real
  // shell has changed page/scope and mounted local Chat messages.
  setup();mount(await ui.render());let releaseLeaving;
  override=url=>url.includes("after=m3")?new Promise(resolve=>{releaseLeaving=resolve;}):undefined;
  const leaving=action("refresh");await tick();sandbox.state.page="chat";sandbox.state.scope="web:default";ui.close();
  sandbox.state.messages=[msg("local-new", "web:default")];releaseLeaving({items:[msg("late-qq")],has_more:false});await leaving;
  assert.equal(sandbox.state.scope,"web:default");assert.equal(sandbox.state.messages[0].id,"local-new");

  // An empty server response does not synthesize a QQ scope or chat history.
  setup();chats=[];initial=await ui.render();mount(initial);assert.match(initial,/尚无 QQ 会话/);assert.doesNotMatch(initial,/data-action="qq-monitor-settings"/);assert.equal(calls.length,1);
  assert.equal(await ui.handleAction("chat-send",{dataset:{}}),false);
  assert.ok(calls.every(call=>(call.options.method||"GET")==="GET"));
  // Current messages poll independently; a frequent tick does not recalculate
  // every conversation's count. A slow manual list refresh cannot block them.
  setup();mount(await ui.render());const lists=calls.filter(call=>call.url.startsWith('/api/qq/chats?')).length;
  await timer()();assert.equal(calls.filter(call=>call.url.startsWith('/api/qq/chats?')).length,lists);
  let releaseList;override=url=>url.startsWith('/api/qq/chats?')?new Promise(resolve=>{releaseList=resolve;}):url.includes('after=m3')?{items:[msg('arrived-during-list')],has_more:false}:undefined;
  await action('refresh');assert.match(html(),/arrived-during-list/,'current messages are painted before the sidebar returns');
  releaseList({items:chats,has_more:false});await tick();ui.close();
  setup();mount(await ui.render());override=url=>url.startsWith('/api/qq/chats?')?new Promise(resolve=>{releaseList=resolve;}):undefined;
  await action('refresh');releaseList({items:[],has_more:false});await tick();
  assert.doesNotMatch(html(),/m3|m2/,'a removed selection cannot leave its messages under another header');ui.close();
  console.log("QQ monitor: filtering, paging, read-only delivery, request races, visibility, and lifecycle regressions passed");
}
run().catch(error=>{console.error(error);process.exitCode=1;});
