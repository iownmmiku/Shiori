"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const calls=[],modal=[],navigations=[];
let computerEnabled=true;
const schema={type:'object',properties:{text:{type:'string',title:'<目标>'},count:{type:'integer',minimum:1},enabled:{type:'boolean'},mode:{type:'string',enum:['a','b']},rows:{type:'array'}},required:['text']};
const plugin={id:'demo_plugin',name:'示例',description:'演示',enabled:true,revision:1,code:'def run(input):\n    return input',manifest:{version:'1.0.0',input_schema:schema}};
const scan={scan_id:'preview1',candidates:[{candidate_id:'bad',root:'bad',error:'<不安全>',file_count:1,bytes:1},{candidate_id:'good',root:'one/plugin',manifest:{...plugin,id:plugin.id,version:'1.0.0'},file_count:3,bytes:123,replaces_existing:true,warnings:['仅说明，不授予权限']}]};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false,handleSubmit:async()=>false}},state:{scope:'web:default'},e:escape,
  heading:(a,b,c)=>a+b+c,button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${escape(text)}</button>`,
  field:(n,t,v,type='text',hint='',extra='')=>`<label>${escape(t)}<input name="${n}" type="${type}" value="${escape(v)}" ${extra}></label>`,textarea:(n,t,v)=>`<label>${escape(t)}<textarea name="${n}">${escape(v)}</textarea></label>`,
  FormData:class{constructor(form){this.values=form.values;}entries(){return Object.entries(this.values)[Symbol.iterator]();}[Symbol.iterator](){return this.entries();}get(key){return this.values[key]??null;}},
  api:async(url,options)=>{calls.push([url,options]);if(url==='/api/plugins')return{items:[plugin],runtime:{enabled:computerEnabled,runtime:'docker',approval_mode:'automatic'}};if(url==='/api/plugins/import')return scan;if(url.endsWith('/run'))return{id:'job1'};return plugin;},
  openModal:(...args)=>modal.push(args),closeModal:()=>{},toast:()=>{},confirmAction:async(t,d,fn)=>fn(),renderPage:async()=>{},navigate:async page=>navigations.push(page),$:()=>null};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/plugins.js'),'utf8'),sandbox,{filename:'plugins.js'});
async function main(){
  const actions=sandbox.window.ShioriExperience;
  const html=await sandbox.window.ShioriPlugins.render();assert.match(html,/导入插件包/);assert.match(html,/用聊天创建/);assert.match(html,/\/api\/plugins\/demo_plugin\/export/);
  sandbox.state.boot={setup_required:true};computerEnabled=false;
  const preparation=await sandbox.window.ShioriPlugins.render();assert.match(preparation,/运行准备/);assert.match(preparation,/data-action="quick-model"/);assert.match(preparation,/data-action="computer-settings"/);
  await actions.handleAction('plugin-import',{dataset:{}});assert.match(modal.at(-1)[2],/type="file"/);
  await actions.handleSubmit('plugin-import',{values:{file:{name:'one.zip',size:100}}});
  assert.equal(calls.at(-1)[0],'/api/plugins/import');assert.match(modal.at(-1)[2],/value="good" checked/);assert.match(modal.at(-1)[2],/&lt;不安全&gt;/);assert.ok(!modal.at(-1)[2].includes('<不安全>'));
  await actions.handleSubmit('plugin-install',{values:{candidate_id:'good',replace:'on'}});
  assert.equal(calls.at(-1)[0],'/api/plugins/import/install');assert.equal(calls.at(-1)[1].body.replace,true);assert.equal(calls.at(-1)[1].body.scan_id,'preview1');
  await actions.handleAction('plugin-run',{dataset:{id:plugin.id}});assert.match(modal.at(-1)[2],/&lt;目标&gt;/);assert.match(modal.at(-1)[2],/type="number"/);assert.match(modal.at(-1)[2],/name="arg_2"/);
  await actions.handleSubmit('plugin-run',{dataset:{id:plugin.id,schema:JSON.stringify(schema)},values:{arg_0:'hello',arg_1:'3',arg_2:'on',arg_3:'1',arg_4:'[1,2]'}});
  const input=calls.at(-1)[1].body.input;assert.equal(input.text,'hello');assert.equal(input.count,3);assert.equal(input.enabled,true);assert.equal(input.mode,'b');assert.equal(JSON.stringify(input.rows),'[1,2]');
  await assert.rejects(()=>actions.handleSubmit('plugin-run',{dataset:{id:plugin.id,schema:JSON.stringify(schema)},values:{}}),/请填写/);
  await actions.handleSubmit('plugin-run',{dataset:{id:plugin.id,schema:JSON.stringify(schema)},values:{input_override:'{"text":"advanced"}'}});assert.equal(calls.at(-1)[1].body.input.text,'advanced');
  await actions.handleSubmit('plugin-editor',{values:{id:plugin.id,name:'x',description:'x',code:plugin.code,input_schema:JSON.stringify(schema)}});assert.equal(calls.at(-1)[1].body.input_schema.type,'object');
  sandbox.state.scope='qq:private:1';sandbox.state.draft='已有草稿';sandbox.state.chatMode='computer';sandbox.state.sending=true;
  await assert.rejects(()=>actions.handleAction('plugin-chat',{dataset:{}}),/先完成或取消/);assert.equal(sandbox.state.scope,'qq:private:1');assert.equal(sandbox.state.draft,'已有草稿');
  sandbox.state.sending=false;sandbox.state.debouncePending=true;
  await assert.rejects(()=>actions.handleAction('plugin-chat',{dataset:{}}),/先完成或取消/);assert.equal(sandbox.state.chatMode,'computer');assert.equal(sandbox.state.draft,'已有草稿');
  sandbox.state.debouncePending=false;
  await actions.handleAction('plugin-chat',{dataset:{}});assert.equal(sandbox.state.scope,'web:default');assert.equal(sandbox.state.chatMode,'chat');assert.equal(navigations.at(-1),'chat');assert.match(sandbox.state.draft,/先保存为草稿/);
  console.log('plugin package preview, escaped candidates, schema forms, ZIP export and local chat creation passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
