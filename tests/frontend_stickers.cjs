"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[];let modal;
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const item={id:'aabb',scope_id:'qq:group:1',meaning:'<script>unsafe</script>',tags:['谢谢'],category:'感谢',enabled:true,clear:true,shareable:false,use_count:0};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false,handleSubmit:async()=>false}},state:{scope:'qq:group:1',settings:{stickers:{},providers:[]}},
  e:escape,query:params=>new URLSearchParams(Object.entries(params).filter(([,v])=>v!==undefined)).toString(),api:async(url,options)=>{calls.push([url,options]);return {items:[item],categories:['感谢'],total:1,offset:0,has_more:false};},
  field:(name,title,value)=>`<input name="${name}" value="${escape(value)}">`,textarea:(name,title,value)=>`<textarea name="${name}">${escape(value)}</textarea>`,select:()=>'',toggle:()=>'',tag:escape,scopeName:escape,
  button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${text}</button>`,getForm:form=>form.values,splitNames:value=>value.split(/[，,]/).map(x=>x.trim()).filter(Boolean),
  openModal:(...args)=>{modal=args;sandbox.state.modalRecord=args[4];},closeModal:()=>{},toast:()=>{},bootstrap:async()=>{},renderPage:async()=>{},confirmAction:async(title,text,fn)=>fn()};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/stickers.js'),'utf8'),sandbox);
async function main(){
  const ui=sandbox.window.ShioriStickers,actions=sandbox.window.ShioriExperience;
  let html=await ui.render();assert.ok(!html.includes('<script>'));assert.match(html,/&lt;script&gt;/);assert.match(html,/value="0.1"/);assert.match(html,/api\/stickers\/aabb\/image/);
  await actions.handleAction('sticker-edit',{dataset:{id:'aabb'}});assert.equal(modal[3],'sticker-edit');assert.ok(!modal[2].includes('<script>'));
  await actions.handleSubmit('sticker-edit',{values:{meaning:'表示感谢',category:'感谢',tags:'开心，谢谢',clear:true,enabled:true,shareable:false}});
  assert.equal(calls.at(-1)[0],'/api/stickers/aabb');assert.equal(calls.at(-1)[1].method,'PUT');assert.equal(calls.at(-1)[1].body.tags.length,2);
  await actions.handleSubmit('sticker-settings',{values:{enabled:true,collect_enabled:true,send_enabled:true,collect_probability:'.2',send_probability:'.3',daily_recognitions:'20',max_items:'1000',model_id:''}});
  assert.equal(calls.at(-1)[1].body.stickers.send_probability,.3);
  await actions.handleAction('sticker-page',{dataset:{offset:'50'}});assert.equal(sandbox.state.stickerOffset,50);
  await actions.handleSubmit('sticker-filter',{values:{q:'谢谢',category:'感谢',all:false}});await ui.render();assert.match(calls.at(-1)[0],/offset=0/);
  await actions.handleAction('sticker-delete',{dataset:{id:'aabb'}});assert.equal(calls.at(-1)[1].method,'DELETE');
  console.log('sticker defaults, safe previews, editing, probabilities and filter pagination passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
