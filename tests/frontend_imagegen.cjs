"use strict";
// The image generation extension's page, loaded with the real extension SDK.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[];let dialog,confirmed,toasts=[];
const saved={id:'main',name:'<b>主力</b>',kind:'openai',endpoint:'https://api.openai.com/v1',model:'gpt-image-1',timeout_seconds:180,negative_prompt:'',
  natural_negative:false,reference:true,quality:'',steps:28,scale:5,sampler:'k_euler_ancestral',strength:.6,extra_body:{},workflow:{},workflow_fields:{},output_node:'',api_key_set:true,supports_reference:true};
const thumb=id=>({id,thumb:'data:image/jpeg;base64,BBBB'});
let config={service_id:'main',services:[saved],personas:{default:{prompt:'long brown hair',negative_prompt:'',outfit:'white sailor uniform',references:[thumb('0123456789abcdef.png')],
  outfits:[{scope_id:'qq:group:404',outfit:'<i>black maid outfit</i>',updated:'2026-11-19T08:00:00Z'}]}},daily_per_user:5,
  persona_list:[{id:'default',name:'佐藤栞'}],usage:{counts:{'qq:1':2,'qq:2':1},queued:1,running:0},warning:''};
const listeners={};
const root={innerHTML:'',addEventListener:(type,fn)=>{listeners[type]=fn;}};
const sandbox={console,setTimeout,clearTimeout,location:{pathname:'/extensions/imagegen/pages/main/'},addEventListener:()=>{},
  document:{getElementById:id=>id==='app'?root:null},FormData:class{},
  FileReader:class{readAsDataURL(file){this.result='data:'+file.type+';base64,CCCC';this.onload();}}};
sandbox.window=sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/extension-sdk.js'),'utf8'),sandbox);
const ext=sandbox.ShioriExtension;
assert.equal(ext.id,'imagegen');
assert.equal(ext.escape('<a href="x">'),'&lt;a href=&quot;x&quot;&gt;');
assert.match(ext.ui.field('daily','每天','<5>','number','提示'),/value="&lt;5&gt;"/);
assert.match(ext.ui.field('key','密钥','secret','password'),/value=""/);           // passwords are never echoed
// Network, dialogs and toasts are replaced; the page's own logic runs as shipped.
Object.assign(ext,{
  api:async(route,options={})=>{calls.push([route,options]);if(route==='test')return {image:'data:image/png;base64,AAAA',elapsed_ms:12000,bytes:2048,persona_references:options.body.persona_id?1:0};
    if(route==='outfit'){const look=config.personas.default;config={...config,personas:{default:{...look,outfits:look.outfits.filter(x=>x.scope_id!==options.body.scope_id)}}};return config;}
    if(route==='reference'){const look=config.personas.default,refs=look.references||[];
      config={...config,personas:{default:{...look,references:options.method==='POST'?[...refs,thumb('fedcba9876543210.jpg')]:refs.filter(x=>x.id!==options.body.id)}}};return config;}
    if(options.method==='PUT'){const body=options.body;if(body.services)config={...config,services:body.services.map(x=>({...x,api_key_set:Boolean(x.api_key)||x.id==='main'}))};if('service_id' in body)config={...config,service_id:body.service_id};if(body.personas)config={...config,personas:body.personas};}
    return config;},
  dialog:(...args)=>{dialog=args;},confirm:async(title,text,fn)=>{confirmed=[title,text];return fn();},toast:(message,error)=>toasts.push([message,Boolean(error)])});
