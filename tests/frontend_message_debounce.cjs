"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
let now=0,serial=0;
const timers=new Map(),calls=[],notices=[],input={value:'',focus(){}};
const state={scope:'web:default',page:'chat',settings:{debounce:{enabled:true,seconds:3,max_wait_seconds:30}},images:[],draft:'',chatMode:'chat',sending:false};
const runtime={attachments:[],attachmentScope:'web:default'};
class Clock extends Date {constructor(...args){super(...(args.length?args:[Date.UTC(2026,9,23)+now]));}static now(){return now;}}
let fail=false,overrides={};
const sandbox={window:{},state,Date:Clock,AbortController,
  setTimeout:(fn,ms)=>{const id=++serial;timers.set(id,{fn,at:now+ms});return id;},clearTimeout:id=>timers.delete(id),
  scope:()=>overrides,$:selector=>selector==='#chat-text'?input:null,e:String,
  renderPage:async()=>{input.value=state.draft || '';},bootstrap:async()=>{input.value=state.draft || '';},
  toast:(text,error)=>notices.push([text,error]),
  api:async(url,opts)=>{calls.push([url,opts]);if(url==='/api/chat'){if(fail)throw new Error('transport interrupted');return{reply:'complete'};}return{};}
};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../mikubot/static/experience-chat.js'),'utf8'),sandbox);
const ui=sandbox.window.MikubotExperienceChat.create(runtime,{}), sent=()=>calls.filter(([url])=>url==='/api/chat');
async function advance(ms){const end=now+ms;for(;;){const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;timers.delete(next[0]);now=next[1].at;await next[1].fn();await new Promise(resolve=>setImmediate(resolve));}now=end;}
async function say(text){input.value=text;state.draft=text;await ui.sendChat();}
function reset(){ui.close();calls.length=0;notices.length=0;timers.clear();now=0;state.scope='web:default';state.images=[];state.draft='';state.sending=false;state.chatMode='chat';runtime.attachments=[];input.value='';fail=false;overrides={};state.settings.debounce={enabled:true,seconds:3,max_wait_seconds:30};}
async function main(){
  await say('第一条');await advance(2000);await say('第二条');await advance(2000);await say('第三条');
  await advance(2999);assert.equal(sent().length,0);assert.equal(state.debouncePending,true);
  await advance(1);assert.equal(sent().length,1);assert.equal(sent()[0][1].body.text,'第一条\n第二条\n第三条');
  assert.equal(sent()[0][1].body.messages.length,3);assert.equal(state.debouncePending,false);assert.equal(state.sending,false);

  reset();state.settings.debounce.max_wait_seconds=5;
  await say('0');await advance(2000);await say('2');await advance(2000);await say('4');await advance(1000);
  assert.equal(sent().length,1,'continuous input is capped at the first message maximum wait');

  reset();await say('取消前');state.draft='还没发送';input.value=state.draft;await ui.cancelCollected();
  assert.equal(sent().length,0);assert.equal(state.draft,'取消前\n还没发送');await advance(10000);assert.equal(sent().length,0);

  reset();await say('马上回复');await ui.flushCollected();await advance(10000);assert.equal(sent().length,1,'flush consumes its timer exactly once');
  reset();await say('保留草稿');input.value='正在编辑的下一条';state.draft=input.value;await advance(3000);
  assert.equal(state.draft,'正在编辑的下一条','automatic flush must preserve text not submitted to collection');

  reset();state.settings.debounce.enabled=false;await say('直接发');assert.equal(sent().length,1);assert.equal(sent()[0][1].body.messages,undefined);
  reset();overrides={debounce_enabled:false};await say('会话关闭');assert.equal(sent().length,1);
  reset();overrides={debounce_seconds:1};await say('单独秒数');await advance(1000);assert.equal(sent().length,1);

  reset();await say('不要再说');await say('停止');await advance(10000);assert.equal(sent().length,0,'stop cancels buffered content without a model request');
  reset();state.chatMode='computer';await say('运行测试');assert.equal(sent().length,1,'explicit computer mode is submitted immediately');
  reset();await say('等待中注销');ui.close();await advance(10000);assert.equal(sent().length,0);

  reset();for(let i=0;i<8;i++)await say('消息'+i);assert.equal(sent().length,1);assert.equal(sent()[0][1].body.messages.length,8);assert.equal(state.draft,'','automatic full-batch send must not restore its final consumed message as an unsent draft');
  reset();await say('旧等待');state.settings.debounce.enabled=false;await say('关闭后的补充');await advance(0);
  assert.equal(sent().length,1);assert.equal(sent()[0][1].body.text,'旧等待\n关闭后的补充');assert.equal(state.debouncePending,false);
  reset();await say('失败也不重放');fail=true;await advance(3000);await advance(10000);
  assert.equal(sent().length,1);assert.equal(state.draft,'失败也不重放');assert.ok(notices.some(([text])=>text.includes('transport interrupted')));
  console.log('local trailing debounce, maximum wait, immediate send, cancel, overrides, drafts and no replay passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
