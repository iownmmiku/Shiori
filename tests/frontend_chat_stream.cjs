"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const calls=[],writes=[],notices=[],nodes=new Map();
const state={scope:'web:default',page:'chat',images:[],chatMode:'chat',draft:'',sending:false};
let response,bootstrap=0;
const preview={innerHTML:'untouched',set textContent(text){writes.push(text);this.text=text;}};
nodes.set('#chat-preview',preview);nodes.set('#chat-preview-status',{textContent:''});
nodes.set('#chat-text',{value:'hello',focus(){}});
const sandbox={window:{},state,TextDecoder,AbortController,URLSearchParams,
  e:String,$:selector=>nodes.get(selector),renderPage:async()=>{},bootstrap:async()=>{bootstrap++;},
  api:async(url,opts)=>{calls.push([url,opts]);return url==='/api/chat'?response:{};},
  toast:(...args)=>notices.push(args)};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../mikubot/static/experience-chat.js'),'utf8'),sandbox);
const runtime={attachments:[],attachmentScope:'web:default'};
const ui=sandbox.window.MikubotExperienceChat.create(runtime,{});
const frame=event=>'data: '+JSON.stringify(event)+'\n\n';
function stream(events,options={}){
  const raw=new TextEncoder().encode(events.map(frame).join(''));
  let offset=0,cancelled=false,reads=0;
  return {headers:{get:()=> 'text/event-stream; charset=utf-8'},body:{getReader:()=>({
    async read(){
      reads++;if(options.onRead) await options.onRead(reads);
      if(offset>=raw.length)return{done:true};
      const end=Math.min(offset+(options.chunk||3),raw.length),value=raw.slice(offset,end);offset=end;
      return{value,done:false};
    },async cancel(){cancelled=true;},releaseLock(){},get cancelled(){return cancelled;}
  })}};
}
function reset(){calls.length=0;writes.length=0;notices.length=0;state.scope='web:default';state.images=[];state.draft='';state.sending=false;nodes.get('#chat-text').value='hello';}
async function main(){
  response=stream([{type:'start'},{type:'delta',text:'你好<script>private</script>'},{type:'reset'},
    {type:'delta',text:'经过工具整理'},{type:'result',data:{reply:'最终回复'}}]);
  await ui.sendChat();
  assert.equal(calls[0][0],'/api/chat');assert.equal(calls[0][1].body.stream,true);assert.equal(calls[0][1].raw,true);
  assert.ok(writes.includes('你好<script>private</script>'),'split UTF-8 fragments are decoded intact');
  assert.ok(writes.includes('经过工具整理'));assert.equal(writes.at(-1),'最终回复');
  assert.equal(preview.innerHTML,'untouched','preview always uses textContent, never renders model HTML');
  assert.ok(writes.indexOf('')<writes.indexOf('经过工具整理'),'tool draft reset precedes the follow-up answer');
  assert.equal(state.draft,'');assert.equal(state.sending,false);assert.equal(runtime.preview,null);

  reset();response=stream([{type:'delta',text:'unfinished'}]);
  await ui.sendChat();
  assert.equal(calls.filter(([url])=>url==='/api/chat').length,1,'an interrupted accepted generation is never automatically replayed');
  assert.equal(state.draft,'hello','the input survives incomplete transport');
  assert.equal(writes.at(-1),'');assert.ok(notices.some(([text])=>text.includes('传输中断')));
  assert.ok(calls[0][1].signal.aborted);

  reset();response=stream([{type:'delta',text:'old session draft'},{type:'result',data:{reply:'old final'}}],{
    chunk:99999,onRead:()=>{state.scope='web:other';}
  });
  await ui.sendChat();
  assert.ok(!writes.includes('old session draft')&&!writes.includes('old final'),'changing session never paints the old session stream into the new one');

  reset();response={headers:{get:()=> 'application/json'},json:async()=>({reply:'compatible JSON'})};
  await ui.sendChat();assert.equal(state.draft,'');assert.equal(calls.length,1,'JSON compatibility uses the original response');

  reset();response=stream([{type:'delta',text:'will be cleared'},
    {type:'error',code:'authentication_required',message:'登录已失效'}]);
  await ui.sendChat();assert.equal(state.authenticated,false);assert.equal(writes.at(-1),'');
  assert.ok(notices.some(([text])=>text==='登录已失效'));assert.ok(bootstrap>=5);
  process.stdout.write('chat incremental preview passed\n');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
