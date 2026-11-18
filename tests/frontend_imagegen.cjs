"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[];let modal,confirmed;
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const saved={id:'main',name:'<b>主力</b>',kind:'openai',endpoint:'https://api.openai.com/v1',model:'gpt-image-1',timeout_seconds:180,negative_prompt:'',
  natural_negative:false,reference:true,quality:'',steps:28,scale:5,sampler:'k_euler_ancestral',strength:.6,extra_body:{},workflow:{},workflow_fields:{},output_node:'',api_key_set:true,supports_reference:true};
let config={enabled:true,service_id:'main',daily_per_user:5,services:[saved],personas:{default:{prompt:'long brown hair',negative_prompt:''}},
  persona_list:[{id:'default',name:'佐藤栞'}],usage:{counts:{'qq:1':2,'qq:2':1},queued:1,running:0},warning:''};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false,handleSubmit:async()=>false}},state:{},
  e:escape,api:async(url,options)=>{calls.push([url,options]);if(url==='/api/imagegen/test')return {image:'data:image/png;base64,AAAA',elapsed_ms:12000,bytes:2048};
    if(options?.method==='PUT'){const body=options.body;if(body.services)config={...config,services:body.services.map(x=>({...x,api_key_set:Boolean(x.api_key)||x.id==='main'}))};if('service_id' in body)config={...config,service_id:body.service_id};}
    return config;},
  field:(name,title,value)=>`<input name="${name}" value="${escape(value)}">`,textarea:(name,title,value)=>`<textarea name="${name}">${escape(value)}</textarea>`,
  select:(name,title,options)=>`<select name="${name}">${options.map(([v,t])=>`<option value="${escape(v)}">${escape(t)}</option>`).join('')}</select>`,
  toggle:name=>`<input type="checkbox" name="${name}">`,button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${escape(text)}</button>`,
  getForm:form=>form.values,openModal:(...args)=>{modal=args;sandbox.state.modalRecord=args[4];},closeModal:()=>{},toast:()=>{},renderPage:async()=>{},
  confirmAction:async(title,text,fn)=>{confirmed=[title,text];return fn();}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/imagegen.js'),'utf8'),sandbox);
async function main(){
  const ui=sandbox.window.ShioriImageGen,actions=sandbox.window.ShioriExperience;
  let html=await ui.render();
  assert.ok(!html.includes('<b>主力</b>'));assert.match(html,/&lt;b&gt;主力&lt;\/b&gt; · 当前使用/);
  assert.match(html,/已保存密钥/);assert.match(html,/今天普通成员已画 3 张（2 人）· 排队 1 张/);
  assert.match(html,/data-form="imagegen-settings"/);assert.match(html,/data-form="imagegen-personas"/);assert.match(html,/long brown hair/);
  assert.match(html,/data-form="imagegen-test"/);assert.match(html,/data-kind="comfyui"/);
  await actions.handleSubmit('imagegen-settings',{values:{enabled:true,service_id:'main',daily_per_user:'3'}});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{enabled:true,service_id:'main',daily_per_user:3});
  // Editing keeps the saved key: nothing is sent for it, and display-only fields are dropped.
  await actions.handleAction('imagegen-edit',{dataset:{id:'main'}});assert.equal(modal[3],'imagegen-service');assert.match(modal[2],/name="clear_api_key"/);
  await actions.handleSubmit('imagegen-service',{values:{name:'主力',endpoint:'https://relay.example/v1',api_key:'',model:'gpt-image-1',timeout_seconds:'120',negative_prompt:'',natural_negative:false,quality:'high',reference:false,extra_body:'{"user":"shiori"}'}});
  let body=calls.at(-1)[1].body;
  assert.equal(body.services.length,1);assert.equal(body.services[0].id,'main');assert.equal(body.services[0].timeout_seconds,120);
  assert.equal(body.services[0].reference,false);assert.deepEqual(JSON.parse(JSON.stringify(body.services[0].extra_body)),{user:'shiori'});
  assert.ok(!('api_key' in body.services[0])&&!('api_key_set' in body.services[0])&&!('supports_reference' in body.services[0]));
  await assert.rejects(actions.handleSubmit('imagegen-service',{values:{name:'x',endpoint:'https://x',model:'m',timeout_seconds:'60',extra_body:'{oops'}}),/不是有效的 JSON/);
  // A ComfyUI workflow with its node mapping.
  await actions.handleAction('imagegen-add',{dataset:{kind:'comfyui'}});assert.match(modal[2],/map_prompt/);
  await actions.handleSubmit('imagegen-service',{values:{name:'本机',endpoint:'http://127.0.0.1:8188',api_key:'',model:'',timeout_seconds:'300',negative_prompt:'lowres',
    workflow:'{"6":{"class_type":"CLIPTextEncode","inputs":{"text":""}}}',map_prompt:'6.inputs.text',map_negative_prompt:'',map_reference:'',map_width:'',map_height:'',map_seed:'',output_node:''}});
  body=calls.at(-1)[1].body;
  assert.equal(body.services.length,2);assert.equal(body.services[1].kind,'comfyui');
  assert.deepEqual(JSON.parse(JSON.stringify(body.services[1].workflow_fields)),{prompt:'6.inputs.text'});
  // Deleting the current service turns generation off with it.
  await actions.handleAction('imagegen-delete',{dataset:{id:'main'}});
  assert.match(confirmed[1],/当前使用的服务/);body=calls.at(-1)[1].body;
  assert.equal(body.service_id,'');assert.equal(body.enabled,false);assert.ok(body.services.every(x=>x.id!=='main'));
  await actions.handleSubmit('imagegen-personas',{values:{prompt__default:' silver hair ',negative__default:''}});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{personas:{default:{prompt:'silver hair',negative_prompt:''}}});
  await actions.handleSubmit('imagegen-test',{values:{prompt:'一只猫',aspect:'portrait'}});
  assert.equal(calls.at(-1)[0],'/api/imagegen/test');assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{prompt:'一只猫',aspect:'portrait'});
  config={...config,services:[saved],service_id:'main'};
  html=await ui.render();assert.match(html,/用时 12 秒/);assert.match(html,/src="data:image\/png;base64,AAAA"/);
}
main().catch(error=>{console.error(error);process.exit(1);});
