"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const escape=value=>String(value??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const sandbox={window:{},state:{settings:{time:{timezone:'Asia/Hong_Kong',awareness_enabled:false}},botTab:'basic'},
  toggle:(name,title,hint,checked)=>`<label>${escape(title)}<input name="${name}" type="checkbox" ${checked?'checked':''}></label>`,
  field:(name,title,value)=>`<input name="${name}" value="${escape(value)}">`,e:escape,
  heading:()=>'',button:()=>'',configuredModel:()=>({}),identitySection:()=>'<p>identity</p>'};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/workspace-features.js'),'utf8'),sandbox);
sandbox.timeAwarenessSection=sandbox.window.ShioriFeatures.timeAwarenessSection;
let html=sandbox.timeAwarenessSection();
assert.match(html,/data-form="time-awareness"/);assert.match(html,/感知现实时间/);assert.ok(!html.includes('checked'));
sandbox.state.settings.time.awareness_enabled=true;html=sandbox.timeAwarenessSection();assert.match(html,/checked/);
sandbox.state.settings.time.timezone='"><img src=x onerror=evil>';
assert.ok(!sandbox.timeAwarenessSection().includes('<img'));
sandbox.window.ShioriExperience={renderPersonas:async()=>'',renderSettings:async()=>'',profilePanel:async()=>'',handleAction:async()=>false};
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/bot-config.js'),'utf8'),sandbox);
sandbox.window.ShioriExperience.renderPersonas().then(result=>assert.match(result,/id="time-awareness-section"/)).catch(error=>{console.error(error);process.exitCode=1;});
