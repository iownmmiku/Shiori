"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[],nodes=new Map();let modal=null,fail=false;
const escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const item={id:'e1',provider_id:'p1',name:'Embedding <script>x</script>',model:'vector-fixture',model_type:'embedding',enabled:true,effective_enabled:true};
const catalog={providers:[{id:'p1',name:'Vector source',base_url:'https://fixture.example/v1'}],models:[item],routing:{chat:'chat1',embedding:'e1'},embedding:{enabled:false,available:true,model_id:'e1',model:'vector-fixture'}};
const state={page:'models'},sandbox={window:{},state,e:escape,button:(title,action,style,icon,attrs='')=>`<button data-action="${action}" ${attrs}>${escape(title)}</button>`,tag:escape,
  heading:(...v)=>v.join(''),empty:(...v)=>v.join(''),field:(name,title,value)=>`<input name="${name}" value="${escape(value)}">`,select:(name,title,options)=>`<select name="${name}">${options.map(([id,title])=>`<option value="${escape(id)}">${escape(title)}</option>`).join('')}</select>`,
  api:async(url,options)=>{calls.push([url,options]);if(url==='/api/model-providers' && !options)return catalog;
    if(url==='/api/model-providers' && options?.method==='POST')return{id:'new-source'};
    if(url==='/api/model-catalog/models')return{models:[{...item,provider_id:options.body.provider_id,model:options.body.models[0].model}],added:[item]};
    if(url==='/api/model-catalog/embedding/test'){if(fail)throw Error('bad vector');return{ok:true,dimension:3,elapsed_ms:12};}return{};},
  $:selector=>nodes.get(selector),getForm:form=>form.values,openModal:(...args)=>{modal=args;state.modalKind=args[3];},closeModal:()=>{modal=null;},bootstrap:async()=>{},renderPage:async()=>{},navigate:async page=>state.page=page,toast:()=>{},setTimeout:()=>1,clearTimeout:()=>{}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/embedding-models.js'),'utf8'),sandbox);
const ui=sandbox.window.ShioriEmbedding,plain=x=>JSON.parse(JSON.stringify(x));
async function main(){
  assert.match(ui.tabs(),/对话模型/);assert.match(ui.tabs(),/嵌入模型/);
  await ui.handleAction('model-type-tab',{dataset:{id:'embedding'}});assert.equal(ui.active(),true);
  const html=await ui.render();assert.match(html,/语义检索已关闭/);assert.match(html,/当前检索模型/);assert.match(html,/&lt;script&gt;/);assert.ok(!html.includes('<script>'));
  await ui.handleAction('new-embedding',{dataset:{}});assert.equal(modal[0],'添加嵌入模型');assert.match(modal[1],/\/embeddings/);assert.match(modal[2],/name="provider_id"/);assert.equal(modal[5],'保存、测试并启用检索');
  const form={dataset:{},values:{provider_id:'p1',model:'vector-fixture',input_price:'.25'}};
  calls.length=0;await ui.handleSubmit('embedding-setup',form);
  assert.deepEqual(calls.map(([url])=>url),['/api/model-catalog/models','/api/model-catalog/embedding/test']);
  assert.equal(calls[0][1].body.model_type,'embedding');assert.equal(calls[0][1].body.models[0].tools,false);
  assert.deepEqual(plain(calls[1][1].body),{model_id:'e1',activate:true});assert.equal(catalog.routing.chat,'chat1');assert.equal(modal,null);
  await ui.handleAction('test-embedding',{dataset:{id:'e1'}});assert.deepEqual(plain(calls.at(-1)[1].body),{model_id:'e1',activate:false});
  await ui.handleAction('disable-embedding',{dataset:{}});assert.deepEqual(plain(calls.at(-1)[1].body),{enabled:false});
  await ui.handleAction('new-embedding',{dataset:{}});fail=true;
  await assert.rejects(ui.handleSubmit('embedding-setup',form),/原检索设置未改变/);assert.notEqual(modal,null);
  fail=false;calls.length=0;const fresh={dataset:{},values:{provider_id:'',source_name:'Local',base_url:'http://localhost:1234/v1',api_key:'fixture-secret',model:'vector-fixture',input_price:'0'}};
  await ui.handleSubmit('embedding-setup',fresh);assert.equal(fresh.dataset.savedProvider,'new-source');
  assert.equal(calls[0][0],'/api/model-providers');assert.equal(calls[1][1].body.provider_id,'new-source');
  await ui.handleAction('edit-embedding',{dataset:{id:'e1'}});assert.equal(modal[0],'嵌入模型参数');assert.ok(!modal[2].includes('temperature'));assert.ok(!modal[2].includes('tools'));
  console.log('Embedding page interactions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
