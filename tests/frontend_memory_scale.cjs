"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../shiori/static/memory-page.js'),'utf8');
const core=fs.readFileSync(path.join(__dirname,'../shiori/static/ui-core.js'),'utf8');
const calls=[],nodes=new Map();let modal;
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function node(){return {children:[],parentNode:null,append(child){this.children=this.children.filter(x=>x!==child);child.parentNode?.children.splice(child.parentNode.children.indexOf(child),1);this.children.push(child);child.parentNode=this;},prepend(child){this.append(child);this.children.unshift(this.children.pop());},remove(){this.parentNode?.children.splice(this.parentNode.children.indexOf(this),1);this.parentNode=null;}};}
const body=node(),region=node(),dialog=node();dialog.open=false;body.append(region);
const sandbox={window:{},URLSearchParams,state:{scope:'web:default',filters:{}},
  $:selector=>nodes.get(selector)||({'#toast-region':region,'#modal':dialog}[selector]),document:{body,createElement:node},
  e:escape,icon:()=>'',scopeName:id=>id,setTimeout:()=>{},rows:value=>Array.isArray(value)?value:value.items||[],
  heading:()=>'',listToolbar:()=>'<div class="toolbar">',notice:()=>'',empty:()=>'',memoryCard:item=>`<article>${escape(item.content)}</article>`,
  button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${text}</button>`,field:()=>'',
  query:params=>new URLSearchParams(Object.entries(params).filter(([,value])=>value!==undefined&&value!=='')).toString(),
  api:async url=>{calls.push(url);if(url.startsWith('/api/memories?')||url.startsWith('/api/memory/candidates?')){const p=new URL(url,'http://local').searchParams,offset=Number(p.get('offset')||0);return {items:[{id:'fixture',content:'<script>unsafe</script>',user_id:'owner'}],total:120,offset,limit:50,next_offset:offset+50,has_more:offset+50<120};}return [];},
  renderPage:async()=>{},getForm:form=>form.values,select:()=>'',
  openModal:(...args)=>{modal=args;sandbox.state.modalRecord=args[4];},
};
vm.createContext(sandbox);
vm.runInContext(core.slice(core.indexOf('function syncToastLayer()'),core.indexOf('function heading(')),sandbox);
vm.runInContext(source.slice(source.indexOf('async function renderMemoryRecords()')),sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/workspace-features.js'),'utf8'),sandbox);
async function main(){
  sandbox.toast('existing message');const existing=region.children[0];dialog.open=true;sandbox.syncToastLayer();
  assert.equal(region.parentNode,dialog);assert.equal(region.children[0],existing,'opening a modal preserves existing notifications');
  sandbox.toast('<script>error</script>',true);assert.match(region.children[1].innerHTML,/&lt;script&gt;/);
  dialog.open=false;sandbox.syncToastLayer();assert.equal(region.parentNode,body,'closing a modal restores the normal notification layer');
  let html=await sandbox.renderMemoryRecords();assert.match(calls.at(-1),/limit=50/);assert.match(html,/第 1 页/);assert.ok(!html.includes('<script>'));
  const ui=sandbox.window.ShioriFeatures;
  await ui.handleAction('memory-page',{dataset:{offset:'50'}});await sandbox.renderMemoryRecords();assert.match(calls.at(-1),/offset=50/);
  sandbox.state.filters.q='old fact';await sandbox.renderMemoryRecords();assert.match(calls.at(-1),/offset=0/,'search resets the page');
  sandbox.state.filters.status='pending';await sandbox.renderMemoryRecords();assert.ok(calls.at(-1).startsWith('/api/memory/candidates?'));assert.match(calls.at(-1),/q=old\+fact/);
  calls.length=0;await ui.memoryToolsPanel();assert.equal(calls.length,0,'closed tools must not fetch full memory or relation lists');
  await ui.handleAction('open-memory-tools',{});await ui.memoryToolsPanel();assert.ok(calls.some(url=>url.startsWith('/api/memories?')&&url.includes('limit=200')));
  await ui.handleSubmit('memory-relation-search',{values:{q:'older memory'}});assert.equal(sandbox.state.relationMemoryQuery,'older memory');
  sandbox.state.relationMemories=[];await ui.handleAction('new-memory-relation',{dataset:{}});
  assert.equal(modal[0],'关联两条记忆');assert.match(modal[2],/data-endpoint="from_id"/);
  const firstSelect={innerHTML:''},secondSelect={innerHTML:'keep selection'};
  nodes.set('[name="from_id"]',firstSelect);nodes.set('[name="to_id"]',secondSelect);
  const form={isConnected:true,values:{from_id_query:'older fact'}};
  await ui.handleAction('search-relation-memory',{dataset:{endpoint:'from_id'},closest:()=>form});
  assert.match(calls.at(-1),/q=older\+fact/);assert.match(calls.at(-1),/limit=50/);
  assert.match(firstSelect.innerHTML,/value="fixture"/);assert.ok(!firstSelect.innerHTML.includes('<script>'));
  assert.equal(secondSelect.innerHTML,'keep selection','searching one endpoint preserves the other endpoint');
  sandbox.api=async()=>{sandbox.state.modalRecord={};return {items:[]};};
  const prior=firstSelect.innerHTML;
  await ui.handleAction('search-relation-memory',{dataset:{endpoint:'from_id'},closest:()=>form});
  assert.equal(firstSelect.innerHTML,prior,'a late search result cannot replace a different modal');
  console.log('modal notifications, memory pagination, filter reset and lazy relation tools passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
