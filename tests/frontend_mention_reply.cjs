"use strict";
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = name => fs.readFileSync(path.join(__dirname, '../shiori/static', name), 'utf8');
const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const decode = value => value.replace(/&(amp|lt|gt|quot|#39);/g, (_, key) => ({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'"}[key]));
const attr = (html, name) => decode(html.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] ?? '');

// Minimal form adapter: consume the real rendered controls, then use the real
// getForm and save handlers. It contains no reply-mode or inheritance logic.
function formFrom(html) {
  const controls = [];
  for (const match of html.matchAll(/<input\b[^>]*>|<select\b[^>]*>[\s\S]*?<\/select>/g)) {
    const markup = match[0], name = attr(markup, 'name');
    if (!name) continue;
    const control = {name, type:attr(markup,'type'), value:attr(markup,'value'), checked:/\schecked(?:\s|>)/.test(markup)};
    if (markup.startsWith('<select')) {
      control.options = [...markup.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map(option => ({value:attr(option[1], 'value'), label:decode(option[2]), selected:/\bselected\b/.test(option[1])}));
      control.value = (control.options.find(option => option.selected) || control.options[0])?.value ?? '';
    }
    controls.push(control);
  }
  return {controls, get(name) { const control=controls.find(item=>item.name===name); assert.ok(control, `missing field ${name}`); return control; },
    choose(name,label) { const control=this.get(name),option=control.options.find(item=>item.label===label); assert.ok(option, `missing choice ${label}`); control.value=option.value; }};
}
class FormDataAdapter {
  constructor(form) { this.form=form; }
  *[Symbol.iterator]() { for (const control of this.form.controls) if(control.type!=='checkbox'||control.checked) yield [control.name,control.value]; }
}

const calls=[];
let modalHTML='';
const body={insertAdjacentHTML(position,html){modalHTML=position==='afterbegin'?html+modalHTML:modalHTML+html;}};
const state={settings:{identity:{},onebot:{chat_policy:'blacklist'},output:{}},scopes:[],personas:[{id:'default',name:'默认人格'}],filters:{},botProfiles:{default_id:'main',items:[{id:'main',name:'默认配置',values:{}}]}};
const sandbox={window:{},state,fieldId:0,e:escape,label:value=>value,icon:()=>'',FormData:FormDataAdapter,rows:value=>Array.isArray(value)?value:value.items||[],
  $:selector=>selector==='#modal .modal-body'?body:null,
  $$:(selector,form)=>selector==='input[type="checkbox"]'?form.controls.filter(item=>item.type==='checkbox'):[],
  api:async(url,options)=>{calls.push([url,options]);if(url==='/api/scopes'&&!options)return state.scopes;if(url==='/api/bot-profiles'&&!options)return state.botProfiles;return {};},
  bootstrap:async()=>{},splitNames:value=>String(value||'').split(/[，,\n]/).filter(Boolean)};
vm.createContext(sandbox);
for(const name of ['ui-core.js','workspace-features.js','settings-page.js','experience-persona.js'])vm.runInContext(source(name),sandbox,{filename:name});
sandbox.openModal=(title,description,html,kind,record)=>{modalHTML=html;state.modalRecord=record;};
sandbox.closeModal=()=>{};sandbox.toast=()=>{};
for(const name of ['Chat','Jobs','Config','Help'])sandbox.window['ShioriExperience'+name]={create:()=>({})};
vm.runInContext(source('experience.js'),sandbox,{filename:'experience.js'});
const app=source('app.js');
for(const [start,end] of [['function scopeModalBase(', 'function showWizardModal('], ['async function saveModal(', 'function modelPayload(']])
  vm.runInContext(app.slice(app.indexOf(start),app.indexOf(end)),sandbox,{filename:'app-mention-save.js'});
const ui=sandbox.window.ShioriFeatures, experience=sandbox.window.ShioriExperience;
const profile=sandbox.window.ShioriExperiencePersona.create({},{});
const payload=()=>JSON.parse(JSON.stringify(calls.at(-1)[1].body));

async function main(){
  // Global checkboxes keep the old payload keys and false survives an edit/save/reopen.
  let html=ui.identitySection(),form=formFrom(html);
  assert.match(html,/aria-label="被 @ 时直接回复"/);
  assert.match(html,/aria-label="提及名称时直接回复"/);
  assert.equal(form.get('respond_to_at').checked,true);
  form.get('respond_to_at').checked=false;
  form.get('respond_to_name').checked=false;
  form.get('respond_to_reply').checked=false;
  await sandbox.saveSettings('identity',form);
  assert.equal(calls.at(-1)[0],'/api/settings');
  assert.deepEqual(payload().identity,{name:'初音未来',aliases:[],respond_to_at:false,respond_to_reply:false,respond_to_name:false,planner_enabled:true,proactive_enabled:true});
  state.settings.identity=payload().identity;
  form=formFrom(ui.identitySection());
  assert.equal(form.get('respond_to_at').checked,false);
  assert.equal(form.get('respond_to_name').checked,false);

  // Exercise every choice by its displayed meaning, then reopen saved values.
  for(const [choice,expected] of [['直接回复',true],['由 Planner 判断',false],['继承全局',null]]){
    ui.scopeModal({id:'qq:group:1',name:'测试群',trigger_at:false,trigger_name:false,trigger_reply:false});
    form=formFrom(modalHTML);
    assert.equal(form.get('trigger_at').value,'false');
    assert.equal(form.get('trigger_name').value,'false');
    assert.equal(form.get('trigger_reply').options.find(option=>option.value==='false').label,'本会话关闭');
    form.choose('trigger_at',choice);form.choose('trigger_name',choice);
    await sandbox.saveModal('scope',form);
    const saved=payload();
    assert.equal(saved.trigger_at,expected);assert.equal(saved.trigger_name,expected);
    assert.equal(saved.trigger_reply,false,'quoted-reply trigger remains an on/off switch');
    assert.equal(calls.at(-1)[0],'/api/scopes/qq%3Agroup%3A1');
    ui.scopeModal({...saved,id:'qq:group:1'});
    assert.equal(formFrom(modalHTML).get('trigger_at').value,expected==null?'':String(expected));
  }
  const bound={id:'qq:group:2',name:'继承配置群',profile_id:'main',trigger_at:false,trigger_name:false};
  ui.scopeModal(bound);await sandbox.saveModal('scope',formFrom(modalHTML));
  assert.ok(!('trigger_at' in payload())&&!('trigger_name' in payload()),'unchanged inherited values must not become overrides');
  ui.scopeModal(bound);form=formFrom(modalHTML);form.choose('trigger_at','直接回复');form.choose('trigger_name','继承全局');
  await sandbox.saveModal('scope',form);
  assert.equal(payload().trigger_at,true);assert.equal(payload().trigger_name,null);

  // Capture the actual displayed form before editing. Renaming does not turn
  // unchanged switches/default-valued fields into explicit scope overrides.
  const inheritedScope={id:'qq:group:4',name:'旧群名',profile_id:'main',profile_overrides:['dictionary_use'],auto_memory:true,dictionary_learn:true,dictionary_use:false,trigger_at:null};
  ui.scopeModal(inheritedScope);form=formFrom(modalHTML);ui.captureScopeForm(form);
  form.get('name').value='新群名';await sandbox.saveModal('scope',form);
  assert.deepEqual(payload(),{name:'新群名'});
  ui.scopeModal(inheritedScope);form=formFrom(modalHTML);ui.captureScopeForm(form);
  form.get('auto_memory').checked=false;await sandbox.saveModal('scope',form);
  assert.deepEqual(payload(),{auto_memory:false});
  ui.scopeModal(inheritedScope);form=formFrom(modalHTML);ui.captureScopeForm(form);
  form.get('inherit__dictionary_use').checked=true;form.get('auto_memory').checked=false;
  await sandbox.saveModal('scope',form);
  assert.deepEqual(payload(),{auto_memory:false,inherit_fields:['dictionary_use']});
  assert.match(ui.scopePolicyDescription(inheritedScope),/黑名单/);
  assert.match(ui.scopePolicyDescription(inheritedScope),/跨群共享已确认词义/);
  state.settings.onebot.chat_policy='whitelist';state.settings.language={group_isolation:true};
  assert.match(ui.scopePolicyDescription(inheritedScope),/白名单/);
  assert.match(ui.scopePolicyDescription(inheritedScope),/黑话按群隔离/);
  state.settings.onebot.chat_policy='blacklist';state.settings.language={group_isolation:false};

  // Bot profiles offer the same meanings and preserve nullable API compatibility.
  for(const [choice,expected] of [['由 Planner 判断',false],['直接回复',true],['继承全局',null]]){
    state.botProfiles.items[0].values={trigger_at:false,trigger_name:false,trigger_reply:true};
    profile.profileForm('main');form=formFrom(modalHTML);
    assert.equal(form.get('trigger_name').value,'false');
    assert.equal(form.get('trigger_reply').options.find(option=>option.value==='false').label,'关闭');
    form.choose('trigger_at',choice);form.choose('trigger_name',choice);
    await experience.handleSubmit('bot-profile',form);
    assert.equal(calls.at(-1)[0],'/api/bot-profiles');
    assert.equal(payload().items[0].values.trigger_at,expected);
    assert.equal(payload().items[0].values.trigger_name,expected);
    assert.equal(payload().items[0].values.trigger_reply,true);
    state.botProfiles=payload();profile.profileForm('main');
    assert.equal(formFrom(modalHTML).get('trigger_name').value,expected==null?'':String(expected));
  }

  // The directory describes the effective policy, including legacy nested scopes
  // and profile-resolved fields from /api/scopes, without promising a false=reply.
  const cases=[
    [{trigger_at:false,trigger_name:true},{},'@：由 Planner 判断 · 名称：直接回复'],
    [{trigger_at:null,trigger_name:null},{respond_to_at:true,respond_to_name:false},'@：直接回复 · 名称：由 Planner 判断'],
    [{trigger_at:false,trigger_name:false},{planner_enabled:false},'@：Planner 已关闭，不因该提及回复 · 名称：Planner 已关闭，不因该提及回复'],
    [{trigger_at:false,trigger_name:false,plan_enabled:true},{planner_enabled:false},'@：由 Planner 判断 · 名称：由 Planner 判断'],
    [{settings:{trigger_at:false,trigger_name:true,plan_enabled:false}},{},'@：Planner 已关闭，不因该提及回复 · 名称：直接回复'],
    [{trigger_at:null,settings:{trigger_at:false},trigger_name:false},{respond_to_at:true},'@：直接回复 · 名称：由 Planner 判断'],
    [{always_reply:true,trigger_at:false,trigger_name:false},{planner_enabled:false},'回复策略 · 每条必回复'],
  ];
  for(const [overrides,identity,expected] of cases){
    state.settings.identity=identity;
    state.scopes=[{id:'qq:group:3',name:'群名',...overrides}];
    html=await ui.renderGroups();assert.ok(html.includes(expected),expected);
  }
  console.log('mention reply choices, real form serialization, profile inheritance and effective directory policies passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
