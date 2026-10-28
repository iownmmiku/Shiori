"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const modal = {open:false,classList:{remove() {}},showModal() {this.open = true;}};
const content = {innerHTML:""};
let activeForm = null;
const calls = [];
let responder = async () => ({}), quickModelCallback = null;
const sandbox = {
  window:{ShioriPages:{startQuickModel:async callback => {quickModelCallback=callback;}},ShioriExperience:{handleAction:async action => calls.push(["experience",action])}},
  state:{settings:{identity:{name:"原称呼",aliases:["旧别名"],planner_enabled:false},model:{base_url:"https://example.test/v1",model:"existing"}},boot:{setup_required:false}},
  location:{protocol:"http:",host:"127.0.0.1:18765"},
  navigator:{clipboard:{writeText:async value => calls.push(["clipboard",value])}},
  sessionStorage:{setItem() {}},
  e:value => String(value ?? "").replaceAll("<","&lt;"),
  button:(text,action) => `<button type="button" data-action="${action}">${text}</button>`,
  field:(name,title,value,type) => `<input name="${name}" type="${type}" value="${type === "password" ? "" : value}">`,
  textarea:(name,title,value) => `<textarea name="${name}">${value}</textarea>`,
  toggle:(name,title,hint,checked) => `<input name="${name}" type="checkbox" ${checked ? "checked" : ""}>`,
  select:(name,title,options,value) => `<select name="${name}">${options.map(([id,label])=>`<option value="${id}" ${id===value ? "selected" : ""}>${label}</option>`).join("")}</select>`,
  icon:() => "",notice:text => `<p>${text}</p>`,settingsModelForm:() => "<input name=model>",
  fmt:value => value,diagnosticCard:() => "",toast:() => {},
  $:selector => selector === "#modal" ? modal : selector === "#modal-content" ? content : selector === '[data-form="quick-start"]' ? activeForm : null,
  getForm:form => form.values,
  api:async (...args) => {calls.push(args); return responder(...args);},
  bootstrap:async () => {},navigate:async page => calls.push(["navigate",page]),closeModal:() => {modal.open=false;},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static/quick-start.js"),"utf8"),sandbox);
const quick = sandbox.window.ShioriQuickStart;
const plain = value => JSON.parse(JSON.stringify(value));

async function main() {
  const before = JSON.stringify(sandbox.state.settings);
  assert.deepEqual(plain(quick.buildPatch(0,{name:" 初音未来 ",aliases:"初音,Miku，初音"},sandbox.state.settings)),{identity:{name:"初音未来",aliases:["初音","Miku"]}});
  assert.equal(JSON.stringify(sandbox.state.settings),before,"identity patch must not alter existing settings");

  const settings = {routing:{chat:"custom",memory:"other"},providers:[{id:"custom",name:"聊天",enabled:true,model:"old",api_key_set:true,temperature:.5},{id:"other",model:"keep",enabled:false,api_key_set:true}]};
  assert.equal(quick.modelSelection(settings).provider_id,"custom");
  const patch = plain(quick.buildPatch(1,{base_url:" https://new.test/v1 ",model:"new",api_key:"",vision:false,model_enabled:true},settings,"custom"));
  assert.equal(patch.providers[0].model,"new");
  assert.equal(patch.providers[0].temperature,.5,"advanced model choices survive quick setup");
  assert.deepEqual(patch.providers[1],{id:"other",model:"keep",enabled:false});
  assert.ok(!("routing" in patch),"quick setup must not reset task routes");
  assert.ok(patch.providers.every(item => !("api_key" in item) && !("api_key_set" in item)),"blank keys retain server credentials instead of replacing them");
  assert.equal(settings.providers[0].model,"old");
  assert.throws(() => quick.buildPatch(1,{},settings,"removed"),/重新打开/);
  const defaultPatch = plain(quick.buildPatch(1,{base_url:"http://localhost/v1",model:"local",vision:true},settings));
  assert.deepEqual(defaultPatch,{model:{base_url:"http://localhost/v1",model:"local",vision:true}});
  const sourceSettings={routing:{chat:"source-model",memory:"keep-memory"},provider_sources:[{id:"source",name:"我的供应商",base_url:"https://source.test/v1",enabled:true},{id:"off-source",base_url:"https://off.test",enabled:false}],providers:[{id:"source-model",source_id:"source",name:"聊天模型",model:"chat-1",base_url:"",enabled:true},{id:"off",source_id:"off-source",model:"off-1",enabled:true},{id:"disabled",model:"off-2",base_url:"https://disabled.test",enabled:false}]};
  assert.equal(quick.modelSelection(sourceSettings).base_url,"https://source.test/v1","quick setup resolves the provider connection without editing it");
  assert.deepEqual(plain(quick.modelOptions(sourceSettings)),[["source-model","我的供应商 · 聊天模型"]]);
  const withEmbedding={...sourceSettings,providers:[...sourceSettings.providers,{...sourceSettings.providers[0],id:"embedding-only",model:"embedding-fixture",model_type:"embedding"}]};
  assert.deepEqual(plain(quick.modelOptions(withEmbedding)),[["source-model","我的供应商 · 聊天模型"]],"embedding models never appear in quick-start chat selection");
  assert.deepEqual(plain(quick.buildPatch(1,{chat_model_id:"source-model"},sourceSettings)),{routing:{chat:"source-model"}},"choosing an existing model changes only the chat route");
  assert.deepEqual(plain(quick.buildPatch(1,{chat_model_id:"default"},sandbox.state.settings)),{routing:{chat:""}},"the original default model uses an empty routing value, not a provider ID");
  assert.throws(()=>quick.buildPatch(1,{chat_model_id:"off"},sourceSettings),/已启用/);

  const napcat = plain(quick.buildPatch(2,{adapter:"napcat",qq_enabled:true,ws_url:"",owner_ids:"123 123",allowed_groups:"456，789",app_id:"do-not-write",app_secret:"hidden"},{}));
  assert.deepEqual(napcat,{onebot:{chat_policy:"blacklist",adapter:"napcat",enabled:true,ws_url:"",allowed_users:["123"],allowed_groups:["456","789"]},qq_official:{enabled:false}});
  const official = plain(quick.buildPatch(2,{adapter:"official",qq_enabled:false,app_id:"app",official_groups:"group-openid",official_users:"user-openid",access_token:"do-not-write"},{}));
  assert.deepEqual(official,{onebot:{adapter:"official",enabled:false},qq_official:{chat_policy:"blacklist",enabled:false,app_id:"app",allowed_groups:["group-openid"],allowed_users:["user-openid"]}});
  assert.equal(quick.buildPatch(2,{adapter:"snowluma",access_token:"explicit-key"},{}).onebot.access_token,"explicit-key");

  quick.open();
  assert.match(content.innerHTML,/原称呼/);
  assert.match(content.innerHTML,/先聊起来，其他随时再设置/);
  assert.ok(!calls.length,"opening setup must never save, probe, or overwrite configuration");
  activeForm = {values:{name:"unsaved-name",aliases:"unsaved"}};
  await quick.handleAction("quick-skip",{});
  assert.match(content.innerHTML,/连接你自己的模型/);
  assert.match(content.innerHTML,/添加供应商并选择模型/);
  assert.match(content.innerHTML,/chat_model_id/);
  await quick.handleAction("quick-model-source",{});
  assert.equal(typeof quickModelCallback,"function","provider onboarding can return directly to the QQ step");
  activeForm = {values:{base_url:"",model:"",api_key:"unsaved-secret"}};
  await quick.handleAction("quick-skip",{});
  assert.match(content.innerHTML,/独立安装 NapCat/);
  assert.match(content.innerHTML,/https:\/\/napneko.github.io\//);
  assert.match(content.innerHTML,/保存并自动配对/);
  assert.match(content.innerHTML,/连接器在另一台电脑/);
  assert.match(content.innerHTML,/平台已确认回复送达/);
  assert.ok(!calls.length,"skipping steps must not make an API request or save drafts");
  activeForm = null;
  await quick.handleAction("quick-copy-address",{});
  assert.deepEqual(calls.pop(),["clipboard","ws://127.0.0.1:18765/onebot/v11/ws"],"copy helper must only copy the connection address");
  await quick.handleAction("quick-finish",{});
  assert.deepEqual(calls.pop(),["navigate","chat"]);
  assert.equal(JSON.stringify(sandbox.state.settings),before,"skipped drafts must never overwrite existing settings");

  calls.length=0;
  await assert.rejects(quick.handleAction("quick-local-qq",{closest:()=>({values:{adapter:"napcat",chat_policy:"whitelist",owner_ids:"",allowed_groups:""}})}),/至少一个/);
  assert.equal(calls.length,0,"empty allowed lists do not silently enable a connector");
  responder=async()=>({onebot:{allowed_users:["old"],access_token_set:true}});
  const pairButton={disabled:false,closest:()=>({values:{adapter:"snowluma",owner_ids:"123 123",allowed_groups:"456",ws_url:"ws://old-forward.test",qq_enabled:false}})};
  await quick.handleAction("quick-local-qq",pairButton);
  assert.equal(pairButton.disabled,false);
  assert.equal(calls[0][0],"/api/settings");
  assert.deepEqual(plain(calls[1][1].body),{onebot:{chat_policy:"blacklist",adapter:"snowluma",allowed_users:["123"],allowed_groups:["456"]},qq_official:{enabled:false}},"automatic pairing saves admission without enabling a token-less connector or disturbing an old forward connection");
  assert.deepEqual(calls.at(-1),["experience","local-qq"],"the connector form opens after the list has been saved");

  calls.length=0;
  await quick.handleAction("quick-local-qq",{disabled:false,closest:()=>({values:{adapter:"napcat",chat_policy:"blacklist",owner_ids:"",allowed_groups:""}})});
  assert.equal(calls[1][1].body.onebot.chat_policy,"blacklist");
  assert.ok(!("enabled" in calls[1][1].body.onebot),"pairing must not enable before its generated token is saved");
  assert.deepEqual(calls.at(-1),["experience","local-qq"],"blacklist accepts empty lists");

  const tips = plain(quick.recommendations({paused:true,model:{configured:false},budget:{exhausted:true},qq:{enabled:true,adapter:"official",allowed_groups:0,allowed_users:0,status:{connected:false}}}));
  assert.equal(tips.length,5);
  assert.ok(tips.some(item => item.text.includes("凭据验证通过仍需等待实际连接")));
  assert.equal(quick.recommendations({model:{configured:true},qq:{enabled:true,allowed_groups:1,status:{connected:true}}}).length,0);
  assert.match(quick.diagnosticsPanel({checks:[],model:{configured:true},qq:{enabled:true,allowed_groups:1,status:{connected:true}}}),/模型是否可用需单独测试/);

  const app = fs.readFileSync(path.join(__dirname,"../shiori/static/ui-core.js"),"utf8");
  assert.match(app,/function button\([^\n]+<button type="button"/);
  console.log("PASS: quick setup preserves settings, skips without writes, uses selected QQ adapter, copies no credentials, and reports local facts.");
}
main().catch(error => {console.error(error); process.exitCode=1;});
