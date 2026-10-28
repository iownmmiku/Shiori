"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const base=path.join(__dirname,"../shiori/static"),calls=[],toasts=[],nodes=new Map();
const escape=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const RealDate=Date,now=RealDate.parse("2026-10-26T01:00:00+08:00");
class Clock extends RealDate {constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const persona={id:"music",name:"音乐伙伴",interests:["音乐"],exploration_enabled:true};
const state={page:"personaKnowledge",scope:"web:default",knowledgePersona:"music",settings:{paused:false,time:{timezone:"Asia/Hong_Kong"},exploration:{enabled:false}},personas:[persona],filters:{}};
let status={enabled:false,paused:false,searches_today:0,daily_searches:6,items:0,pending:0,personas:{music:{enabled:false,disabled_reason:"global_disabled",searches_today:0,remaining_searches:6,effective_config:{daily_searches:6}}}};
let response={},rendered="",renderCount=0;
const area={innerHTML:""};nodes.set("#persona-exploration-result",area);
const sandbox={window:{},state,Date:Clock,Intl,URL,URLSearchParams,fieldId:0,e:escape,icon:()=>"",
  rows:value=>Array.isArray(value)?value:value?.items || [],query:values=>new URLSearchParams(values).toString(),scope:()=>({persona_id:"music"}),
  fmt:value=>String(value),$:selector=>nodes.get(selector) || null,
  api:async(url,options)=>{
    calls.push([url,options]);
    if(url==="/api/knowledge/status")return structuredClone(status);
    if(url.startsWith("/api/knowledge?"))return [];
    if(url==="/api/knowledge/explore"){
      if(response.last_result)status.personas.music={...status.personas.music,last_result:structuredClone(response.last_result),last_attempt_at:response.last_result.at};
      return structuredClone(response);
    }
    throw Error("unexpected API route");
  },navigate:async page=>{state.page=page;},bootstrap:async()=>{},getForm:form=>form.values,closeModal:()=>{},
  renderPage:async()=>{renderCount++;rendered=await sandbox.window.ShioriFeatures.renderPersonaKnowledge();},toast:(...args)=>toasts.push(args)};
vm.createContext(sandbox);
const readForm=sandbox.getForm;
vm.runInContext(fs.readFileSync(path.join(base,"ui-core.js"),"utf8"),sandbox);
sandbox.getForm=readForm;sandbox.toast=(...args)=>toasts.push(args);
vm.runInContext(fs.readFileSync(path.join(base,"workspace-features.js"),"utf8"),sandbox);
const ui=sandbox.window.ShioriFeatures;
const record=changes=>({at:"2026-10-26T00:30:00+08:00",interest:"音乐",query:"音乐 最新资讯 2026年10月",created:0,updated:0,pending:0,filtered:2,failures:0,reason:"no_relevant_sources",rejections:[
  {url:"https://one.example/article",reason:"网页主段与兴趣主题关联不足"},
  {url:"https://two.example/article",reason:"域名不符合当前来源规则"}
],truncated:false,...changes});

async function main(){
  const cases=[
    ["global_disabled",/全局定期探索已关闭/],["persona_disabled",/此人格的定期探索已关闭/],
    ["no_interests",/尚未设置兴趣主题/],["paused",/程序已暂停/],["degraded",/状态异常/],
    ["daily_limit",/今日探索额度已用完/],["excluded_topics",/全部兴趣焦点都命中排除主题/]
  ];
  for(const [code,expected] of cases){
    status.personas.music.disabled_reason=code;
    const html=await ui.renderPersonaKnowledge();assert.match(html,expected);
    assert.match(html,/自动探索暂不运行/);assert.match(html,/data-action="interest-global-settings"/);
  }
  assert.match(ui.explorationRunState(persona,{enabled:true,daily_searches:0},{disabled_reason:"daily_limit"}),/全局每日探索额度为 0/);
  assert.match(ui.explorationRunState(persona,{enabled:true,daily_searches:6},{disabled_reason:"daily_limit",effective_config:{daily_searches:0}}),/此人格每日探索额度为 0/);
  assert.match(ui.explorationRunState(persona,{enabled:false},{}),/全局定期探索已关闭/);
  assert.match(ui.explorationRunState({...persona,exploration_enabled:false},{enabled:true},{}),/此人格的定期探索已关闭/);
  assert.match(ui.explorationRunState({...persona,interests:[]},{enabled:true},{}),/尚未设置兴趣主题/);
  assert.equal(ui.explorationRunState(persona,{enabled:true},{enabled:true}),"");

  const legacy={last_attempt_at:"2026-10-24T09:58:00Z",last_message:"本次没有取得可用公开资料。已过滤 10 个网页。"};
  let html=ui.explorationDiagnostics(legacy);
  assert.match(html,/2026\/10\/24/);assert.match(html,/17:58/);assert.match(html,/历史结果，非今日执行/);
  assert.match(html,/今日次数按配置时区单独计算/);assert.match(html,/过滤原因未记录/);
  assert.ok(!html.includes("新增 0"),"legacy messages cannot invent a structured result");
  assert.match(ui.explorationDiagnostics({}),/尚无探索记录/);
  html=ui.explorationDiagnostics({last_attempt_at:'<img onerror="evil">',last_message:"<script>old message</script>"});
  assert.match(html,/时间未记录/);assert.ok(!html.includes("<script>"));assert.ok(!html.includes("<img"));

  // The configured zone, rather than UTC or the browser's zone, defines today.
  html=ui.explorationDiagnostics({last_result:record({at:"2026-10-25T16:30:00Z"})});
  assert.ok(!html.includes("历史结果，非今日执行"));assert.match(html,/2026\/10\/26/);
  html=ui.explorationDiagnostics({last_result:record({at:"2026-10-25T15:30:00Z"})});
  assert.match(html,/历史结果，非今日执行/);
  state.settings.time.timezone="invalid/timezone";
  assert.match(ui.explorationDiagnostics({last_result:record()}),/Asia\/Shanghai/);state.settings.time.timezone="Asia/Hong_Kong";

  const hostile=record({interest:'<img src=x onerror="evil">',query:'<script>evil()</script>',filtered:9,
    rejections:[{url:"https://valid.example/path?a=1&b=2",reason:'<img src=x onerror="evil">'},
      ...["javascript:alert(1)","data:text/html,bad","/relative","//host.example/path","https://user:password@host.example/","https://bad.example/\npath"].map(url=>({url,reason:"来源规则不符"}))]});
  html=ui.explorationDiagnostics({last_result:hostile});
  assert.ok(!html.includes("<img"));assert.ok(!html.includes("<script>"));assert.match(html,/&lt;img/);assert.match(html,/&lt;script/);
  assert.match(html,/href="https:\/\/valid\.example\/path\?a=1&amp;b=2"/);assert.match(html,/rel="noopener noreferrer"/);
  assert.ok(!html.includes("javascript:"));assert.ok(!html.includes("user:password"));assert.match(html,/来源地址不可展示/);
  assert.match(html,/已保留明细的原因汇总/);

  const many=record({filtered:24,rejections:Array.from({length:24},(_,i)=>({url:`https://source.example/${i}`,reason:"主题关联不足"}))});
  html=ui.explorationDiagnostics({last_result:many});
  assert.equal((html.match(/target="_blank"/g)||[]).length,12);assert.match(html,/过滤 24/);assert.match(html,/12 条明细/);
  assert.match(html,/部分条目未展示/);assert.match(html,/主题关联不足（12）/);
  html=ui.explorationDiagnostics({last_result:record({created:null,rejections:[],filtered:5,truncated:true})});
  assert.match(html,/新增 未记录/);assert.match(html,/逐页过滤详情未记录/);assert.match(html,/总数以上方汇总为准/);
  for(const [reason,label] of [["collected","已取得公开资料"],["no_sources","未发现可用的网页来源"],["cancelled","探索已取消"],["error","探索未完成"]]){
    assert.ok(ui.explorationDiagnostics({last_result:record({reason})}).includes(label));
  }

  status.personas.music={...status.personas.music,...legacy,disabled_reason:"global_disabled"};
  rendered=await ui.renderPersonaKnowledge();
  assert.match(rendered,/全局今日 0 \/ 6/);assert.match(rendered,/历史结果，非今日执行/);assert.match(rendered,/过滤原因未记录/);
  const before=calls.length,details={open:false};let scrolled=false;
  nodes.set("#exploration-section",{closest:()=>details,scrollIntoView:()=>{scrolled=true;}});
  await ui.handleAction("interest-global-settings",{dataset:{}});
  assert.equal(state.page,"persona");assert.equal(state.botTab,"reply");assert.equal(details.open,true);assert.equal(scrolled,true);
  assert.equal(calls.length,before);assert.equal(state.settings.exploration.enabled,false,"navigation must not enable exploration");

  state.page="personaKnowledge";response={created:0,updated:0,message:"本次探索未取得可用资料。",last_result:record()};
  const target={dataset:{id:"music"},innerHTML:"探索一次",disabled:false,isConnected:true};
  await ui.explorePersona(target);
  assert.equal(renderCount,1);assert.equal(target.disabled,false);assert.equal(target.innerHTML,"探索一次");
  assert.match(area.innerHTML,/逐页过滤原因/);assert.match(area.innerHTML,/one\.example/);
  assert.equal((area.innerHTML.match(/<h3>最近探索/g)||[]).length,1);
  assert.equal((rendered.match(/<h3>最近探索/g)||[]).length,1);
  rendered=await ui.renderPersonaKnowledge();
  assert.match(rendered,/one\.example/);assert.match(rendered,/域名不符合当前来源规则/);
  assert.ok(calls.filter(([url])=>url==="/api/knowledge/explore").length===1);
  assert.ok(!calls.some(([url,options])=>url==="/api/settings" || options?.method==="PUT"));
  console.log("Interest diagnostics: opt-in reasons, historical time, bounded safe rejections, persistent refresh and manual rendering passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
