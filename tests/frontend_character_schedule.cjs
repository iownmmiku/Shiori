"use strict";
// Exercise the native controller's real forms and asynchronous identity guards.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const root = path.join(__dirname, "../shiori/static");
const calls = [], notices = [], nodes = new Map();
const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const settings = {enabled:false, reply_control:false, control_scope:"all", timezone:"Asia/Hong_Kong", daily_calls:12,
  model_id:"", diary_model_id:"", auto_schedule_enabled:true, diary_enabled:true, diary_chat_enabled:false,
  diary_scopes:[], scope_ids:[], slow_min_seconds:60, slow_max_seconds:180, diary_limit:180,
  adaptive_max_delay_minutes:30, active_conversation_minutes:10, transition_notice_minutes:10,
  queue_limit:60, queue_expire_hours:72};
const slot = {start:"00:00", end:"24:00", title:"<script>activity</script>", status:"free", note:"notes", locked:false};
let result = {settings, personas:[{id:"miku",name:"<img src=x onerror=evil()>"}], persona_id:"miku", persona_key:"identity-one", date:"2026-10-29", revision:4,
  scopes:[{id:"qq:group:123",name:"群聊"}], models:[{id:"fast",name:"模型"}], usage:{calls:0,limit:12},
  profile:null, day:{summary:"今天的安排",slots:[slot]}, calendar:null, recurrences:[], diaries:[], snapshots:[], jobs:[],
  current:{date:"2026-10-29",timezone:"Asia/Tokyo",now:"2026-10-29T12:00:00+09:00",slot,status:"free"},
  pending:[{scope_id:"qq:group:123",status:"uncertain",count:2,due_at:1793246400,reason:"<script>error</script>"}]};
const initialResult=structuredClone(result), timers=new Map();
let rendered = "", renderCount = 0, deferState = null, previewResult, deferDiary=null, deferPreview=null, deferJob=null, failState=null;
let deferSettings=null,failSettings=null;
let diaryResponse={diary:null}, jobResponse={}, timerId=0;
const sandbox = {window:{}, state:{page:"characterSchedule",authenticated:true,scope:"qq:group:123"}, fieldId:0,
  document:{hidden:false}, AbortController, URLSearchParams, Object, Map, Number, String, Array, Date,
  setTimeout:(callback,delay) => {const id=++timerId;timers.set(id,{callback,delay});return id;}, clearTimeout:id => timers.delete(id), e:escape, icon:() => "", fmt:value => String(value),
  $:(selector,root) => root?.querySelector?.(selector) || nodes.get(selector) || null,
  $$:(selector,root) => root?.querySelectorAll?.(selector) || [],
  query:params => new URLSearchParams(Object.entries(params).filter(([,v]) => v!==undefined && v!==null && v!=="")).toString(),
  api:async(url,options={}) => {
    calls.push([url, structuredClone(options.body)]);
    if(url.includes("/state?")) { if(failState) {const error=failState;failState=null;throw error;} if(deferState) {const deferred=deferState;deferState=null;return deferred;} return structuredClone(result); }
    if(url.endsWith("/preview")) {if(deferPreview) {const deferred=deferPreview;deferPreview=null;return deferred;} return structuredClone(previewResult);}
    if(url.includes("/diary?")) {if(deferDiary) {const deferred=deferDiary;deferDiary=null;return deferred;} return structuredClone(diaryResponse);}
    if(url.includes("/job?")) {if(deferJob) {const deferred=deferJob;deferJob=null;return deferred;} return structuredClone(jobResponse);}
    if(url.endsWith("/settings")) {
      if(failSettings) {const error=failSettings;failSettings=null;throw error;}
      if(deferSettings) {const deferred=deferSettings;deferSettings=null;await deferred;}
      result.settings={...result.settings,...structuredClone(options.body)};
      return {settings:structuredClone(result.settings)};
    }
    if(url.endsWith("/generate")) return {id:"job-1",status:"queued",message:"正在准备"};
    return {};
  },
  getForm:form => structuredClone(form.values || {}),
  toast:(...args) => notices.push(args), confirmAction:async(_title,_desc,perform) => perform(),
  renderPage:async() => {const id=++renderCount,html=await sandbox.window.ShioriCharacterSchedule.render();if(id===renderCount) rendered=html;},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(root,"ui-core.js"),"utf8"),sandbox);
