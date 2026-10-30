"use strict";
// Exercise the single local Chat path and its in-turn job controls without a
// browser, model credentials, filesystem writes or real command execution.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const nodes=new Map(),calls=[],sources=[],notices=[];
const e=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const input={value:"整理这份文件",focus(){}},detail={innerHTML:""};nodes.set("#chat-text",input);nodes.set("#live-job-detail",detail);
const state={scope:"web:default",page:"chat",authenticated:true,authGeneration:1,scopes:[{id:"web:default",name:"工作台"}],
  settings:{computer:{enabled:true,workspace:"D:/project/<safe>"}},boot:{},images:[],draft:"",chatMode:"computer",sending:false};
const base={id:"inline-one",kind:"computer",inline:true,scope_id:state.scope,title:"读取文件",stage:"read_file",
  status:"awaiting_approval",approval:{id:"approve-one",summary:"读取选定工作目录",details:{path:"notes.txt"}},
  events:[{type:"progress",data:{message:"<script>file</script>"}}]};
let records=[{...base,id:"finished",status:"completed",approval:null,result:{summary:"读取完成",files:[{name:"notes.txt",url:"/api/jobs/finished/files/0"},{name:"bad",url:"javascript:bad()"}]}}],bootstraps=0;
let response, messages=[];
class Source{constructor(url){this.url=url;this.closed=false;sources.push(this);}close(){this.closed=true;}addEventListener(){}}
const sandbox={window:{},state,TextDecoder,AbortController,URLSearchParams,EventSource:Source,setTimeout,clearTimeout,
  e,$:selector=>nodes.get(selector)||null,$$:()=>[],rows:value=>Array.isArray(value)?value:value?.items||[],query:params=>new URLSearchParams(params).toString(),
  button:(text,action,style,icon,attrs="")=>`<button data-action="${e(action)}" ${attrs}>${e(text)}</button>`,
  icon:()=>"",tag:e,notice:e,fmt:e,toolJSON:JSON.stringify,heading:()=>"",empty:()=>"",renderMessage:item=>`<p class="final-reply">${e(item.content)}</p>`,toolRunCard:()=>"",personaName:()=>"初音未来",scope:()=>state.scopes[0],
  getForm:form=>form.values,closeModal(){},openModal(){},toast:(...args)=>notices.push(args),renderPage:async()=>{},bootstrap:async()=>{bootstraps++;},
  api:async(url,options={})=>{calls.push([url,options]);if(url==="/api/chat")return response;
    if(url.startsWith("/api/messages?"))return messages;if(url.startsWith("/api/jobs?"))return records;
    if(url.endsWith("/approve"))return{...base,status:"running",approval:null};
    if(url.endsWith("/cancel"))return{...base,status:"cancelled",approval:null};return[];}
};
vm.createContext(sandbox);
for(const file of [...require('./ui_modules.cjs').experienceFactories,'experience.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static',file),'utf8'),sandbox,{filename:file});
const ui=sandbox.window.ShioriExperience;
const deferred=()=>{let resolve;const promise=new Promise(done=>{resolve=done;});return{promise,resolve};};
const frame=data=>new TextEncoder().encode('data: '+JSON.stringify(data)+'\n\n');
async function main(){
  const html=await ui.renderChat();assert.doesNotMatch(html,/id="chat-mode"/);assert.match(html,/电脑能力已开启/);assert.match(html,/D:\/project\/&lt;safe&gt;/);
  assert.match(html,/读取完成/);assert.match(html,/执行记录/);assert.match(html,/&lt;script&gt;file&lt;\/script&gt;/);assert.doesNotMatch(html,/javascript:bad/);
  assert.match(html,/\/api\/jobs\/finished\/files\/0/,'completed task artifacts remain available after rendering Chat');
  assert.match(html,/class="chat-tool-step"/);assert.doesNotMatch(html,/<details class="chat-tool-details"[^>]*\bopen\b/,'inline execution details start collapsed');
  messages=[{id:'answer-1',scope_id:state.scope,role:'assistant',content:'最终答复在步骤之后',metadata:{job_ids:['finished']}}];
  const completed=await ui.renderChat();assert.ok(completed.indexOf('data-job-id="finished"')<completed.indexOf('最终答复在步骤之后'));
  assert.equal((completed.match(/data-job-id="finished"/g)||[]).length,1,'completed inline steps render before their answer, not again at the bottom');
  messages=[];await ui.renderChat();
  const arrived=deferred(),finish=deferred();let reads=0;
  response={headers:{get:()=>"text/event-stream"},body:{getReader:()=>({async read(){
    if(reads++===0)return{value:frame({type:"job",data:base}),done:false};
    if(reads===2){arrived.resolve();await finish.promise;return{value:frame({type:"result",data:{reply:"已停止"}}),done:false};}
    return{done:true};},async cancel(){},releaseLock(){}})}};
  const pending=ui.sendChat();await arrived.promise;
  assert.equal(calls.find(([url])=>url==="/api/chat")[1].body.mode,"chat");
  assert.match(detail.innerHTML,/data-action="job-approve"/);assert.match(detail.innerHTML,/data-action="job-cancel"/);assert.match(detail.innerHTML,/执行记录/);
  assert.equal(state.sending,true);assert.equal(bootstraps,0);
  await ui.handleSubmit("job-approval",{values:{job_id:base.id,approval_id:"approve-one"}});
  assert.equal(bootstraps,0,"approving within a streamed turn must not replace the composer");assert.equal(state.sending,true);assert.doesNotMatch(detail.innerHTML,/data-action="job-approve"/);
  const oldSource=sources.at(-1);
  await ui.handleAction("job-cancel",{dataset:{id:base.id}});
  assert.equal(bootstraps,0);assert.equal(state.sending,true);assert.match(detail.innerHTML,/已取消/);assert.ok(oldSource.closed);
  const afterCancel=detail.innerHTML;oldSource.onmessage({data:JSON.stringify({...base,status:"running"})});assert.equal(detail.innerHTML,afterCancel,'late events from a closed task stream cannot resurrect cancelled tasks');
  finish.resolve();await pending;assert.equal(state.sending,false);assert.equal(bootstraps,1);
  state.liveJobs=[{...base,status:"failed",error:"失败信息"},{...base,id:"foreign",scope_id:"web:other",title:"foreign secret",status:"completed"}];
  const failed=ui.renderChatJobs();assert.match(failed,/失败信息/);assert.doesNotMatch(failed,/foreign secret/);
  state.settings.computer={enabled:false};records=[];assert.match(await ui.renderChat(),/开启电脑能力/);
  ui.close();console.log("Single Chat harness: legacy mode ignored, task events, process/results/artifacts, scoped history, approval/cancellation and stale-stream guards passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
