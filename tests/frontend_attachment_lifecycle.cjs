"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const calls=[],nodes=new Map();let modal=null,perform=null,responder=async()=>({});
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const state={scope:'web:default',page:'chat',scopes:[{id:'web:default',name:'local'}],settings:{model:{},computer:{}},boot:{},images:[],personas:[],filters:{}};
const sandbox={window:{},state,FormData,AbortController,URLSearchParams,setTimeout,clearTimeout,
  EventSource:class{close(){}addEventListener(){}},e:escape,configuredModel:()=>({}),query:obj=>new URLSearchParams(obj).toString(),
  rows:obj=>Array.isArray(obj)?obj:(obj.items||[]),heading:()=>'',icon:()=>'',tag:()=>'',notice:escape,empty:()=>'',fmt:String,
  button:(label,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${escape(label)}</button>`,
  scope:()=>state.scopes.find(item=>item.id===state.scope)||{},personaName:()=>'',renderMessage:()=>'',toolRunCard:()=>'',toolJSON:JSON.stringify,
  api:async(url,opts)=>{calls.push([url,opts]);return responder(url,opts);},toast:()=>{},
  $:selector=>nodes.get(selector)||null,$$:()=>[],bootstrap:async()=>{},renderPage:async()=>{},navigate:async()=>{},
  localStorage:{setItem(){}},document:{getElementById:()=>null},getForm:()=>({}),
  openModal:(...args)=>{modal=args;},closeModal:()=>{modal=null;},confirmAction:async(a,b,callback)=>{perform=callback;},
  field:()=>'',select:()=>'',toggle:()=>'',textarea:()=>'',renderPersonas:async()=>'',renderSettings:async()=>''};
vm.createContext(sandbox);
for(const file of [...require('./ui_modules.cjs').experienceFactories,'experience.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../mikubot/static',file),'utf8'),sandbox,{filename:file});
const ui=sandbox.window.MikubotExperience,plain=value=>JSON.parse(JSON.stringify(value));
const refId='a'.repeat(32),defaultResponse=(url,opts)=>url==='/api/chat/attachments'?{id:refId,name:'fixture.txt'}:{};
async function upload(){const file=new Blob(['fixture']);file.name='fixture.txt';await ui.handleChange({id:'attachment-input',value:'file',files:[file]});}

async function main(){
  nodes.set('#attachment-chips',{innerHTML:''});nodes.set('#chat-text',{value:'summarize fixture',focus(){}});
  responder=defaultResponse;
  await upload();
  await ui.handleAction('remove-attachment',{dataset:{index:'0'}});
  assert.equal(calls.at(-1)[0],'/api/chat/attachments/release');
  assert.deepEqual(plain(calls.at(-1)[1].body),{scope_id:'web:default',ids:[refId]});
  assert.ok(!calls.some(([,opts])=>opts?.method==='DELETE'),'removing a selection must not revoke a task source');

  await upload();responder=async(url,opts)=>url==='/api/chat'?{job:{id:'job',status:'queued'}}:url.endsWith('/release')?{retained:[refId]}:defaultResponse(url,opts);
  await ui.sendChat();
  const submitted=calls.findLast(([url])=>url==='/api/chat');
  assert.deepEqual(plain(submitted[1].body.attachments),[refId]);
  assert.equal(calls.at(-1)[0],'/api/chat/attachments/release','task creation releases the selection through the safe endpoint');
  assert.ok(!calls.some(([,opts])=>opts?.method==='DELETE'));

  responder=defaultResponse;await upload();state.scope='web:other';await ui.renderChat();
  assert.deepEqual(plain(calls.at(-1)[1].body),{scope_id:'web:default',ids:[refId]},'switching chats releases the old scope only');
  state.scope='web:default';responder=defaultResponse;await upload();
  responder=async(url,opts)=>{if(url.endsWith('/release'))throw new Error('fixture storage offline');return defaultResponse(url,opts);};
  await assert.rejects(ui.handleAction('remove-attachment',{dataset:{index:'0'}}),/offline/);
  assert.match(await ui.renderChat(),/attachment-chip/,'a failed release must not discard the visible selection');
  responder=defaultResponse;await ui.handleAction('remove-attachment',{dataset:{index:'0'}});

  responder=async(url)=>{if(url==='/api/chat/attachments'){state.scope='web:other';return{id:refId};}return{};};
  await upload();
  assert.deepEqual(plain(calls.at(-1)[1].body),{scope_id:'web:default',ids:[refId]},'late upload completion releases its captured owner scope');
  state.scope='web:default';

  responder=async()=>({items:[{id:refId,name:'<img src=x onerror=evil>',purpose:'legacy_unknown',characters:80001}],total:1,temporary_count:1,temporary_limit:200});
  await ui.handleAction('attachment-manager',{dataset:{}});
  assert.match(modal[2],/&lt;img/);assert.ok(!modal[2].includes('<img'));
  assert.match(modal[2],/旧版资料/);assert.match(modal[2],/attachment-preview/);
  assert.ok(!modal[2].includes('attachment-reuse'),'legacy purpose is not silently converted to a new temporary reference');

  responder=async()=>({name:'legacy',purpose:'legacy_unknown',preview:'<script>private reference</script>',in_use:true});
  await ui.handleAction('attachment-preview',{dataset:{id:refId,scope:'web:default'}});
  assert.match(modal[1],/正在被使用/);assert.match(modal[2],/&lt;script&gt;/);
  assert.match(modal[2],/attachment-delete/);
  await ui.handleAction('attachment-delete',{dataset:{id:refId,scope:'web:default'}});
  responder=defaultResponse;await perform();
  assert.equal(calls.at(-1)[1].method,'DELETE','explicit removal retains revocation semantics');
  assert.equal(calls.at(-1)[0],`/api/chat/attachments/${refId}?scope_id=web%3Adefault`);

  // A reference GET may finish after a send has already consumed/released that
  // very reference. Even if sending is false again, the old receipt is stale.
  let completeLookup;
  responder=async(url)=>url.startsWith('/api/chat/attachments/'+refId+'?')?new Promise(resolve=>{completeLookup=resolve;}):{};
  const lateReuse=ui.handleAction('attachment-reuse',{dataset:{id:refId,scope:'web:default'}});
  nodes.get('#chat-text').value='another turn';state.draft='another turn';
  await ui.sendChat();
  completeLookup({id:refId,name:'late.txt'});
  await assert.rejects(lateReuse,/已变化/);
  assert.ok(!(await ui.renderChat()).includes('late.txt'),'a completed send invalidates in-flight reuse receipts');

  responder=async(url)=>url.startsWith('/api/chat/attachments/'+refId+'?')?new Promise(resolve=>{completeLookup=resolve;}):{};
  const otherScope=ui.handleAction('attachment-reuse',{dataset:{id:refId,scope:'web:default'}});
  state.scope='web:other';completeLookup({id:refId,name:'wrong-scope.txt'});
  await assert.rejects(otherScope,/已变化/);
  assert.ok(!(await ui.renderChat()).includes('wrong-scope.txt'),'a late reuse callback must retain its captured owner scope');
  console.log('PASS: attachment release, task protection, scope changes, failed release, restart management and explicit revocation UI');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
