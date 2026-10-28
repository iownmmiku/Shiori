"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const calls=[];let modal=null,local=false;
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const item={id:'usage-1',model:'<script>secret</script>',created_at:'now',purpose:'chat',input_tokens:10,output_tokens:90,cost:1};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false,handleSubmit:async()=>false}},crypto:{randomUUID:()=> '12345678-1234-1234-1234-123456789abc'},
 e:escape,fmt:String,number:String,button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${text}</button>`,
 select:()=>'<select name="action"></select>',field:name=>`<input name="${name}">`,textarea:name=>`<textarea name="${name}"></textarea>`,
 api:async(url,options)=>{calls.push([url,options]);if(url==='/api/auth/status')return {local_setup_allowed:local};if(url.startsWith('/api/usage/unconfirmed'))return {items:[item],total:1};return{};},
 openModal:(...args)=>{modal=args;},closeModal:()=>{},toast:()=>{},renderPage:async()=>{},getForm:form=>form.values};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/usage-ledger.js'),'utf8'),sandbox);
(async()=>{
 const ui=sandbox.window.ShioriExperience;
 await ui.handleAction('usage-open',{});assert.match(modal[2],/请在运行电脑本机核实/);assert.ok(!modal[2].includes('<script>'));
 await assert.rejects(ui.handleAction('usage-reconcile',{dataset:{id:item.id}}),/本机/);
 local=true;await ui.handleAction('usage-open',{});assert.match(modal[2],/usage-reconcile/);assert.match(modal[2],/&lt;script&gt;/);
 await ui.handleAction('usage-reconcile',{dataset:{id:item.id}});assert.equal(modal[3],'usage-reconciliation');assert.match(modal[2],/reconciliation_id/);
 await ui.handleSubmit('usage-reconciliation',{values:{id:item.id,reconciliation_id:'12345678-1234-1234-1234-123456789abc',action:'actual',input_tokens:'5',output_tokens:'7',cost:'0.2',note:'已核对'}});
 assert.equal(calls.at(-1)[0],'/api/usage/usage-1/reconcile');assert.equal(calls.at(-1)[1].body.output_tokens,7);assert.equal(calls.at(-1)[1].body.note,'已核对');
 console.log('Usage ledger local-only controls, escaping and reconciliation payload passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