// The test only substitutes DOM transport; field/select/toggle markup is real.
sandbox.getForm=form => structuredClone(form.values || {});
sandbox.toast=(...args)=>notices.push(args);
sandbox.confirmAction=async(_title,_desc,perform)=>perform();
vm.runInContext(fs.readFileSync(path.join(root,"character-schedule.js"),"utf8"),sandbox);
let ui=sandbox.window.ShioriCharacterSchedule;
const field=(key,value,type="text")=>({dataset:{lifeField:key},value,checked:value,type});
const row=(fields,day="")=>({dataset:{day},querySelectorAll:()=>fields});
const form=(kind,values,groups={})=>({dataset:{lifeKind:kind,persona:"miku",personaKey:"identity-one",date:"2026-10-29",revision:"4"},values,
  querySelector:()=>null,querySelectorAll:selector=>groups[selector] || []});

async function asynchronousRegressions(){
  const fresh=async(overrides={})=>{
    result={...structuredClone(initialResult),...overrides}; calls.length=0;notices.length=0;nodes.clear();timers.clear();
    deferState=deferDiary=deferPreview=deferJob=failState=deferSettings=failSettings=null;diaryResponse={diary:null};jobResponse={};
    sandbox.state.page="characterSchedule";sandbox.confirmAction=async(_title,_desc,perform)=>perform();
    vm.runInContext(fs.readFileSync(path.join(root,"character-schedule.js"),"utf8"),sandbox);
    ui=sandbox.window.ShioriCharacterSchedule;rendered=await ui.render();
  };
  const count=action=>calls.filter(([url])=>url.endsWith("/"+action)).length;
  const last=action=>calls.filter(([url])=>url.endsWith("/"+action)).at(-1)?.[1];
  const tick=()=>new Promise(resolve=>setImmediate(resolve));
  const timer=delay=>{const item=Array.from(timers.values()).find(value=>value.delay===delay);assert.ok(item,`expected ${delay} ms poll`);return item.callback;};
  const editable=(summary="仍需保留的草稿")=>form("day",{summary},{'[data-life-row="slot"]':[row([
    field("start","00:00"),field("end","24:00"),field("title","休息"),field("status","free"),field("locked",false,"checkbox")])]});

  // A selected date may be older than the state endpoint's recent diary list.
  await fresh({date:"2026-08-01",diaries:[]});
  diaryResponse={diary:{date:result.date,scope_id:"",persona_key:result.persona_key,title:"早期日记",content:"精确日期查询的正文"}};
  await ui.handleAction("life-tab",{dataset:{tab:"diary"}});
  assert.match(rendered,/精确日期查询的正文/);
  assert.match(calls.filter(([url])=>url.includes("/diary?")).at(-1)[0],/date=2026-08-01/);
  result.diaries=[{date:result.date,scope_id:"",persona_key:"old-persona",current_persona:false,title:"旧人格日记",content:"不可作为当前正文"}];
  diaryResponse={diary:null};rendered=await ui.render();
  assert.match(rendered,/选择日期查看日记/);assert.ok(!rendered.includes("旧人格日记"));assert.ok(!rendered.includes("不可作为当前正文"));
  result.date="2026-08-02";diaryResponse={diary:{date:result.date,title:"日期切换后的正文",content:"第二天"}};
  await ui.handleChange({id:"life-date",value:result.date});assert.match(rendered,/日期切换后的正文/);
  await ui.handleAction("life-tab",{dataset:{tab:"today"}});
  diaryResponse={diary:{date:result.date,title:"重新进入时的正文",content:"已更新"}};
  await ui.handleAction("life-tab",{dataset:{tab:"diary"}});assert.match(rendered,/重新进入时的正文/);

  // A late exact lookup must not overwrite the next persona/date selection.
  let resolveDiary;deferDiary=new Promise(resolve=>{resolveDiary=resolve;});
  const oldDiary=ui.render();await tick();
  result={...result,persona_id:"other",persona_key:"other-key",personas:[{id:"miku",name:"初音"},{id:"other",name:"另一个角色"}]};
  diaryResponse={diary:{date:result.date,persona_key:"other-key",title:"另一个角色的日记",content:"新结果"}};
  await ui.handleChange({id:"life-persona",value:"other"});
  resolveDiary({diary:{date:"2026-08-02",title:"过期异步正文",content:"旧结果"}});
  assert.equal(await oldDiary,"");assert.match(rendered,/另一个角色的日记/);assert.ok(!rendered.includes("过期异步正文"));

  // Scope choices follow the selected persona, while settings remain global.
  const scopes=[{id:"qq:group:123",name:"本角色素材",persona_id:"miku"},{id:"qq:group:456",name:"别人的素材",persona_id:"other"}];
  await fresh({date:"2026-10-28",scopes,settings:{...settings,diary_scopes:scopes.map(item=>item.id)}});
  await ui.handleAction("life-tab",{dataset:{tab:"diary"}});
  assert.match(rendered,/本角色素材/);assert.ok(!rendered.includes("别人的素材"));
  await ui.handleAction("life-tab",{dataset:{tab:"preview"}});assert.match(rendered,/本角色素材/);assert.ok(!rendered.includes("别人的素材"));
  await ui.handleAction("life-tab",{dataset:{tab:"settings"}});assert.match(rendered,/本角色素材/);assert.match(rendered,/别人的素材/);

  // Confirmation closures must retain the identity and source the user saw.
  await fresh({date:"2026-10-28",scopes:[scopes[0],{...scopes[1],persona_id:"miku"}],settings:{...settings,diary_scopes:scopes.map(item=>item.id)}});
  await ui.handleAction("life-tab",{dataset:{tab:"diary"}});
  await ui.handleChange({id:"life-diary-scope",value:scopes[0].id});
  let confirm;sandbox.confirmAction=async(_title,_desc,perform)=>{confirm=perform;};
  await ui.handleAction("life-generate",{dataset:{kind:"diary"}});
  await ui.handleChange({id:"life-diary-scope",value:scopes[1].id});
  await assert.rejects(confirm(),/日记来源已变化/);assert.equal(count("generate"),0);
  await ui.handleAction("life-generate",{dataset:{kind:"diary"}});await confirm();
  assert.equal(last("generate").scope_id,scopes[1].id);assert.equal(last("generate").revision,4);
  assert.equal(last("generate").persona_key,"identity-one");
  await fresh();sandbox.confirmAction=async(_title,_desc,perform)=>{confirm=perform;};
  await ui.handleAction("life-generate",{dataset:{kind:"day"}});
  result.persona_key="identity-two";rendered=await ui.render();
  await assert.rejects(confirm(),/人格内容已变化/);assert.equal(count("generate"),0);
  await ui.handleAction("life-generate",{dataset:{kind:"day"}});
  result.revision=5;rendered=await ui.render();
  await assert.rejects(confirm(),/角色日程已变化/);assert.equal(count("generate"),0);

  await fresh();ui.handleInput({closest:()=>editable()});
  sandbox.confirmAction=async(_title,_desc,perform)=>{confirm=perform;};
  await ui.handleAction("life-restore",{dataset:{id:"snapshot-one"}});
  result.persona_key="identity-two";rendered=await ui.render();
  await assert.rejects(confirm(),/人格内容已变化/);assert.equal(count("restore"),0);assert.match(rendered,/仍需保留的草稿/);

  await fresh();ui.handleInput({closest:()=>editable()});
  sandbox.confirmAction=async(_title,_desc,perform)=>{confirm=perform;};
  await ui.handleAction("life-discard",{dataset:{}});
  await ui.handleAction("life-tab",{dataset:{tab:"profile"}});
  await assert.rejects(confirm(),/页面已变化/);
  await ui.handleAction("life-tab",{dataset:{tab:"today"}});assert.match(rendered,/仍需保留的草稿/);
  await ui.handleAction("life-discard",{dataset:{}});
  ui.handleInput({closest:()=>editable("确认后又修改的内容")});
  await assert.rejects(confirm(),/草稿已变化/);rendered=await ui.render();assert.match(rendered,/确认后又修改的内容/);

  // Rejected loads restore the last loaded identity; late old-form input is ignored.
  await fresh();failState=new Error("选择加载失败");
  await assert.rejects(ui.handleChange({id:"life-persona",value:"other"}),/选择加载失败/);
  await ui.handleSubmit("character-life-day",editable());assert.equal(last("save").persona_id,"miku");assert.equal(last("save").date,"2026-10-29");
  result.date="2026-10-30";await ui.handleChange({id:"life-date",value:result.date});
  ui.handleInput({closest:()=>editable("旧日期输入不应污染新日期")});rendered=await ui.render();assert.ok(!rendered.includes("旧日期输入不应污染新日期"));
  failState=new Error("日期加载失败");await assert.rejects(ui.handleChange({id:"life-date",value:"2026-10-31"}),/日期加载失败/);
  const correctDate=editable();correctDate.dataset.date="2026-10-30";
  await ui.handleSubmit("character-life-day",correctDate);assert.equal(last("save").date,"2026-10-30");

  // A preview cannot return after its tab has been replaced or its form expired.
  await fresh();await ui.handleAction("life-tab",{dataset:{tab:"preview"}});
  const output={innerHTML:""};nodes.set("#life-preview-result",output);
  let resolvePreview;deferPreview=new Promise(resolve=>{resolvePreview=resolve;});
  const oldPreview=ui.handleSubmit("character-life-preview",form("preview",{time:"03:00",scope_id:""}));
  await ui.handleAction("life-tab",{dataset:{tab:"today"}});
  resolvePreview({current:{slot:{title:"过期预演"}}});await oldPreview;
  assert.equal(output.innerHTML,"");
  result.persona_key="identity-two";rendered=await ui.render();
  await assert.rejects(ui.handleSubmit("character-life-preview",form("preview",{time:"03:00",scope_id:""})),/人格内容已变化/);

  // A running job for an old prompt is never shown as the new persona's job.
  const running={id:"job-real",persona_id:"miku",persona_key:"identity-one",date:"2026-10-29",status:"running",message:"本次生成"};
  await fresh({jobs:[{...running,persona_key:"obsolete",message:"旧人格任务"}]});
  assert.ok(!rendered.includes("旧人格任务"));ui.afterRender();timer(15000);
  await fresh({jobs:[running]});ui.afterRender();
  const jobNode={innerHTML:""};nodes.set("#life-job-status",jobNode);
  let resolveJob;deferJob=new Promise(resolve=>{resolveJob=resolve;});const polling=timer(2000)();
  result.date="2026-10-30";await ui.handleChange({id:"life-date",value:result.date});
  resolveJob({...running,status:"failed",message:"过期任务响应"});await polling;
  assert.equal(jobNode.innerHTML,"");

  // A slow status poll cannot replace a newer render's optimistic revision.
  await fresh();ui.afterRender();let resolveState;
  const olderState=structuredClone(result);deferState=new Promise(resolve=>{resolveState=resolve;});const statusPolling=timer(15000)();
  result.revision=5;rendered=await ui.render();resolveState(olderState);await statusPolling;
  await ui.handleAction("life-generate",{dataset:{kind:"day"}});assert.equal(last("generate").revision,5);
  await fresh();nodes.set("#life-job-status",jobNode);ui.afterRender();result.jobs=[running];await timer(15000)();
  assert.match(jobNode.innerHTML,/本次生成/);timer(2000);

  // Text inputs must round-trip multiple URLs, including valid URL commas.
  const urls=["https://example.com/one,two","https://example.com/three"];
  await fresh({calendar:{events:[{title:"活动",start:result.date,end:result.date,source_urls:urls}]}});
  await ui.handleAction("life-tab",{dataset:{tab:"calendar"}});
  assert.match(rendered,/value="https:\/\/example\.com\/one,two https:\/\/example\.com\/three"/);
  const calendar=form("calendar",{}, {'[data-life-row="event"]':[row([field("title","活动"),field("start","2026-10-29"),field("end","2026-10-29"),field("kind","activity"),field("day_off",false,"checkbox"),field("source_urls",urls.join(" ")),field("description","")])]});
  await ui.handleSubmit("character-life-calendar",calendar);assert.deepEqual(Array.from(last("save").value.events[0].source_urls),urls);
  await fresh();await ui.handleAction("life-tab",{dataset:{tab:"diary"}});
  assert.match(rendered,/data-kind="diary" disabled aria-describedby="life-diary-date-hint"/);assert.match(rendered,/选中日期尚未结束/);
  result.date="2026-10-28";await ui.handleChange({id:"life-date",value:result.date});assert.ok(!rendered.includes('data-kind="diary" disabled'));
  await fresh({today:"2026-10-29",current:null});await ui.handleAction("life-tab",{dataset:{tab:"diary"}});
  assert.match(rendered,/data-kind="diary" disabled aria-describedby="life-diary-date-hint"/);

  // The header master switch reflects saved settings on every page tab.
  const masterMarkup=()=>{
    const match=rendered.match(/<input\b(?=[^>]*\bid="life-master-toggle")[^>]*>/);
    assert.ok(match,"every schedule tab exposes the header master switch");return match[0];
  };
  const isChecked=markup=>/\bchecked(?:\s|>|=)/.test(markup);
  const runtimeEnabled=()=>{
    const match=rendered.match(/<input\b(?=[^>]*\bname="enabled")[^>]*>/);
    assert.ok(match,"runtime settings retain their enabled checkbox");return isChecked(match[0]);
  };
  const masterTarget=checked=>{
    const target={id:"life-master-toggle",checked,disabled:false,dataset:{},closest:()=>null};
    nodes.set("#life-master-toggle",target);return target;
  };
  await fresh();
  for(const enabled of [false,true]) {
    result.settings.enabled=enabled;
    for(const tab of ["today","profile","calendar","diary","settings","preview"]) {
      await ui.handleAction("life-tab",{dataset:{tab}});
      assert.equal(isChecked(masterMarkup()),enabled,`${tab} header uses persisted enabled=${enabled}`);
      assert.match(rendered,/id="life-master-toggle"[^>]*>\s*<span(?:\s[^>]*)?><\/span>/,"custom switches require a visible track after the hidden checkbox");
    }
  }

  // Immediate toggles submit only enabled, preserving the rest of a runtime draft.
  await fresh();await ui.handleAction("life-tab",{dataset:{tab:"settings"}});
  const runtimeDraft=form("settings",{...settings,enabled:false,daily_calls:37,model_id:"unsaved-model",reply_control:true},{
    '[name="scope_ids"]:checked':[{value:"qq:group:123"}], '[name="diary_scopes"]:checked':[]});
  ui.handleInput({closest:()=>runtimeDraft});
  const enableTarget=masterTarget(true);
  assert.equal(await ui.handleChange(enableTarget),true);
  assert.deepEqual(last("settings"),{enabled:true},"master toggle cannot save unrelated draft fields");
  assert.equal(result.settings.enabled,true);assert.equal(result.settings.daily_calls,12);assert.equal(result.settings.reply_control,false);
  assert.equal(isChecked(masterMarkup()),true);assert.equal(runtimeEnabled(),true);
  assert.match(rendered,/name="daily_calls"[^>]*value="37"/);assert.match(rendered,/value="unsaved-model" selected/);
  runtimeDraft.values.enabled=runtimeEnabled();
  await ui.handleSubmit("character-life-settings",runtimeDraft);
  assert.equal(last("settings").enabled,true,"later runtime save cannot undo the successful master toggle");
  assert.equal(last("settings").daily_calls,37);assert.equal(last("settings").model_id,"unsaved-model");
  assert.equal(last("settings").reply_control,true);assert.deepEqual(last("settings").scope_ids,["qq:group:123"]);

  runtimeDraft.values={...runtimeDraft.values,enabled:false,daily_calls:39};ui.handleInput({closest:()=>runtimeDraft});
  rendered=await ui.render();assert.equal(isChecked(masterMarkup()),true,"header ignores an unsaved runtime enabled draft");
  await ui.handleChange(masterTarget(false));assert.deepEqual(last("settings"),{enabled:false});
  assert.equal(runtimeEnabled(),false);assert.match(rendered,/name="daily_calls"[^>]*value="39"/);
  assert.equal(result.settings.daily_calls,37,"disabling cannot flush the pending quota edit");

  // Failed requests revert the checkbox and keep both persisted state and drafts.
  await fresh();await ui.handleAction("life-tab",{dataset:{tab:"settings"}});
  const failedDraft=form("settings",{...settings,daily_calls:41});ui.handleInput({closest:()=>failedDraft});
  failSettings=new Error("总开关保存失败");const failureTarget=masterTarget(true);
  await assert.rejects(ui.handleChange(failureTarget),/总开关保存失败/);
  assert.equal(failureTarget.checked,false);assert.equal(failureTarget.disabled,false);assert.equal(result.settings.enabled,false);
  rendered=await ui.render();assert.equal(isChecked(masterMarkup()),false);assert.equal(runtimeEnabled(),false);
  assert.match(rendered,/name="daily_calls"[^>]*value="41"/);

  // A second change while the first request is pending cannot enqueue a duplicate.
  await fresh();let resolveSettings;deferSettings=new Promise(resolve=>{resolveSettings=resolve;});
  const firstTarget=masterTarget(true),saving=ui.handleChange(firstTarget);await tick();
  assert.equal(count("settings"),1);assert.equal(firstTarget.disabled,true,"the visible master control is disabled while saving");
  const duplicateTarget=masterTarget(true);
  await assert.rejects(ui.handleChange(duplicateTarget),/正在|稍后|等待/);
  assert.equal(count("settings"),1);assert.equal(duplicateTarget.checked,false);
  resolveSettings();await saving;assert.equal(result.settings.enabled,true);assert.equal(isChecked(masterMarkup()),true);

  // Ordinary controls must not accept edits that remember() skips while a
  // mutation is in flight. Both success and failure restore their prior state.
  const originalQueryAll=sandbox.$$;
  try {
    for (const fail of [false,true]) {
      await fresh();await ui.handleAction("life-tab",{dataset:{tab:"settings"}});
      const draft=form("settings",{...settings,daily_calls:37});ui.handleInput({closest:()=>draft});
      const controls=[{disabled:false},{disabled:false},{disabled:true}];
      sandbox.$$=(selector,root)=>selector==='.life-page input,.life-page select,.life-page textarea' ? controls : originalQueryAll(selector,root);
      let finish;deferSettings=new Promise((resolve,reject)=>{finish=fail ? ()=>reject(new Error("保存失败")) : resolve;});
      const saving=ui.handleChange(masterTarget(true));
      const finished=fail ? assert.rejects(saving,/保存失败/) : saving;
      await tick();assert.ok(controls.every(item=>item.disabled),"visible input, select and textarea controls are paused during a save");
      finish();await finished;
      assert.deepEqual(controls.map(item=>item.disabled),[false,false,true]);
      rendered=await ui.render();assert.match(rendered,/name="daily_calls"[^>]*value="37"/);
    }
  } finally {sandbox.$$=originalQueryAll;}

  // Both forms on the calendar tab share the persona's revision. Saving the
  // calendar safely rebases a recurrence draft only while its source is intact.
  const originalAPI=sandbox.api;
  try {
    sandbox.api=async(url,options={})=>{
      if (!url.endsWith("/save")) return originalAPI(url,options);
      calls.push([url,structuredClone(options.body)]);
      const payload=options.body;
      if(payload.revision!==result.revision) throw new Error("revision conflict");
      if(payload.kind==="calendar") result.calendar=structuredClone(payload.value);
      if(payload.kind==="recurrences") result.recurrences=structuredClone(payload.value);
      result.revision++;return structuredClone(result);
    };
    await fresh();await ui.handleAction("life-tab",{dataset:{tab:"calendar"}});
    const calendar=form("calendar",{}, {'[data-life-row="event"]':[]});
    const recurring=form("recurrences",{}, {'[data-life-row="recurrence"]':[row([
      field("title","本地生日草稿"),field("kind","anniversary"),field("month","8"),field("day","31"),
      field("duration_days","1"),field("start_year",""),field("end_year",""),field("enabled",true,"checkbox")])]});
    ui.handleInput({closest:()=>calendar});ui.handleInput({closest:()=>recurring});
    await ui.handleSubmit("character-life-calendar",calendar);
    assert.equal(result.revision,5);assert.match(rendered,/本地生日草稿/);
    await ui.handleSubmit("character-life-recurrences",recurring);
    assert.equal(last("save").revision,5);assert.equal(result.recurrences[0].title,"本地生日草稿");

    // A real same-section remote edit stays protected after repeated refreshes.
    await fresh();await ui.handleAction("life-tab",{dataset:{tab:"calendar"}});
    ui.handleInput({closest:()=>calendar});ui.handleInput({closest:()=>recurring});
    result.recurrences=[{title:"远端生日安排",kind:"anniversary",month:9,day:1,duration_days:1,enabled:true}];result.revision++;
    for(let i=0;i<2;i++) {
      await ui.handleAction("life-refresh",{dataset:{}});
      assert.match(rendered,/这部分已保存内容已变化/);assert.match(rendered,/本地生日草稿/);
      assert.match(rendered,/type="submit" disabled>保存重复规则/);
      await assert.rejects(ui.handleSubmit("character-life-recurrences",recurring),/草稿已保留/);
    }
    assert.equal(count("save"),0,"a known conflict cannot overwrite the remote section");
    let confirm;sandbox.confirmAction=async(_title,_desc,perform)=>{confirm=perform;};
    await ui.handleAction("life-reload-draft",{dataset:{kind:"recurrences"}});
    assert.match(rendered,/本地生日草稿/,"opening the confirmation retains the local draft");
    await confirm();assert.match(rendered,/远端生日安排/);assert.ok(!rendered.includes("本地生日草稿"));
    assert.match(rendered,/有未保存修改/,"reloading one section retains the other section's draft");
  } finally {sandbox.api=originalAPI;}
}

