"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const source=name=>fs.readFileSync(path.join(__dirname,"../shiori/static",name),"utf8"),plain=value=>JSON.parse(JSON.stringify(value));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
function fixture(){
  const calls=[],notices=[],nodes=new Map();
  const record={id:"event-one",kind:"event",scope_id:"web:a",user_id:"owner",visibility:"private",status:"active",version:2,importance:.7,created_at:"2026-11-03T10:00:00Z",event_time_known:false,event_time_start:null,source_ids:["source-one","source-missing"],content:"用于索引的完整正文",
    episode:{title:"一起完成 <计划>",summary:"先讨论，然后做出决定。",narrative:"完整经过的唯一标识\n<script>never execute</script>",participants:["我","朋友 <A>"],location:"<工作室>",outcome:"按约定完成 <交付>"}};
  const legacy={id:"legacy",kind:"fact",scope_id:"web:a",user_id:"owner",visibility:"private",status:"active",content:"旧事实 <原文>",source_ids:[]};
  const oldEvent={...legacy,id:"old-event",kind:"event",content:"旧事件保留的原文"};
  const state={authenticated:true,authGeneration:0,renderId:1,page:"memory",scope:"web:a",scopes:[{id:"web:a",name:"本地A"},{id:"web:b",name:"本地B"}],settings:{},filters:{},records:[]};
  const config={records:[record,legacy,oldEvent],record,conflicts:[],modal:null,opens:0,closes:0,renders:0,boots:0};
  const modal={open:false},content={firstElementChild:null},body={innerHTML:""},sources={innerHTML:""};
  nodes.set("#modal",modal);nodes.set("#modal-content",content);nodes.set("#modal .modal-body",body);nodes.set("#memory-sources",sources);
  const escape=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const labels={fact:"事实",event:"事件",preference:"偏好",note:"笔记",active:"有效",pending:"待确认",private:"仅本人"};
  const sandbox={window:{ShioriFeatures:{memoryToolsPanel:async()=>""}},state,URLSearchParams,fieldId:0,
    e:escape,label:value=>labels[value]||value||"",icon:()=>'<svg></svg>',iconInline:()=>"",fmt:value=>value||"未记录",toLocal:value=>value||"",scopeName:id=>id,
    $:(selector,root)=>root?.nodes?.get(selector)||nodes.get(selector),$$:()=>[],
    rows:value=>Array.isArray(value)?value:value?.items||[],query:params=>new URLSearchParams(Object.entries(params).filter(([,value])=>value!==undefined&&value!==null&&value!=="")).toString(),
    renderPage:async()=>{config.renders++;state.renderId++;},bootstrap:async()=>{config.boots++;state.renderId++;},
    api:async(url,options={})=>{
      calls.push([url,options.method||"GET",plain(options.body||{})]);
      if(config.intercept){const override=await config.intercept(url,options);if(override?.handled)return override.value;}
      if(url.startsWith("/api/memories?")||url.startsWith("/api/memory/candidates?")){const params=new URL(url,"http://local").searchParams,offset=Number(params.get("offset"))||0;return{items:plain(config.records),total:60,offset,next_offset:offset+50,has_more:offset<50};}
      if(url.endsWith("/conflicts"))return plain(config.conflicts);
      if(url.includes("/sources?")){const offset=Number(new URL(url,"http://local").searchParams.get("offset"))||0;return{items:offset?[{id:"source-later",available:true,message:{id:"source-later",scope_id:"web:a",user_id:"owner",content:"较早的规范来源",created_at:"2026-10-01"}}]:[
        {id:"source-one",available:true,message:{id:"source-one",scope_id:"web:a",user_id:"owner",content:"原始消息 <script>escaped</script>",created_at:"2026-10-02"}},
        {id:"source-missing",available:false,message:{id:"source-missing",scope_id:"web:a",content:"已撤回的旧摘录不应出现"}},
        {id:"source-wrong-scope",available:true,message:{id:"source-wrong-scope",scope_id:"web:b",content:"其他会话不应出现"}}
      ],total:21,offset,limit:20,next_offset:offset+20,has_more:offset===0};}
      if(url==="/api/memories/event-one"&&(!options.method||options.method==="GET"))return plain(config.record);
      if(url.startsWith("/api/memories")&&["PUT","POST"].includes(options.method))return {id:"saved",...plain(options.body)};
      return [];
    }
  };
  vm.createContext(sandbox);vm.runInContext(source("ui-core.js"),sandbox);vm.runInContext(source("memory-page.js"),sandbox);
  sandbox.openModal=(...args)=>{config.modal=args;config.opens++;state.modalRecord=args[4]||null;state.modalKind=args[3]||"";modal.open=true;content.firstElementChild={};body.innerHTML=args[2];sources.innerHTML="";};
  sandbox.closeModal=()=>{config.closes++;config.modal=null;state.modalRecord=null;state.modalKind=null;modal.open=false;};
  sandbox.toast=(...args)=>notices.push(args);sandbox.getForm=form=>form.values;
  const app=source("app.js");vm.runInContext(app.slice(app.indexOf("async function saveModal("),app.indexOf("function modelPayload(")),sandbox);
  const ui=sandbox.window.ShioriMemoryEvents;
  const formValues={kind:"event",memory_event_mode:"structured",scope_id:"web:a",user_id:"owner",visibility:"private",importance:"0.7",valid_to:"",episode_title:"新的事件",episode_summary:"概述",episode_narrative:"完整经过",episode_participants:"我\n朋友",episode_location:"书店",episode_outcome:"约好下次见面"};
  return {sandbox,ui,state,record,legacy,oldEvent,config,calls,notices,nodes,body,sources,modal,formValues,openOther:()=>sandbox.openModal("另一窗口","","其他内容","memory",{id:"other"})};
}
async function main(){
  {const f=fixture(),html=await f.sandbox.renderMemory();
    assert.ok(html.includes("一起完成 &lt;计划&gt;"));assert.ok(html.includes("先讨论，然后做出决定。"));assert.ok(html.includes("朋友 &lt;A&gt;"));assert.ok(html.includes("&lt;工作室&gt;"));
    assert.ok(html.includes("发生时间未知"));assert.ok(!html.includes("2026-11-03T10:00:00Z"),"record creation must not masquerade as event time");
    assert.ok(!html.includes("完整经过的唯一标识"),"list cards do not duplicate long event narrative in the DOM");
    assert.ok(html.includes("旧事实 &lt;原文&gt;"));assert.ok(html.includes("旧事件保留的原文"));assert.ok(html.includes("旧版原文"));assert.ok(!html.includes("<script>"));
    f.state.memoryPage.offset=50;await f.ui.handleAction("memory-kind-filter",{dataset:{kind:"event"}});await f.sandbox.renderMemoryRecords();
    const params=new URL(f.calls.at(-1)[0],"http://local").searchParams;assert.equal(params.get("kind"),"event");assert.equal(params.get("offset"),"0");
    f.state.filters.status="pending";await f.sandbox.renderMemoryRecords();assert.ok(f.calls.at(-1)[0].startsWith("/api/memory/candidates?"));assert.ok(f.calls.at(-1)[0].includes("kind=event"));
  }
  {const f=fixture();f.sandbox.memoryModal();const html=f.config.modal[2];
    assert.ok(html.includes('value="event" selected'));assert.ok(html.includes('name="episode_narrative"'));assert.ok(html.includes('maxlength="3600"'));assert.ok(html.includes('name="memory_event_mode" value="structured"'));
    f.sandbox.memoryModal(f.legacy);assert.ok(f.config.modal[2].includes('value="fact" selected'));assert.ok(f.config.modal[2].includes("旧事实 &lt;原文&gt;"));
    f.sandbox.memoryModal(f.oldEvent);assert.ok(f.config.modal[2].includes('name="memory_event_mode" value="legacy"'));assert.ok(f.config.modal[2].includes("memory-structure-event"));
    const data=f.ui.payload(f.formValues,null);assert.deepEqual(plain(data.episode),{title:"新的事件",summary:"概述",narrative:"完整经过",participants:["我","朋友"],location:"书店",outcome:"约好下次见面"});assert.equal(data.content,"完整经过");assert.ok(!("episode_narrative" in data));
    assert.equal(f.ui.payload({...f.formValues,kind:"fact",content:"保留的事实"},f.record).episode,null);
    const unchanged=f.ui.payload({kind:"event",memory_event_mode:"legacy",content:"旧原文"},f.oldEvent);assert.equal(unchanged.content,"旧原文");assert.ok(!("episode" in unchanged));
    assert.throws(()=>f.ui.payload({...f.formValues,episode_narrative:" "}),/详细经过/);
    assert.throws(()=>f.ui.payload({...f.formValues,episode_narrative:"长".repeat(3601)}),/3600/);
    assert.throws(()=>f.ui.payload({...f.formValues,episode_participants:Array.from({length:9},(_,i)=>"人"+i).join("\n")}),/最多 8/);
    assert.throws(()=>f.ui.payload({...f.formValues,episode_location:"地".repeat(201)}),/200/);
  }
  {const f=fixture(),eventInput={disabled:true},plainInput={disabled:false},event={hidden:true,querySelectorAll:()=>[eventInput]},normal={hidden:false,querySelectorAll:()=>[plainInput]},mode={value:"legacy"},narrative={value:""},content={value:"保留的旧经过"};
    const form={nodes:new Map([["#memory-event-editor",event],["#memory-plain-editor",normal],['[name="memory_event_mode"]',mode],['[name="episode_narrative"]',narrative],['[name="content"]',content]])};
    assert.equal(f.ui.handleChange({name:"kind",value:"event",closest:()=>form}),true);assert.equal(event.hidden,false);assert.equal(eventInput.disabled,false);assert.equal(plainInput.disabled,true);
    f.ui.handleChange({name:"kind",value:"fact",closest:()=>form});assert.equal(event.hidden,true);assert.equal(plainInput.disabled,false);
    f.ui.handleAction("memory-structure-event",{closest:()=>form,dataset:{}});assert.equal(narrative.value,"保留的旧经过");assert.equal(content.value,"保留的旧经过","opt-in structuring preserves the original editor buffer");
  }
  {const f=fixture();await f.sandbox.memoryDetail({...f.record,content:"搜索接口的短摘录",episode:{...f.record.episode,narrative:"截断经过"}});
    const html=f.config.modal[2];assert.ok(html.includes("完整经过的唯一标识"));assert.ok(!html.includes("截断经过"));assert.ok(html.includes("按约定完成 &lt;交付&gt;"));assert.ok(html.includes("&lt;script&gt;escaped"));
    assert.ok(!html.includes("已撤回的旧摘录不应出现"));assert.ok(!html.includes("其他会话不应出现"));assert.ok(html.includes("来源已不可用"));
    assert.ok(f.calls.some(([url])=>url==="/api/memories/event-one"));assert.ok(f.calls.some(([url])=>url.includes("/sources?offset=0&limit=20")));assert.ok(!f.calls.some(([url])=>url.startsWith("/api/messages")));
    const opens=f.config.opens,identity=f.nodes.get("#modal-content").firstElementChild;await f.ui.handleAction("memory-source-page",{dataset:{id:"event-one",offset:"20"}});
    assert.ok(f.sources.innerHTML.includes("较早的规范来源"));assert.equal(f.config.opens,opens);assert.equal(f.nodes.get("#modal-content").firstElementChild,identity,"source paging preserves review controls and modal identity");
    f.config.record.status="pending";f.config.conflicts=[{...f.legacy,content:"可能冲突 <旧事实>"}];await f.sandbox.reviewMemory(f.record);
    assert.equal(f.config.modal[3],"memory-review");assert.equal(f.state.modalRecord.status,"pending");assert.ok(f.config.modal[2].includes('name="replace_conflicts"'));assert.ok(f.config.modal[2].includes("可能冲突 &lt;旧事实&gt;"));assert.ok(f.config.modal[2].includes("完整经过的唯一标识"));
  }
  {const f=fixture(),reasons=[
    ["source_fingerprint_unavailable","旧记录缺少校验信息，无法核验原始来源。"],
    ["source_changed","原始来源已发生改动，无法核验当时的内容。"],
    ["source_revoked","原始来源已撤回，不再展示。"],
    ["source_missing_or_outside_scope","原始来源已不存在，或不在本条记忆所属的会话中。"],
    ["source_identity_mismatch","原始来源的身份信息与本条记忆不符，无法核验。"],
    ["invalid_source_id","来源编号无效，无法读取原始来源。"],
    ["unknown_reason","来源已不可用，可能已删除、改动或超出本条记忆的来源范围。"]
  ];
    f.config.intercept=url=>url.includes("/sources?")?{handled:true,value:{items:reasons.map(([reason],i)=>({id:`unavailable-${i}`,available:false,reason,message:{id:`unavailable-${i}`,scope_id:"web:a",content:"不可用来源的旧摘录"}})),total:reasons.length,offset:0}}:undefined;
    await f.sandbox.memoryDetail(f.record);for(const [,message] of reasons)assert.ok(f.config.modal[2].includes(message));assert.ok(!f.config.modal[2].includes("不可用来源的旧摘录"));
  }
  for(const change of ["modal","scope","page","auth"]){
    const f=fixture(),gate=deferred();f.config.intercept=url=>url==="/api/memories/event-one"?gate.promise.then(()=>({handled:true,value:plain(f.record)})):undefined;
    const opening=f.sandbox.memoryDetail(f.record);if(change==="modal")f.openOther();if(change==="scope")f.state.scope="web:b";if(change==="page"){f.state.page="chat";f.state.renderId++;}if(change==="auth"){f.state.authGeneration++;f.state.authenticated=false;f.modal.open=false;}
    const opens=f.config.opens,body=f.body.innerHTML;gate.resolve();await opening;assert.equal(f.config.opens,opens);assert.equal(f.body.innerHTML,body,"late full-record results cannot replace a newer surface");
  }
  {const f=fixture();await f.sandbox.memoryDetail(f.record);const gate=deferred();f.config.intercept=url=>url.includes("offset=20")?gate.promise.then(()=>({handled:true,value:{items:[],total:0}})):undefined;
    const paging=f.ui.handleAction("memory-source-page",{dataset:{id:"event-one",offset:"20"}});f.openOther();f.sources.innerHTML="another dialog source";gate.resolve();await paging;assert.equal(f.sources.innerHTML,"another dialog source");
  }
  for(const change of ["none","modal","auth"]){
    const f=fixture();f.sandbox.memoryModal(f.record);const gate=deferred();f.config.intercept=(url,options)=>options.method==="PUT"?gate.promise:undefined;
    const saving=f.sandbox.saveModal("memory",{values:{...f.formValues}});assert.equal(f.calls.at(-1)[1],"PUT");assert.equal(f.calls.at(-1)[2].episode.narrative,"完整经过");
    if(change==="modal")f.openOther();if(change==="auth"){f.state.authGeneration++;f.state.authenticated=false;f.modal.open=false;}
    gate.resolve();await saving;assert.equal(f.config.closes,change==="none"?1:0);assert.equal(f.config.boots,change==="none"?1:0);
  }
  for(const change of ["none","modal","auth"]){
    const f=fixture();f.config.record.status="pending";await f.sandbox.reviewMemory(f.record);const gate=deferred();f.config.intercept=url=>url.endsWith("/approve")?gate.promise:undefined;
    const approval=f.ui.handleSubmit("memory-review",{values:{replace_conflicts:true}}),duplicate=f.ui.handleSubmit("memory-review",{values:{replace_conflicts:true}});
    assert.equal(f.calls.filter(([url])=>url.endsWith("/approve")).length,1);assert.deepEqual(f.calls.at(-1)[2],{replace_conflicts:true});
    if(change==="modal")f.openOther();if(change==="auth"){f.state.authGeneration++;f.state.authenticated=false;f.modal.open=false;}
    gate.resolve();await Promise.all([approval,duplicate]);assert.equal(f.config.closes,change==="none"?1:0);assert.equal(f.config.boots,change==="none"?1:0);
  }
  {const f=fixture(),gate=deferred();f.config.intercept=url=>url.startsWith("/api/memories?")?gate.promise.then(()=>({handled:true,value:{items:[plain(f.record)],total:1,offset:0}})):undefined;
    const reading=f.sandbox.renderMemoryRecords();f.state.scope="web:b";f.state.renderId++;f.state.records=[{id:"new-scope"}];gate.resolve();assert.equal(await reading,"");assert.equal(f.state.records[0].id,"new-scope");
  }
  console.log("PASS: structured event cards/forms, legacy preservation, typed pagination, canonical sources, full detail/review and auth/modal races");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
