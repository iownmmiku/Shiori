"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[],messages=[],fields=[];
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const catalog={providers:[{id:'p1',name:'Source',api_key_set:true}],models:[
  {id:'default',model:'legacy',enabled:true},{id:'chat1',provider_id:'p1',model:'chat-main',enabled:true,vision:true},
  {id:'disabled',model:'old<script>x</script>',enabled:false},{id:'vector',model:'embed',model_type:'embedding',enabled:true}],
  routing:{chat:'disabled',planner:'chat1',memory:'disabled',embedding:'vector',gate:'chat1',fallback:['disabled']},
  routes:{chat:{model_id:'disabled',model:'old<script>x</script>'},distillation:{model:'chat-main'}},embedding:{enabled:true,model:'embed'}};
let embedding=false,modal=null,fail=false;
const result={textContent:''};
const sandbox={window:{ShioriPages:{renderModels:async()=>'<div>library</div>'},ShioriEmbedding:{active:()=>embedding}},
  state:{settings:{model:{model:'legacy'},stickers:{model_id:'disabled'}}},routeNames:{chat:'对话',planner:'判断',memory:'记忆',summary:'整理',distillation:'蒸馏',embedding:'检索',gate:'旧判断'},
  document:{createElement:()=>({innerHTML:'',content:{querySelector:()=>null}})},
  e:escape,select:(name,label,options,value)=>{fields.push({name,options,value});return `<select name="${name}">${options.map(([id,label])=>`<option value="${escape(id)}" ${String(id)===String(value)?'selected':''}>${escape(label)}</option>`).join('')}</select>`;},
  tag:escape,button:(text,action)=>`<button data-action="${action}">${escape(text)}</button>`,getForm:form=>form.values,
  api:async(url,opts)=>{calls.push([url,opts]);if(url==='/api/model-providers')return catalog;if(url==='/api/model/test'){if(fail)throw Error('unavailable');return{ok:true};}return{};},
  $:()=>result,toast:text=>messages.push(text),bootstrap:async()=>{},settingsModelForm:()=>'<form-fields>',openModal:(...args)=>modal=args
};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/bot-models.js'),'utf8'),sandbox);
const ui=sandbox.window.ShioriBotModels,plain=value=>JSON.parse(JSON.stringify(value));
async function main(){
  const html=await ui.render();assert.match(html,/默认使用哪些模型/);assert.ok(!html.includes('<script>'));
  for(const name of ['chat','memory','sticker_model_id','fallback_0']){
    const field=fields.find(item=>item.name===name);assert.equal(field.value,'disabled');assert.ok(field.options.some(([id])=>id==='disabled'),'unavailable selection must not silently become an empty route');
  }
  assert.ok(!fields.find(item=>item.name==='chat').options.some(([id])=>id==='vector'),'embedding is not a chat choice');
  assert.match(fields.find(item=>item.name==='summary').options[0][1],/继承全局对话模型/);
  sandbox.state.settings.model.base_url='https://legacy.invalid/v1';
  fields.length=0;await ui.render();
  assert.match(fields.find(item=>item.name==='summary').options[0][1],/基础默认模型/);
  calls.length=0;
  await ui.handleSubmit('bot-model-defaults',{values:{chat:'chat1',sticker_model_id:''}});
  assert.deepEqual(plain(calls[0]),['/api/settings',{method:'PUT',body:{routing:{chat:'chat1'},stickers:{model_id:''}}}]);
  calls.length=0;
  await ui.handleSubmit('bot-model-advanced',{values:{planner:'chat1',memory:'',summary:'chat1',distillation:'',fallback_0:'chat1',fallback_1:'chat1',fallback_2:''}});
  assert.deepEqual(plain(calls[0][1].body),{planner:'chat1',memory:'',summary:'chat1',distillation:'',fallback:['chat1']});
  assert.ok(!('chat' in calls[0][1].body)&&!('embedding' in calls[0][1].body)&&!('gate' in calls[0][1].body),'advanced save preserves other routes');
  calls.length=0;const target={disabled:false,closest:()=>({values:{chat:'chat1'}})};
  await ui.handleAction('test-bot-chat-model',target);
  assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/model/test');assert.equal(target.disabled,false);assert.match(result.textContent,/测试不保存选择/);
  fail=true;await ui.handleAction('test-bot-chat-model',target);assert.match(result.textContent,/测试失败/);assert.equal(target.disabled,false);
  await ui.handleAction('edit-base-model',{});assert.equal(modal[3],'model');
  embedding=true;fields.length=0;assert.ok(!(await ui.render()).includes('bot-model-defaults'));assert.equal(fields.length,0,'embedding keeps its own activation flow');
  console.log('nested model routing, unavailable selections, minimal saves, tests and embedding independence passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
