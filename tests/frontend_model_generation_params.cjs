"use strict";
// Regression checks for PR #8 review findings 4 and 5 (frontend):
// CSP-safe range/number pairing, one shared advanced-parameter component,
// a single temperature input in the catalog modal and shared JSON validation.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const root=path.join(__dirname,"../shiori/static");
const read=name=>fs.readFileSync(path.join(root,name),"utf8");
const escape=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const calls=[];let modal=null;
const elements=new Map();
const responder=async(url,options)=>{
  calls.push([url,options]);
  if(url==="/api/model-providers")return{providers:[{id:"p1",name:"Provider",base_url:"https://models.example/v1",api_key_set:true,enabled:true}],models:[{id:"m1",provider_id:"p1",name:"Chat",model:"chat-a",enabled:true,effective_enabled:true,tools:true,vision:true,custom_params:{note:"</textarea><script>alert(1)</script>"}}],routing:{chat:"m1"}};
  return {};
};
const sandbox={
  window:{},URLSearchParams,AbortController,
  state:{page:"settings",scope:"web:test",scopes:[{id:"web:test"}],settings:{},authenticated:true,sending:false},
  document:{hidden:false,getElementById:id=>elements.get(id)||null},
  e:escape,icon:()=>"",label:key=>key,fieldId:0,
  setTimeout:()=>0,clearTimeout:()=>{},
  $:()=>null,$$:()=>[],
  api:async(url,options)=>responder(url,options),query:params=>new URLSearchParams(params).toString(),
  heading:(a,b,c,d="")=>a+b+c+d,tag:text=>`<small>${escape(text)}</small>`,notice:text=>`<p>${escape(text)}</p>`,
  empty:(a,b,c,d,f)=>b+c,number:value=>String(value||0),fmt:value=>String(value),
  bootstrap:async()=>{},navigate:async()=>{},confirmAction:async()=>{},
  navigator:{clipboard:{writeText:async()=>{}}},
};
vm.createContext(sandbox);
// Load order matters: the shared helpers live in ui-core.js, the shared component
// in settings-page.js and the catalog editor consumes both in workspace-pages.js.
for(const name of ["ui-core.js","settings-page.js","workspace-pages.js"])vm.runInContext(read(name),sandbox,{filename:name});
sandbox.getForm=form=>form.values;
sandbox.openModal=(...args)=>{modal=args;};
sandbox.closeModal=()=>{modal=null;};
sandbox.toast=()=>{};
const pages=sandbox.window.ShioriPages;
const plain=value=>JSON.parse(JSON.stringify(value));
const count=(text,needle)=>text.split(needle).length-1;
const inputs=html=>[...html.matchAll(/<input\b[^>]*>/g)].map(match=>match[0]);
const inputNamed=(html,name)=>inputs(html).find(tag=>tag.includes(`name="${name}"`));
const rangePairedWith=(html,name)=>inputs(html).find(tag=>/type="range"/.test(tag)&&new RegExp(`data-sync-target="[^"]*${name}`).test(tag));
const assertNoInlineHandlers=html=>assert.doesNotMatch(html,/\son(?:input|change|click|submit|keydown|keyup)\s*=/i);
const maxTokensHelp="0 表示不额外限制；设置后聊天按该额度发送，其他任务最多使用该上限。";