ext.ui.values=form=>form.values;
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/extensions/imagegen/pages/main/app.js'),'utf8'),sandbox);
const settle=()=>new Promise(resolve=>setTimeout(resolve,0));
const click=(action,dataset={})=>listeners.click({target:{closest:()=>({dataset:{action,...dataset}})}});
const change=(personaId,file)=>listeners.change({target:{closest:()=>({dataset:{upload:personaId},files:[file],value:'C:\\fakepath\\a.png'})}});
const submit=(name,values)=>listeners.submit({preventDefault(){},target:{closest:()=>({dataset:{form:name},values,querySelector:()=>null})}});
async function main(){
  await settle();
  let html=root.innerHTML;
  assert.ok(!html.includes('<b>主力</b>'));assert.match(html,/&lt;b&gt;主力&lt;\/b&gt; · 当前使用/);
  assert.match(html,/已保存密钥/);assert.match(html,/今天普通成员已画 3 张（2 人）· 排队 1 张/);assert.match(html,/每人每天最多 5 张/);
  assert.match(html,/data-form="personas"/);assert.match(html,/long brown hair/);assert.match(html,/data-form="test"/);assert.match(html,/data-kind="comfyui"/);
  // The default outfit, and the outfits she changed into in chats, each with a way back to the default.
  assert.match(html,/name="outfit__default"[^>]*>white sailor uniform<\/textarea>/);assert.match(html,/不要写衣服/);
  assert.match(html,/qq:group:404<\/strong> · &lt;i&gt;black maid outfit&lt;\/i&gt;/);
  assert.match(html,/data-action="forget-outfit" data-persona="default" data-scope="qq:group:404"/);
  await click('forget-outfit',{persona:'default',scope:'qq:group:404'});await settle();
  assert.equal(calls.at(-1)[0],'outfit');assert.equal(calls.at(-1)[1].method,'DELETE');
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{persona_id:'default',scope_id:'qq:group:404'});
  assert.deepEqual(toasts.at(-1),['已换回默认服装',false]);assert.ok(!root.innerHTML.includes('聊天里换上的衣服'));
  // A persona's reference pictures: thumbnails, removal and uploads up to three.
  assert.match(html,/src="data:image\/jpeg;base64,BBBB" alt="佐藤栞的参考图 1"/);assert.match(html,/data-action="remove-reference" data-persona="default" data-id="0123456789abcdef.png"/);
  assert.match(html,/data-upload="default"/);assert.match(html,/name="persona_id"/);assert.match(html,/佐藤栞的自拍/);
  await change('default',{type:'image/gif',size:10});await settle();assert.deepEqual(toasts.at(-1),['请选择 PNG、JPEG 或 WebP 图片。',true]);
  await change('default',{type:'image/png',size:11*1024*1024});await settle();assert.deepEqual(toasts.at(-1),['参考图不能超过 10 MB。',true]);
  await change('default',{type:'image/webp',size:2048});await settle();
  assert.equal(calls.at(-1)[0],'reference');assert.equal(calls.at(-1)[1].method,'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{persona_id:'default',image:'data:image/webp;base64,CCCC'});
  await change('default',{type:'image/jpeg',size:2048});await settle();
  html=root.innerHTML;assert.match(html,/参考图 3/);assert.ok(!html.includes('data-upload='));     // full: no more uploads
  await click('remove-reference',{persona:'default',id:'0123456789abcdef.png'});await settle();
  assert.match(confirmed[1],/佐藤栞以后自拍不再参考它/);assert.equal(calls.at(-1)[1].method,'DELETE');
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{persona_id:'default',id:'0123456789abcdef.png'});
  assert.match(root.innerHTML,/data-upload="default"/);
  // Editing keeps the saved key: nothing is sent for it, and display-only fields are dropped.
  await click('edit',{id:'main'});assert.match(dialog[2],/name="clear_api_key"/);assert.match(dialog[0],/编辑生图服务/);
  assert.match(dialog[2],/name="persona_reference" aria-label="自拍时带上人格参考图" checked/);    // on by default for OpenAI
  await dialog[3]({name:'主力',endpoint:'https://relay.example/v1',api_key:'',model:'gpt-image-1',timeout_seconds:'120',negative_prompt:'',natural_negative:false,quality:'high',reference:false,extra_body:'{"user":"shiori"}'});
  let body=calls.at(-1)[1].body;
  assert.equal(calls.at(-1)[0],'config');assert.equal(body.services.length,1);assert.equal(body.services[0].timeout_seconds,120);
  assert.equal(body.services[0].reference,false);assert.equal(body.services[0].persona_reference,false);assert.deepEqual(JSON.parse(JSON.stringify(body.services[0].extra_body)),{user:'shiori'});
  assert.ok(!('api_key' in body.services[0])&&!('api_key_set' in body.services[0])&&!('supports_reference' in body.services[0]));
  await assert.rejects(dialog[3]({name:'x',endpoint:'https://x',model:'m',timeout_seconds:'60',extra_body:'{oops'}),/不是有效的 JSON/);
  // A ComfyUI workflow with its node mapping.
  await click('add',{kind:'comfyui'});assert.match(dialog[2],/map_prompt/);
  assert.match(dialog[2],/name="persona_reference" aria-label="自拍时带上人格参考图" >/);assert.match(dialog[2],/IPAdapter/);   // off for ComfyUI
  await dialog[3]({name:'本机',endpoint:'http://127.0.0.1:8188',api_key:'',model:'',timeout_seconds:'300',negative_prompt:'lowres',
    workflow:'{"6":{"class_type":"CLIPTextEncode","inputs":{"text":""}}}',map_prompt:'6.inputs.text',map_negative_prompt:'',map_reference:'',map_width:'',map_height:'',map_seed:'',output_node:''});
  body=calls.at(-1)[1].body;
  assert.equal(body.services.length,2);assert.equal(body.services[1].kind,'comfyui');
  assert.deepEqual(JSON.parse(JSON.stringify(body.services[1].workflow_fields)),{prompt:'6.inputs.text'});
  // Deleting the current service clears the selection with it.
  await click('delete',{id:'main'});await settle();
  assert.match(confirmed[1],/当前使用的服务/);body=calls.at(-1)[1].body;
  assert.equal(body.service_id,'');assert.ok(body.services.every(x=>x.id!=='main'));
  await submit('personas',{prompt__default:' silver hair ',outfit__default:'',negative__default:''});await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{personas:{default:{prompt:'silver hair',negative_prompt:''}}});   // pictures are not resent
  await submit('personas',{prompt__default:'',outfit__default:' red hoodie ',negative__default:''});await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{personas:{default:{prompt:'',negative_prompt:'',outfit:'red hoodie'}}});
  config={...config,services:[saved],service_id:'main'};
  await click('use',{id:'main'});await settle();
  await submit('test',{prompt:'一只猫',aspect:'portrait'});await settle();
  assert.equal(calls.at(-1)[0],'test');assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{prompt:'一只猫',aspect:'portrait'});
  html=root.innerHTML;assert.match(html,/用时 12 秒/);assert.match(html,/src="data:image\/png;base64,AAAA"/);assert.ok(!html.includes('人格参考图</figcaption>'));
  await submit('test',{prompt:'自拍',aspect:'portrait',persona_id:'default'});await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{prompt:'自拍',aspect:'portrait',persona_id:'default'});
  html=root.innerHTML;assert.match(html,/带了 1 张人格参考图/);assert.match(html,/<option value="default" selected>/);
  // An error from the API reaches the owner as a toast, not as an unhandled rejection.
  ext.api=async()=>{throw new Error('生图接口返回 HTTP 401');};
  await submit('test',{prompt:'一只猫',aspect:'square'});await settle();
  assert.deepEqual(toasts.at(-1),['生图接口返回 HTTP 401',true]);
  console.log('image generation page passed');
}
main().catch(error=>{console.error(error);process.exit(1);});
