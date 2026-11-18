"use strict";
// The console's 扩展 page: listing, trust confirmation, generated settings form and embedded pages.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[];let modal,confirmed;
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const imagegen={id:'imagegen',name:'生图',version:'1.0.0',description:'画图',author:'Shiori',builtin:true,enabled:true,loaded:true,error:'',trusted:true,
  pages:[{id:'main',title:'生图设置',entry:'pages/main/index.html',url:'/extensions/imagegen/pages/main/'}],tools:['generate_image'],
  config_schema:{daily_per_user:{type:'int',title:'每人每天最多张数',description:'',default:5,min:0,max:1000}},config:{daily_per_user:5},secrets_set:{}};
const hello={id:'hello',name:'<i>挥手</i>',version:'0.1.0',description:'',author:'',builtin:false,enabled:false,loaded:false,error:'',trusted:false,
  pages:[{id:'main',title:'挥手设置',entry:'pages/main/index.html',url:'/extensions/hello/pages/main/'}],tools:[],
  config_schema:{greeting:{type:'string',title:'问候语',default:'你好'},loud:{type:'bool',title:'大声',default:false},token:{type:'secret',title:'令牌'}},
  config:{greeting:'你好',loud:false},secrets_set:{token:true}};
const broken={...hello,id:'broken',name:'坏掉的',error:'加载失败：RuntimeError: boom',config_schema:{}};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false,handleSubmit:async()=>false}},state:{},e:escape,
  api:async(url,options)=>{calls.push([url,options]);return url==='/api/extensions'?{items:[imagegen,hello,broken]}:{};},
  heading:(eyebrow,title,description,actions='')=>`<h1>${escape(title)}</h1>${actions}`,
  button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${escape(text)}</button>`,
  field:(name,title,value,type)=>`<input name="${name}" type="${type}" value="${type==='password'?'':escape(value)}">`,textarea:(name)=>`<textarea name="${name}"></textarea>`,
  select:(name)=>`<select name="${name}"></select>`,toggle:name=>`<input type="checkbox" name="${name}">`,
  getForm:form=>form.values,openModal:(...args)=>{modal=args;sandbox.state.modalRecord=args[4];},closeModal:()=>{},toast:()=>{},renderPage:async()=>{},
  confirmAction:async(title,text,fn)=>{confirmed=[title,text];return fn();}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/extensions.js'),'utf8'),sandbox);
async function main(){
  const ui=sandbox.window.ShioriExtensions,actions=sandbox.window.ShioriExperience;
  let html=await ui.render();
  assert.match(html,/<h1>扩展<\/h1>/);assert.match(html,/权限和 Shiori 相同/);
  assert.ok(!html.includes('<i>挥手</i>'));assert.match(html,/&lt;i&gt;挥手&lt;\/i&gt;/);
  assert.match(html,/Shiori 自带/);assert.match(html,/已停用 · 尚未确认可信/);assert.match(html,/加载失败：RuntimeError: boom/);
  assert.match(html,/给聊天模型的工具：generate_image/);
  assert.match(html,/data-action="extension-open" data-id="imagegen" data-extension-page="main"/);
  assert.ok(!/data-action="extension-open" data-id="hello"/.test(html));          // off: no page yet
  // data-page is the console's navigation attribute; an extension page button must not carry it.
  assert.ok(!/data-page=/.test(html));
  assert.ok(!/data-action="extension-toggle" data-id="broken"/.test(html));       // broken: cannot be switched on
  // A data-folder extension needs the trust confirmation.
  await actions.handleAction('extension-toggle',{dataset:{id:'hello',enabled:'true'}});
  assert.match(confirmed[1],/权限和 Shiori 相同/);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))),['/api/extensions/hello',{method:'PUT',body:{enabled:true,confirm:true}}]);
  await actions.handleAction('extension-toggle',{dataset:{id:'imagegen',enabled:'false'}});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{enabled:false});
  // The settings form comes from the manifest; an empty secret keeps the saved one.
  await actions.handleAction('extension-settings',{dataset:{id:'hello'}});
  assert.equal(modal[3],'extension-config');assert.match(modal[2],/name="cfg_greeting"/);assert.match(modal[2],/name="cfg_loud"/);
  assert.match(modal[2],/name="cfg_token" type="password" value=""/);assert.match(modal[2],/name="clear_token"/);
  await actions.handleSubmit('extension-config',{values:{cfg_greeting:'早上好',cfg_loud:true,cfg_token:'',clear_token:false}});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{config:{greeting:'早上好',loud:true},clear:[]});
  await actions.handleAction('extension-settings',{dataset:{id:'imagegen'}});
  await assert.rejects(actions.handleSubmit('extension-config',{values:{cfg_daily_per_user:''}}),/须为数字/);
  await actions.handleSubmit('extension-config',{values:{cfg_daily_per_user:'3'}});
  assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1)[1].body)),{config:{daily_per_user:3},clear:[]});
  // An extension page opens embedded in the console.
  await actions.handleAction('extension-open',{dataset:{id:'imagegen',extensionPage:'main'}});
  html=await ui.render();
  assert.match(html,/<iframe class="extension-frame" src="\/extensions\/imagegen\/pages\/main\/"/);assert.match(html,/生图 · 生图设置/);
  await actions.handleAction('extension-back',{dataset:{}});
  assert.equal(sandbox.state.extensionPage,null);
  console.log('extensions page passed');
}
main().catch(error=>{console.error(error);process.exit(1);});
