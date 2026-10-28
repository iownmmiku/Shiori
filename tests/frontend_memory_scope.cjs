"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const calls=[],renders=[];
const escape=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const record={id:"memory-fixture",scope_id:"qq:private:42",user_id:"42",status:"active",kind:"fact",content:"fixture <script>unsafe</script>"};
const sandbox={window:{ShioriFeatures:{memoryToolsPanel:async()=>""}},URLSearchParams,
  state:{page:"memory",scope:"web:default",filters:{},learningAll:false},fieldId:0,
  e:escape,icon:()=>"",label:value=>value,fmt:value=>value,
  scopeName:id=>id==="qq:private:42"?'<img src=x onerror="unsafe">':id==="web:default"?"本地聊天":id,
  rows:value=>Array.isArray(value)?value:value.items||[],
  query:params=>new URLSearchParams(Object.entries(params).filter(([,v])=>v!==undefined&&v!==""&&v!==null)).toString(),
  renderPage:async()=>renders.push(true),
  api:async url=>{
    calls.push(url);const params=new URL(url,"http://local").searchParams;
    const visible=!params.has("scope_id")&&!params.has("q");
    if(url.startsWith("/api/memories?")||url.startsWith("/api/memory/candidates?")){
      const offset=Number(params.get("offset")||0);
      return {items:visible?[record]:[],total:visible?120:0,offset,limit:50,next_offset:offset+50,has_more:visible&&offset<100};
    }
    if(url.startsWith("/api/learning/dashboard?"))return {counts:{active:visible?120:0},policy:{},trend:[{day:"10-24",count:visible?1:0}],progress:[]};
    return [];
  },
};
vm.createContext(sandbox);
for(const name of ["ui-core.js","memory-page.js","learning-dashboard.js"])
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static",name),"utf8"),sandbox);
const paramsFor=prefix=>new URL(calls.findLast(url=>url.startsWith(prefix)),"http://local").searchParams;
async function main(){
  let html=await sandbox.renderMemory();
  assert.equal(paramsFor("/api/memories?").has("scope_id"),false,"memory opens across all chats, even when current chat is empty");
  assert.equal(paramsFor("/api/learning/dashboard?").has("scope_id"),false,"overview and records must use the same range");
  assert.match(html,/查看范围：全部会话/);assert.match(html,/共 120 条/);
  assert.match(html,/来源会话 · &lt;img/);assert.match(html,/&lt;script&gt;unsafe/);
  assert.ok(!html.includes('<img src=x')&&!html.includes('<script>'),"source names and record bodies remain escaped");
  assert.match(html,/机器人回复时仍按各会话的隔离规则召回/);
  assert.equal((html.match(/data-action="learning-all"/g)||[]).length,1,"one range control above the records");
  sandbox.state.memoryPage.offset=50;
  await sandbox.window.ShioriLearning.handleAction("learning-all",{dataset:{kind:"memory"}});
  html=await sandbox.renderMemory();
  assert.equal(paramsFor("/api/memories?").get("scope_id"),"web:default");
  assert.equal(paramsFor("/api/memories?").get("offset"),"0","range changes reset pagination");
  assert.equal(paramsFor("/api/learning/dashboard?").get("scope_id"),"web:default");
  assert.match(html,/查看范围：当前聊天 · 本地聊天/);assert.match(html,/当前聊天还没有保存记忆/);
  assert.match(html,/查看全部会话/);assert.equal(sandbox.state.learningAll,false,"memory range does not change culture range");
  // The empty-state button has no data-kind; actual app page supplies its context.
  await sandbox.window.ShioriLearning.handleAction("learning-all",{dataset:{}});
  await sandbox.renderMemory();assert.equal(paramsFor("/api/memories?").has("scope_id"),false);
  sandbox.state.filters={q:"missing"};html=await sandbox.renderMemory();
  assert.match(html,/没有符合筛选条件的记忆/);assert.ok(!html.includes("还没有保存记忆"));
  sandbox.state.filters={status:"pending"};html=await sandbox.renderMemory();
  assert.equal(paramsFor("/api/memory/candidates?").has("scope_id"),false,"candidate review keeps the chosen range");
  assert.equal(paramsFor("/api/memory/candidates?").get("limit"),"50");assert.match(html,/待你确认/);
  sandbox.state.page="culture";
  await sandbox.window.ShioriLearning.panel("language");
  assert.equal(paramsFor("/api/learning/dashboard?").get("scope_id"),"web:default");
  await sandbox.window.ShioriLearning.handleAction("learning-all",{dataset:{kind:"language"}});
  await sandbox.window.ShioriLearning.panel("language");
  assert.equal(paramsFor("/api/learning/dashboard?").has("scope_id"),false);
  assert.equal(sandbox.state.memoryAll,true,"culture range does not change memory range");
  assert.equal(renders.length,3);
  console.log("memory default range, scope source, pagination, candidates, empty states and culture independence passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
