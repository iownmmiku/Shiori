"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[],listeners={},inputs=new Map(),note={textContent:''};
const sandbox={window:{},state:{settings:{}},document:{addEventListener:(name,fn)=>{listeners[name]=fn;}},
  field:(key,label,value)=>{inputs.set(key,{value:String(value)});return `<input name="${key}" value="${value}">${label}`;},
  getForm:()=>Object.fromEntries([...inputs].map(([key,item])=>[key,item.value])),
  api:async(url,options)=>calls.push([url,options]),toast:()=>{},bootstrap:async()=>{}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../mikubot/static/resource-settings.js'),'utf8'),sandbox);
const ui=sandbox.window.MikubotResources;
const html=ui.render();assert.match(html,/在线语义检索总等待/);assert.match(html,/request_max_mb/);assert.equal(inputs.size,19);
assert.match(html,/近期对话轮数/);assert.equal(inputs.get('context__recent_turns').value,'24');
assert.match(html,/memory__background_indexing/);assert.match(html,/后台仍会调用嵌入模型并计费/);
inputs.set('memory__background_indexing',{value:'false'});
const form={querySelector:selector=>selector==='#resource-preset-note'?note:inputs.get(selector.match(/name="([^"]+)/)?.[1])};
listeners.click({target:{closest:()=>({closest:()=>form})}});
assert.equal(calls.length,0);assert.equal(inputs.get('memory__term_cache_mb').value,'16');assert.equal(inputs.get('scheduler__request_max_mb').value,'12');assert.match(note.textContent,/尚未保存/);
(async()=>{await ui.handleSubmit(form);assert.equal(calls.length,1);const [url,options]=calls[0];assert.equal(url,'/api/settings');assert.equal(options.method,'PUT');assert.equal(options.body.context.max_images,1);assert.equal(options.body.scheduler.global_model_concurrency,1);assert.equal(options.body.memory.semantic_timeout_seconds,8);assert.ok(!('model' in options.body));console.log('Resource controls preserve credentials and require explicit save');})().catch(error=>{console.error(error);process.exitCode=1;});
