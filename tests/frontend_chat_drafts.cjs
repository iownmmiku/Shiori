"use strict";
// Exercise the shipped chat factory with a real in-memory attachment lifecycle.
// No browser, network request, model or automatically replayed task is involved.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../mikubot/static/experience-chat.js"),"utf8");
const plain=value=>JSON.parse(JSON.stringify(value));
const A={id:"a".repeat(32),name:"first.txt"},B={id:"b".repeat(32),name:"next.txt"},C={id:"c".repeat(32),name:"other.txt"};
const image=name=>({name,data:"data:image/png;base64,"+name});
function fixture(){
  const calls=[],notices=[],available=new Set([A.id,B.id,C.id]),timers=new Map();let serial=0;
  const state={scope:"web:default",page:"chat",settings:{debounce:{enabled:true,seconds:3,max_wait_seconds:30}},
    scopes:[{id:"web:default",name:"first"},{id:"web:other",name:"other"}],boot:{},images:[],draft:"",chatMode:"chat",sending:false};
  const input={value:"",focus(){}},runtime={attachments:[],attachmentScope:state.scope};
  const config={bootstrapError:false,releaseError:false,renderError:false,reply:async()=>({reply:"complete"})};
  const render=async()=>{if(config.renderError)throw new Error("render interrupted");input.value=state.draft;};
  const sandbox={window:{},state,AbortController,Date,URLSearchParams,
    setTimeout:fn=>{timers.set(++serial,fn);return serial;},clearTimeout:id=>timers.delete(id),
    $:selector=>selector==="#chat-text"?input:null,scope:()=>({}),e:String,
    heading:()=>"",button:()=>"",notice:()=>"",icon:()=>"",personaName:()=>"",renderMessage:()=>"",toolRunCard:()=>"",
    query:value=>new URLSearchParams(value).toString(),rows:value=>value.items || [],
    renderPage:render,bootstrap:async()=>{if(config.bootstrapError)throw new Error("refresh offline");await render();},
    toast:(...args)=>notices.push(args),
    api:async(url,options={})=>{
      calls.push([url,plain(options.body || {})]);
      if(url==="/api/chat"){
        assert.ok(options.body.attachments.every(id=>available.has(id)),"a displayed reference must still exist when sent");
        return config.reply(options);
      }
      if(url.endsWith("/release")){
        if(config.releaseError)throw new Error("release offline");
        options.body.ids.forEach(id=>available.delete(id));
        return{deleted:options.body.ids,retained:[]};
      }
      return{items:[]};
    }
  };
  vm.createContext(sandbox);vm.runInContext(source,sandbox);
  const ui=sandbox.window.MikubotExperienceChat.create(runtime,{inactive:()=>true,jobDetail:()=>""});
  const draft=(text,images=[],attachments=[])=>{input.value=text;state.draft=text;state.images=images;runtime.attachments=attachments;runtime.attachmentScope=state.scope;};
  const say=async(text,images=[],attachments=[])=>{draft(text,images,attachments);await ui.sendChat();};
  return{ui,state,runtime,input,config,calls,notices,available,draft,say,timers,sends:()=>calls.filter(([url])=>url==="/api/chat"),releases:()=>calls.filter(([url])=>url.endsWith("/release"))};
}
async function verifyOutcome(outcome,refreshError){
  const f=fixture(),first=image("first"),next=image("next");
  await f.say("已提交的一条",[first],[A]);f.draft("尚未发送的补充",[next],[B]);
  f.config.bootstrapError=refreshError;
  if(outcome==="failure")f.config.reply=async()=>{throw new Error("transport interrupted");};
  let entered;
  if(outcome==="cancel"){
    const started=new Promise(resolve=>{entered=resolve;});
    f.config.reply=options=>new Promise((resolve,reject)=>{
      options.signal.addEventListener("abort",()=>{const error=new Error("cancelled");error.name="AbortError";reject(error);},{once:true});entered();
    });
    const pending=f.ui.flushCollected();await started;f.state.controller.abort();await pending;
  }else await f.ui.flushCollected();
  assert.equal(f.sends().length,1);assert.equal(f.state.sending,false);assert.equal(f.state.debouncePending,false);
  assert.equal(f.state.draft,outcome==="success"?"尚未发送的补充":"已提交的一条\n尚未发送的补充");
  assert.equal(f.input.value,f.state.draft,"the visible input follows the preserved draft even if refresh fails");
  assert.deepEqual(plain(f.state.images),outcome==="success"?[next]:[first,next]);
  assert.deepEqual(plain(f.runtime.attachments),outcome==="success"?[B]:[A,B]);
  assert.equal(f.available.has(A.id),outcome!=="success");assert.ok(f.available.has(B.id));
  assert.equal(f.timers.size,0,"a completed/cancelled turn never leaves an automatic replay timer");
  if(refreshError)assert.ok(f.notices.some(([text])=>text.includes("刷新失败")));
  f.ui.close();
}
async function main(){
  for(const outcome of ["success","failure","cancel"])for(const refreshError of [false,true])await verifyOutcome(outcome,refreshError);

  {const f=fixture();await f.say("第一轮",[image("one")],[A]);f.draft("第二轮",[image("two")],[A]);
    f.config.bootstrapError=true;await f.ui.flushCollected();
    assert.equal(f.state.draft,"第二轮");assert.deepEqual(plain(f.runtime.attachments),[A]);
    assert.ok(f.available.has(A.id));assert.equal(f.releases().length,0,"the next draft owns the shared reference");
    f.config.bootstrapError=false;await f.ui.sendChat();await f.ui.flushCollected();
    assert.equal(f.sends().length,2);assert.deepEqual(f.sends()[1][1].attachments,[A.id]);
    assert.equal(f.runtime.attachments.length,0);assert.ok(!f.available.has(A.id),"the last selection releases exactly once");
    assert.equal(f.releases().length,1);}

  {const f=fixture();await f.say("第一条",[],[A]);await f.say("仍引用这一份",[],[A]);
    f.draft("草稿里暂时移除",[],[A]);await f.ui.releaseAttachments([A]);f.runtime.attachments=[];
    assert.ok(f.available.has(A.id));assert.equal(f.releases().length,0,"removing a duplicate selection must preserve collected references");
    await f.ui.flushCollected();assert.equal(f.sends().length,1);assert.deepEqual(f.sends()[0][1].attachments,[A.id]);
    assert.equal(f.releases().length,1);}

  {const f=fixture();await f.say("收集中的文字",[image("one")],[A]);f.draft("编辑中的文字",[image("two")],[A,B]);
    await f.ui.cancelCollected();assert.equal(f.sends().length,0);assert.equal(f.state.draft,"收集中的文字\n编辑中的文字");
    assert.deepEqual(plain(f.runtime.attachments),[A,B]);assert.equal(f.state.images.length,2);assert.equal(f.releases().length,0);}

  {const f=fixture();await f.say("取消这条",[],[A]);f.draft("仍要参考同一份资料",[],[A]);
    await f.ui.cancelCollected(false);assert.equal(f.sends().length,0);assert.ok(f.available.has(A.id));
    assert.equal(f.state.draft,"仍要参考同一份资料");assert.deepEqual(plain(f.runtime.attachments),[A]);}

  {const f=fixture();await f.say("已完成请求",[],[A]);f.draft("未发送",[],[B]);
    f.config.releaseError=true;f.config.bootstrapError=true;await f.ui.flushCollected();
    assert.equal(f.state.draft,"未发送");assert.deepEqual(plain(f.runtime.attachments),[B]);
    assert.equal(f.sends().length,1,"a cleanup/refresh failure cannot turn an accepted request back into a resend draft");}

  {const f=fixture();await f.say("尚未发往后端",[image("one")],[A]);f.draft("继续编辑",[image("two")],[B]);
    f.config.renderError=true;f.config.bootstrapError=true;await f.ui.flushCollected();
    assert.equal(f.sends().length,0);assert.equal(f.state.draft,"尚未发往后端\n继续编辑");
    assert.deepEqual(plain(f.runtime.attachments),[A,B]);assert.equal(f.state.sending,false);}

  {const f=fixture();await f.say("原会话的请求",[image("one")],[A]);f.draft("原会话的草稿",[image("two")],[B]);
    f.config.reply=async()=>{f.state.scope="web:other";f.draft("另一个会话的草稿",[image("other")],[C]);throw new Error("transport interrupted");};
    await f.ui.flushCollected();assert.equal(f.state.draft,"另一个会话的草稿");assert.deepEqual(plain(f.runtime.attachments),[C]);
    f.state.scope="web:default";f.draft("");await f.ui.renderChat();
    assert.equal(f.state.draft,"原会话的请求\n原会话的草稿");assert.deepEqual(plain(f.runtime.attachments),[A,B]);
    assert.equal(f.sends().length,1,"restoring a scoped draft does not dispatch it");}

  console.log("PASS: drafts survive success/failure/cancellation/refresh errors; shared attachments, release failures and scope ownership remain consistent");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