async function main(){
  // 1. Generated settings markup: no inline handlers, one of each named control,
  //    unified max_tokens range/number bounds and a default of 0.
  const settingsHtml=sandbox.settingsModelForm({});
  assertNoInlineHandlers(settingsHtml);
  for(const name of ["temperature","top_p","max_tokens","reasoning_effort","custom_params"])assert.equal(count(settingsHtml,`name="${name}"`),1,`settings form renders exactly one ${name} input`);
  assert.match(settingsHtml,/对话温度/);
  assert.match(settingsHtml,/Top-p/);
  assert.match(settingsHtml,/最大 Tokens/);
  assert.match(settingsHtml,/思考强度/);
  assert.match(settingsHtml,/自定义参数/);
  assert.ok(settingsHtml.includes(maxTokensHelp),"max_tokens help explains 0 and the request ceiling in Chinese");
  const settingsMaxNumber=inputNamed(settingsHtml,"max_tokens"),settingsMaxRange=rangePairedWith(settingsHtml,"max_tokens");
  assert.ok(settingsMaxNumber&&settingsMaxRange,"max_tokens renders a named number input paired with a range");
  assert.ok(settingsMaxNumber.includes('min="0"')&&settingsMaxNumber.includes('max="100000"'),"max_tokens number is bounded 0..100000");
  assert.ok(settingsMaxNumber.includes('value="0"'),"settings form defaults max_tokens to 0, not 8192");
  assert.ok(settingsMaxRange.includes('min="0"')&&settingsMaxRange.includes('max="100000"'),"max_tokens range is bounded 0..100000");
  assert.ok(!/\sname=/.test(settingsMaxRange),"ranges stay out of the form so the number input is the submitted value");
  assert.ok(settingsMaxRange.includes('aria-label="最大 Tokens"'),"ranges keep an accessible Chinese label");
  assert.match(settingsMaxNumber,/data-sync-source="[^"]*max_tokens-range"/);
  assert.ok(settingsMaxRange.includes('data-sync-target="')&&settingsMaxRange.includes("max_tokens-number"),"range points at its number twin");
  assert.ok(rangePairedWith(settingsHtml,"temperature")?.includes('max="2"'),"temperature stays 0..2");
  assert.ok(rangePairedWith(settingsHtml,"top_p")?.includes('max="1"'),"top_p stays 0..1 only once");

  // 2. The custom-params textarea escapes dangerous content instead of closing early.
  const escaped=sandbox.settingsModelForm({custom_params:{note:"</textarea><script>alert(1)</script>"}});
  assert.ok(!escaped.includes("</textarea><script>"),"custom params cannot escape the textarea element");
  assert.ok(escaped.includes("&lt;/textarea&gt;"),"custom params are HTML-escaped");
  assert.match(escaped,/<textarea[^>]*name="custom_params"/);

  // 3. The shared component is reused by the catalog modal: exactly one
  //    temperature, Chinese labels, no old English duplicate controls.
  await pages.loadCatalog();
  await pages.handleAction("edit-catalog-model",{dataset:{id:"m1"}});
  assert.ok(modal,"catalog editor opens a modal");
  const catalogHtml=modal[2];
  assertNoInlineHandlers(catalogHtml);
  for(const name of ["temperature","top_p","max_tokens","reasoning_effort","custom_params"])assert.equal(count(catalogHtml,`name="${name}"`),1,`catalog modal renders exactly one ${name} input`);
  assert.equal(count(catalogHtml,'name="temperature"'),1,"the old duplicate temperature field is gone so FormData cannot shadow the slider");
  assert.match(catalogHtml,/对话温度/);assert.match(catalogHtml,/最大 Tokens/);assert.match(catalogHtml,/思考强度/);assert.match(catalogHtml,/自定义参数/);
  assert.ok(catalogHtml.includes(maxTokensHelp),"catalog modal reuses the same max_tokens help");
  assert.ok(!catalogHtml.includes("Custom parameters"),"old English custom-params textarea is gone");
  assert.ok(!catalogHtml.includes("Reasoning Effort"),"old English reasoning select is gone");
  assert.ok(!catalogHtml.includes("Max Tokens</label>"),"old English max-tokens label is gone");
  assert.match(catalogHtml,/name="timeout_seconds"/,"timeout stays editable in the catalog modal");
  assert.match(catalogHtml,/name="vision"/);
  assert.match(catalogHtml,/name="tools"/);
  assert.equal(inputNamed(catalogHtml,"max_tokens")?.includes('value="0"'),true,"catalog editor defaults an unset max_tokens to 0");
  const catalogMaxNumber=inputNamed(catalogHtml,"max_tokens"),catalogMaxRange=rangePairedWith(catalogHtml,"max_tokens");
  assert.ok(catalogMaxNumber.includes('min="0"')&&catalogMaxNumber.includes('max="100000"'),"catalog max_tokens number is bounded 0..100000");
  assert.ok(catalogMaxRange.includes('min="0"')&&catalogMaxRange.includes('max="100000"'),"catalog max_tokens range is bounded 0..100000");
  assert.ok(catalogHtml.includes('<option value="" selected>未指定</option>'),"reasoning effort defaults to 未指定");
  assert.ok(!catalogHtml.includes("</textarea><script>"),"catalog custom params cannot escape the textarea element");
  assert.ok(catalogHtml.includes("&lt;/textarea&gt;"),"catalog custom params are HTML-escaped");

  // 4. syncRangePair keeps range and number in sync in both directions.
  const numberTwin={value:"0"},rangeTwin={value:"0"};
  elements.set("fixture-number",numberTwin);elements.set("fixture-range",rangeTwin);
  sandbox.syncRangePair({value:"1.3",dataset:{syncTarget:"fixture-number"}});
  assert.equal(numberTwin.value,"1.3","moving the range updates the submitted number");
  sandbox.syncRangePair({value:"0.4",dataset:{syncSource:"fixture-range"}});
  assert.equal(rangeTwin.value,"0.4","typing a number moves the range");
  assert.doesNotThrow(()=>sandbox.syncRangePair({value:"x",dataset:{}}),"inputs without sync metadata are ignored");
  assert.doesNotThrow(()=>sandbox.syncRangePair({value:"x",dataset:{syncTarget:"missing"}}),"missing targets are ignored");
  assert.match(read("app.js"),/syncRangePair\(event\.target\)/,"the existing delegated input listener drives the sync");
  assert.match(read("ui-core.js"),/function syncRangePair\(/,"syncRangePair lives in the shared ui-core helpers");

  // 5. Shared parseCustomParams rules used by the settings model payload.
  const appSource=read("app.js"),payloadStart=appSource.indexOf("function modelPayload(");
  vm.runInContext(appSource.slice(payloadStart,appSource.indexOf("\n",payloadStart)),sandbox,{filename:"app-model-payload.js"});
  const payload=sandbox.modelPayload({values:{base_url:"https://api.example/v1",model:"chat-a",temperature:"0.4",top_p:"0.9",max_tokens:"8192",reasoning_effort:" high ",custom_params:'{"seed":7}'}});
  assert.equal(payload.temperature,0.4);assert.equal(payload.top_p,0.9);assert.equal(payload.max_tokens,8192);
  assert.equal(payload.reasoning_effort,"high","reasoning effort is trimmed text");
  assert.deepEqual(plain(payload.custom_params),{seed:7},"valid custom params become objects");
  assert.deepEqual(plain(sandbox.modelPayload({values:{base_url:"u",model:"m",custom_params:""}}).custom_params),{},"empty custom params mean no extras");
  for(const invalid of ['[1,2]','"text"','42','null','{bad json']){
    assert.throws(()=>sandbox.modelPayload({values:{base_url:"u",model:"m",custom_params:invalid}}),/自定义参数必须是有效的 JSON 对象。/,`rejects ${invalid}`);
  }
  assert.match(read("app.js"),/parseCustomParams\(data\.custom_params\)/,"settings payload uses the shared parser");
  for (const value of ['{"seed":1e309}', '{"seed":-1e309}']) assert.throws(() => sandbox.parseCustomParams(value), /有限数字/);

  // 6. Catalog submission: invalid custom params are reported in Chinese before
  //    any request; valid values are mapped numerically and as an object.
  const invalidValues={id:"m1",name:"Chat",temperature:"0.5",input_price:"1",output_price:"2",vision:true,tools:false,top_p:"0.9",max_tokens:"8192",reasoning_effort:"high",custom_params:'[1,2]'};
  const beforeInvalid=calls.length;
  await assert.rejects(pages.handleSubmit("catalog-model",{values:invalidValues}),/自定义参数必须是有效的 JSON 对象。/);
  await assert.rejects(pages.handleSubmit("catalog-model",{values:{...invalidValues,custom_params:'{"a":1'}}),/自定义参数必须是有效的 JSON 对象。/,"broken JSON is rejected too");
  assert.equal(calls.length,beforeInvalid,"invalid catalog custom params never reach the API");
  assert.doesNotMatch(read("workspace-pages.js"),/Custom parameters must be valid JSON/,"the English catalog error is gone");
  assert.match(read("workspace-pages.js"),/parseCustomParams\(data\.custom_params\)/,"catalog submit uses the shared parser");

  await pages.handleSubmit("catalog-model",{values:{...invalidValues,timeout_seconds:"240",custom_params:'{"seed":7}'}});
  const [url,options]=calls.at(-1);
  assert.equal(url,"/api/model-catalog/models/m1");
  assert.equal(options.method,"PATCH");
  const body=plain(options.body);
  assert.equal(body.top_p,0.9,"top_p is sent as a number");
  assert.equal(body.max_tokens,8192,"max_tokens is sent as a number");
  assert.equal(body.reasoning_effort,"high","reasoning_effort is sent as text");
  assert.deepEqual(body.custom_params,{seed:7},"custom_params is sent as an object");
  assert.equal(body.temperature,0.5);assert.equal(body.timeout_seconds,240,"timeout stays editable");
  assert.equal(body.vision,true);assert.equal(body.tools,false);

  // 7. No changed frontend source keeps an inline event handler, and the CSS
  //    for the paired control is present.
  for(const name of ["settings-page.js","workspace-pages.js","ui-core.js","app.js"]){
    assert.doesNotMatch(read(name),/\son(?:input|change|click|submit|keydown|keyup)\s*=/i,`${name} must not add inline event handlers`);
  }
  assert.match(read("workspace-pages.css"),/\.range-with-value\{/,"range-with-value layout is styled");
  assert.match(read("workspace-pages.css"),/grid-template-columns/);
  console.log("generation-parameter sliders, shared catalog fields, CSP-safe sync and custom-param validation checks passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