async function main(){
  rendered=await ui.render();
  assert.match(rendered,/角色日程/); assert.match(rendered,/日程总览/); assert.match(rendered,/待回留言/);
  assert.match(rendered,/data-action="life-queue-retry"/); assert.match(rendered,/data-scope="qq:group:123"/);
  assert.ok(!rendered.includes("<script>")); assert.ok(!rendered.includes("<img src="));
  assert.match(rendered,/12:00/); assert.match(rendered,/Asia\/Tokyo/);
  assert.match(rendered,/&lt;script&gt;activity/); assert.match(rendered,/&lt;script&gt;error/);
  assert.equal(calls.filter(([url])=>url.endsWith("/generate")).length,0,"opening a page never charges for generation");

  // Edits survive a background state refresh, and retain the original revision.
  const dayForm=form("day",{summary:"未保存的草稿"},{'[data-life-row="slot"]':[row([
    field("start","00:00"),field("end","24:00"),field("title","休息"),field("status","slow"),
    field("note","说明"),field("locked",true,"checkbox"),field("slow_min_seconds","15","number"),field("slow_max_seconds","30","number")])]});
  ui.handleInput({closest:()=>dayForm});
  result.day.summary="后台更新的安排"; result.revision=5;
  rendered=await ui.render(); assert.match(rendered,/未保存的草稿/); assert.match(rendered,/data-revision="4"/);
  await assert.rejects(ui.handleSubmit("character-life-day",dayForm),/草稿已保留/);
  assert.equal(calls.filter(([url])=>url.endsWith("/save")).length,0);
  await ui.handleAction("life-reload-draft",{dataset:{kind:"day"}});
  assert.match(rendered,/后台更新的安排/);assert.ok(!rendered.includes("未保存的草稿"));
  result=structuredClone(initialResult);rendered=await ui.render();ui.handleInput({closest:()=>dayForm});
  await ui.handleSubmit("character-life-day",dayForm);
  const saved=calls.find(([url])=>url.endsWith("/save"))[1];
  assert.equal(saved.persona_id,"miku"); assert.equal(saved.revision,4); assert.equal(saved.kind,"day");
  assert.equal(saved.persona_key,"identity-one");
  assert.equal(saved.value.slots[0].slow_min_seconds,15); assert.equal(saved.value.slots[0].locked,true);
  assert.ok(!("days" in saved.value.slots[0]));
  const before=calls.length;
  await assert.rejects(ui.handleSubmit("character-life-day",{...dayForm,dataset:{...dayForm.dataset,persona:"other"}}),/角色或日期已变化/);
  assert.equal(calls.length,before,"a stale persona cannot receive another persona's edits");

  await ui.handleAction("life-tab",{dataset:{tab:"profile"}});
  assert.match(rendered,/每周基础作息/); assert.match(rendered,/data-day="7"/);
  const profileForm=form("profile",{world_type:"real",world_name:"世界",timezone:"Asia/Tokyo",countries:"jp，cn",identity:"音乐人"},
    {'[data-life-row="slot"]':[row([field("start","00:00"),field("end","24:00"),field("title","活动"),field("status","free"),field("locked",false,"checkbox")],"7")]});
  await ui.handleSubmit("character-life-profile",profileForm);
  const profile=calls.filter(([url])=>url.endsWith("/save")).at(-1)[1].value;
  assert.deepEqual(Array.from(profile.countries),["JP","CN"]); assert.deepEqual(Array.from(profile.weekly[0].days),[7]);

  await ui.handleAction("life-tab",{dataset:{tab:"settings"}});
  assert.match(rendered,/按日程控制回复/); assert.match(rendered,/默认关闭/); assert.match(rendered,/联网参考与留言保留/);
  const settingForm=form("settings",{...settings,scope_ids:"wrong",diary_scopes:"wrong"},{
    '[name="scope_ids"]:checked':[{value:"qq:group:123"}], '[name="diary_scopes"]:checked':[]});
  await ui.handleSubmit("character-life-settings",settingForm);
  const config=calls.find(([url])=>url.endsWith("/settings"))[1];
  assert.deepEqual(Array.from(config.scope_ids),["qq:group:123"]); assert.deepEqual(Array.from(config.diary_scopes),[]);
  assert.equal(config.reply_control,false); assert.equal(config.queue_limit,60);

  await ui.handleAction("life-tab",{dataset:{tab:"preview"}});
  const output={innerHTML:""};nodes.set("#life-preview-result",output);
  previewResult={read_only:true,model_called:false,current:{slot:{title:"睡眠"},status:"busy",timezone:"Asia/Tokyo",next_contact_at:1793246400},policy:{active:false}};
  await ui.handleSubmit("character-life-preview",form("preview",{time:"03:00",scope_id:"qq:group:123"}));
  assert.match(output.innerHTML,/睡眠/); assert.match(output.innerHTML,/未调用模型或发送消息/); assert.match(output.innerHTML,/不启用日程回复控制/);
  assert.match(output.innerHTML,/2026/); assert.equal(calls.filter(([url])=>url.endsWith("/generate")).length,0);

  // Explicit queue retry carries only its own scope; it cannot re-send on open.
  await ui.handleAction("life-queue-retry",{dataset:{scope:"qq:group:123"}});
  assert.deepEqual(calls.find(([url])=>url.endsWith("/queue"))[1],{scope_id:"qq:group:123",action:"retry"});

  await ui.handleAction("life-tab",{dataset:{tab:"diary"}});
  await ui.handleChange({id:"life-diary-scope",value:"",closest:()=>null});
  assert.match(rendered,/选择日期查看日记/); assert.ok(!rendered.includes("undefined"),"a null diary must not become a fake empty entry");

  // Changing a persona's prompt can preserve a numeric revision; the origin
  // fingerprint still prevents an old draft from being saved into that persona.
  await ui.handleAction("life-tab",{dataset:{tab:"today"}});
  ui.handleInput({closest:()=>dayForm});
  result.persona_key="identity-two";result.revision=4;
  rendered=await ui.render();
  assert.match(rendered,/人格内容已变化/);assert.match(rendered,/data-persona-key="identity-one"/);
  assert.match(rendered,/type="submit" disabled>保存当天日程/);
  const beforeStaleSave=calls.length;
  await assert.rejects(ui.handleSubmit("character-life-day",dayForm),/旧草稿不能保存/);
  assert.equal(calls.length,beforeStaleSave);
  await ui.handleAction("life-discard",{dataset:{}});
  assert.match(rendered,/data-persona-key="identity-two"/);assert.ok(!rendered.includes("这份草稿属于旧的"));

  await ui.handleAction("life-generate",{dataset:{kind:"day"}});
  assert.equal(calls.filter(([url])=>url.endsWith("/generate")).at(-1)[1].persona_key,"identity-two");

  // A slower state response cannot replace the later selected persona.
  let resolveOld;deferState=new Promise(resolve=>{resolveOld=resolve;});
  const first=ui.render();
  result={...result,persona_id:"other",personas:[{id:"other",name:"第二个角色"}],date:"2026-10-30"};
  const newer=await ui.render();resolveOld({persona_id:"miku",date:"2026-10-29",settings,personas:[]});
  assert.equal(await first,"");assert.match(newer,/第二个角色/);
  assert.equal(await ui.handleAction("unrelated",{dataset:{}}),false);
  assert.equal(await ui.handleSubmit("other-form",{}),false);
  assert.equal(await ui.handleChange({id:"unrelated",closest:()=>null}),false);

  result={...result,persona_id:"",personas:[],profile:null,current:null,day:null,jobs:[]};
  const empty=await ui.render();assert.match(empty,/先创建一个人格/);assert.match(empty,/前往机器人配置/);
  assert.ok(!empty.includes('data-form="character-life-day"'),"an empty persona library cannot show a writable phantom plan");

  const app=fs.readFileSync(path.join(root,"app.js"),"utf8"),index=fs.readFileSync(path.join(root,"index.html"),"utf8"),css=fs.readFileSync(path.join(root,"character-schedule.css"),"utf8");
  assert.match(app,/characterSchedule:window\.ShioriCharacterSchedule\?\.render/);
  const assets=fs.readFileSync(path.join(root,"asset-loader.js"),"utf8");
  assert.match(index,/asset-loader\.js\?v=/);
  assert.ok(!index.includes('/static/character-schedule.js?'),"schedule code is lazy loaded");
  assert.match(assets,/characterSchedule:.*script:'character-schedule\.js'.*css:'character-schedule\.css'/);
  assert.match(css,/@media\(max-width:700px\)/);assert.match(css,/grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  await asynchronousRegressions();
  console.log("Character schedule: forms, drafts, exact diaries, identity guards, stale polls and master-switch regressions passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
