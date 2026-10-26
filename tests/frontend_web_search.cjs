"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname,"../mikubot/static/web-search.js"),"utf8");
const escape = value => String(value ?? "").replace(/[&<>"']/g,char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const decode = value => String(value ?? "").replace(/&(?:amp|lt|gt|quot|#39);/g,value => ({"&amp;":"&","&lt;":"<","&gt;":">","&quot;":'"',"&#39;":"'"}[value]));
const plain = value => JSON.parse(JSON.stringify(value));
const calls = [], toasts = [];
let apiError = null, testResult = {channel:"public",results:[]}, statusResult = null, bootstraps = 0, bootstrapError = null;
const settings = {
  search:{enabled:true,provider:"tavily",timeout_seconds:12,max_results:5,daily_calls:100,fallback_public:false},
  search_providers:[
    {id:"tavily",base_url:"https://api.tavily.com/search",api_key_set:true,api_key:"SERVER_SECRET_MUST_NOT_APPEAR"},
    {id:"deepseek",base_url:"https://api.deepseek.com/v1/tools/search",api_key_set:true},
    {id:"brave",base_url:"https://api.search.brave.com/res/v1/web/search",api_key_set:false}
  ]
};
const sandbox = {window:{},URL,state:{settings},fieldId:0,e:escape,icon:()=>"",
  api:async (url,options) => {
    calls.push([url,plain(options || {})]);
    if(apiError && url === apiError.url)throw Object.assign(new Error(apiError.message),{code:apiError.code});
    if(url === "/api/search/status")return statusResult || {enabled:true,provider:sandbox.state.settings.search?.provider || "public",used_today:3,daily_calls:100,remaining:97};
    if(url === "/api/search/test")return testResult;
    return {};
  },
  getForm:form => Object.fromEntries(Object.entries(form.controls).map(([name,input]) => [name,input.type === "checkbox" ? input.checked : input.value])),
  bootstrap:async () => { bootstraps++; if(bootstrapError)throw new Error(bootstrapError); }
};
vm.createContext(sandbox);
const readForm = sandbox.getForm;
vm.runInContext(fs.readFileSync(path.join(__dirname,"../mikubot/static/ui-core.js"),"utf8"),sandbox);
sandbox.getForm = readForm;
sandbox.toast = (...args) => toasts.push(args);
vm.runInContext(source,sandbox);
const ui = sandbox.window.MikubotWebSearch;

class Area {
  constructor(){this._html="";this._text="";}
  set innerHTML(value){this._html=String(value);this._text="";}
  get innerHTML(){return this._html;}
  set textContent(value){this._text=String(value);this._html="";}
  get textContent(){return this._text;}
}
function attrs(tag){
  const out={};for(const match of tag.matchAll(/([\w-]+)="([^"]*)"/g))out[match[1]]=decode(match[2]);
  return out;
}
function root(){
  const status=new Area(),results=new Area();
  return {status,results,querySelector:selector => selector === "[data-web-search-status]" ? status : selector === "[data-web-search-results]" ? results : null};
}
function formFrom(html,kind="web-search",container=root()){
  const match=html.match(new RegExp(`<form data-form="${kind}"([^>]*)>([\\s\\S]*?)<\\/form>`));
  assert.ok(match,`${kind} form exists`);
  const form={dataset:{form:kind,provider:attrs(match[1])["data-provider"]},controls:{},container,
    closest:selector => selector === "[data-web-search]" ? container : null};
  function add(fragment){
    for(const match of fragment.matchAll(/<input\b[^>]*>/g)){
      const a=attrs(match[0]);if(!a.name)continue;
      form.controls[a.name]={name:a.name,type:a.type,value:a.value ?? "",checked:/\bchecked\b/.test(match[0]),closest:() => form};
    }
    for(const match of fragment.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)){
      const a=attrs(match[1]), options=[...match[2].matchAll(/<option\b([^>]*)>/g)];
      const picked=options.find(item=>/\bselected\b/.test(item[1])) || options[0];
      if(a.name)form.controls[a.name]={name:a.name,value:attrs(picked?.[1] || "").value || "",closest:() => form};
    }
  }
  const panel=new Area();
  Object.defineProperty(panel,"innerHTML",{get:()=>panel._html,set:value=>{
    panel._html=String(value);
    for(const key of ["base_url","api_key","clear_api_key"])delete form.controls[key];
    add(panel._html);
  }});
  add(match[2]);
  panel._html=match[2].match(/<div data-web-search-provider>([\s\S]*?)<\/div><details/)?.[1] || "";
  form.submit={disabled:false};form.panel=panel;
  form.querySelector=selector => {
    if(selector === "[data-web-search-provider]")return panel;
    if(selector === '[type="submit"]')return form.submit;
    return form.controls[selector.match(/^\[name="([^"]+)"\]$/)?.[1]] || null;
  };
  return form;
}
function switchTo(form,id){form.controls.provider.value=id;return ui.handleChange(form.controls.provider);}
function change(form,values){for(const [name,value] of Object.entries(values)){
  assert.ok(form.controls[name],`control ${name} exists`);
  if(form.controls[name].type === "checkbox")form.controls[name].checked=value;else form.controls[name].value=String(value);
}}
function lastSave(){return calls.filter(([url])=>url === "/api/settings").at(-1)?.[1].body;}

async function main(){
  let html=await ui.render();
  assert.match(html,/data-form="web-search"/);assert.match(html,/data-form="web-search-test"/);
  assert.match(html,/修改后请先保存/);assert.match(html,/计入每日调用额度/);assert.match(html,/0 停止搜索调用/);
  assert.match(html,/会把本次查询发送给公共搜索站点/);
  assert.match(html,/type="password" value=""/);assert.ok(!html.includes("SERVER_SECRET_MUST_NOT_APPEAR"));
  assert.match(html,/https:\/\/docs\.tavily\.com/);assert.match(html,/剩余 97 次/);
  assert.match(html,/name="query"[^>]*maxlength="300"/);

  const form=formFrom(html);
  change(form,{enabled:false,timeout_seconds:23,max_results:7,daily_calls:0,fallback_public:true,
    api_key:"USER_TAVILY_DRAFT",base_url:"https://custom.example/tavily",clear_api_key:true});
  assert.equal(switchTo(form,"brave"),true);
  assert.equal(form.controls.api_key.value,"");assert.equal(form.controls.clear_api_key.checked,false);
  assert.equal(form.controls.base_url.value,"https://api.search.brave.com/res/v1/web/search");
  assert.equal(form.controls.timeout_seconds.value,"23");assert.equal(form.controls.daily_calls.value,"0");
  assert.equal(form.controls.enabled.checked,false);assert.equal(form.controls.fallback_public.checked,true);
  change(form,{api_key:"USER_BRAVE_DRAFT",base_url:"https://custom.example/brave"});
  switchTo(form,"tavily");
  assert.equal(form.controls.api_key.value,"USER_TAVILY_DRAFT");assert.equal(form.controls.clear_api_key.checked,true);
  assert.equal(form.controls.base_url.value,"https://custom.example/tavily");
  assert.ok(!form.panel.innerHTML.includes("USER_TAVILY_DRAFT"));assert.ok(!form.panel.innerHTML.includes("USER_BRAVE_DRAFT"));
  switchTo(form,"public");assert.equal(form.controls.api_key,undefined);assert.equal(form.controls.base_url,undefined);
  switchTo(form,"brave");assert.equal(form.controls.api_key.value,"USER_BRAVE_DRAFT");
  await ui.handleSubmit("web-search",form);
  assert.deepEqual(lastSave(),{search:{enabled:false,provider:"brave",timeout_seconds:23,max_results:7,daily_calls:0,fallback_public:true},
    search_providers:[{id:"brave",base_url:"https://custom.example/brave",api_key:"USER_BRAVE_DRAFT"}]});
  assert.equal(form.controls.api_key.value,"");assert.equal(bootstraps,1);
  assert.ok(!JSON.stringify(lastSave()).includes("SERVER_SECRET"));

  const blank=formFrom(html);await ui.handleSubmit("web-search",blank);
  assert.deepEqual(lastSave().search_providers,[{id:"tavily",base_url:"https://api.tavily.com/search"}]);
  change(blank,{clear_api_key:true});await ui.handleSubmit("web-search",blank);
  assert.deepEqual(lastSave().search_providers,[{id:"tavily",base_url:"https://api.tavily.com/search",clear_api_key:true}]);
  change(blank,{api_key:"CONFLICT_SECRET"});
  const beforeConflict=calls.length;
  await assert.rejects(ui.handleSubmit("web-search",blank),/不能同时选择/);assert.equal(calls.length,beforeConflict);
  switchTo(blank,"public");await ui.handleSubmit("web-search",blank);assert.ok(!("search_providers" in lastSave()));

  for(const [name,value] of [["timeout_seconds",0],["timeout_seconds",61],["max_results",1.5],["max_results",11],["daily_calls",-1],["daily_calls",100001],["daily_calls",""]]){
    const invalid=formFrom(html);change(invalid,{[name]:value});const count=calls.length;
    await assert.rejects(ui.handleSubmit("web-search",invalid));assert.equal(calls.length,count);
  }
  for(const url of ["http://api.example/search","javascript:alert(1)","https://user:password@api.example/search","https://api.example/search?api_key=secret","https://api.example/search#secret"]){
    const invalid=formFrom(html);change(invalid,{base_url:url});const count=calls.length;
    await assert.rejects(ui.handleSubmit("web-search",invalid),/完整 HTTPS/);assert.equal(calls.length,count);
  }

  const refresh=formFrom(html),button={disabled:false,closest:()=>refresh.container};
  change(refresh,{api_key:"REFRESH_DRAFT",daily_calls:333});
  await ui.handleAction("web-search-status",button);
  assert.equal(button.disabled,false);assert.equal(refresh.controls.api_key.value,"REFRESH_DRAFT");assert.equal(refresh.controls.daily_calls.value,"333");

  const test=formFrom(html,"web-search-test",refresh.container);change(test,{query:"  ordinary query  "});
  testResult={channel:"public",text:"UNTRUSTED_RAW_TEXT",links:["javascript:alert(1)"],results:[
    {title:'<img src=x onerror="alert(1)">',url:'https://example.org/page?a=1&b=%22',content:"<script>bad()</script>"},
    ...["javascript:alert(1)","data:text/html,evil","/relative","https://user:password@host.example/","https://evil.example/\nattack"].map(url=>({title:"UNSAFE",url,content:"unsafe"}))
  ]};
  await ui.handleSubmit("web-search-test",test);
  const request=calls.filter(([url])=>url === "/api/search/test").at(-1);
  assert.deepEqual(request,["/api/search/test",{method:"POST",body:{query:"ordinary query"}}]);
  const results=test.container.results.innerHTML;
  assert.match(results,/&lt;img/);assert.match(results,/&lt;script/);assert.ok(!results.includes("<script>"));
  assert.ok(!results.includes("javascript:"));assert.ok(!results.includes("UNSAFE"));assert.ok(!results.includes("UNTRUSTED_RAW_TEXT"));
  assert.match(results,/href="https:\/\/example\.org\/page\?a=1&amp;b=%22"/);
  assert.match(results,/rel="noopener noreferrer"/);assert.match(results,/实际搜索渠道/);
  assert.equal(refresh.controls.api_key.value,"REFRESH_DRAFT");assert.equal(refresh.controls.daily_calls.value,"333");
  for(const [channel,label] of [["bing_html","Bing 网页"],["bing_rss","Bing RSS"],["duckduckgo_html","DuckDuckGo 网页"]]){
    testResult={channel,failures:[{channel:"tavily",status:"unavailable"}],results:[{title:"Real source",url:"https://example.org/",content:"source excerpt"}]};
    await ui.handleSubmit("web-search-test",test);
    assert.ok(test.container.results.innerHTML.includes(label));assert.ok(!test.container.results.innerHTML.includes("未提供"));
  }

  apiError={url:"/api/search/test",message:"PRIVATE_TOKEN https://private.invalid Traceback"};
  await assert.rejects(ui.handleSubmit("web-search-test",test),error=>!/PRIVATE_TOKEN|private\.invalid|Traceback/.test(error.message));
  assert.ok(!/PRIVATE_TOKEN|private\.invalid|Traceback/.test(test.container.results.textContent));assert.equal(test.submit.disabled,false);
  for(const [code,expected] of [["daily_limit",/额度已用完/],["not_configured",/独立 API 密钥/],["disabled",/已停用/],
    ["configuration_changed",/发生变化/],["queue_full",/排队已满/],["timeout",/等待上限/],["invalid_response",/兼容的公开网页结果/],
    ["http_error",/供应商未接受/],["provider_unavailable",/搜索服务暂不可用/]]){
    apiError={url:"/api/search/test",code,message:"PRIVATE_TOKEN https://private.invalid Traceback"};
    await assert.rejects(ui.handleSubmit("web-search-test",test),expected);
    assert.match(test.container.results.textContent,expected);assert.ok(!test.container.results.textContent.includes("PRIVATE_TOKEN"));
  }
  apiError={url:"/api/search/test",code:"constructor",message:"PRIVATE_TOKEN"};
  await assert.rejects(ui.handleSubmit("web-search-test",test),/搜索测试未完成/);
  apiError={url:"/api/settings",message:"PRIVATE_SAVE_TOKEN"};
  await assert.rejects(ui.handleSubmit("web-search",formFrom(html)),error=>!error.message.includes("PRIVATE_SAVE_TOKEN"));
  apiError={url:"/api/search/status",message:"PRIVATE_STATUS_TOKEN"};
  const statusFailure=await ui.render();assert.match(statusFailure,/用量状态暂不可用/);assert.ok(!statusFailure.includes("PRIVATE_STATUS_TOKEN"));apiError=null;
  statusResult={enabled:true,provider:"tavily",used:9,daily_calls:0,remaining:0};
  const zero=await ui.render();assert.match(zero,/今日已用 <strong>9<\/strong>/);assert.match(zero,/今日额度设为 0/);statusResult=null;
  change(test,{query:"x".repeat(301)});const beforeLongPublic=calls.length;
  await assert.rejects(ui.handleSubmit("web-search-test",test),/300/);assert.equal(calls.length,beforeLongPublic);

  sandbox.state.settings.search.provider="deepseek";
  const deep=await ui.render();assert.match(deep,/128 个字符/);assert.match(deep,/https:\/\/api-docs\.deepseek\.com/);
  assert.match(deep,/name="query"[^>]*maxlength="128"/);
  const deepTest=formFrom(deep,"web-search-test");change(deepTest,{query:"😀".repeat(129)});const beforeLong=calls.length;
  await assert.rejects(ui.handleSubmit("web-search-test",deepTest),/128/);assert.equal(calls.length,beforeLong);
  change(deepTest,{query:"😀".repeat(128)});testResult={channel:"deepseek",results:[]};await ui.handleSubmit("web-search-test",deepTest);
  assert.match(deepTest.container.results.innerHTML,/没有返回可显示/);

  sandbox.state.settings={};html=await ui.render();const fresh=formFrom(html);assert.equal(fresh.controls.provider.value,"public");
  switchTo(fresh,"tavily");assert.equal(fresh.controls.base_url.value,"https://api.tavily.com/search");
  bootstrapError="PRIVATE_BOOTSTRAP_TOKEN";
  await assert.rejects(ui.handleSubmit("web-search",fresh),error=>error.message.includes("已保存") && !error.message.includes("PRIVATE_BOOTSTRAP_TOKEN"));
  bootstrapError=null;
  assert.equal(await ui.handleSubmit("unrelated",{}),false);assert.equal(await ui.handleAction("unrelated",{}),false);
  assert.equal(ui.handleChange({name:"other",closest:()=>fresh}),false);
  console.log("Web search settings, independent key drafts, saved-only testing, quota bounds and safe source links verified");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
