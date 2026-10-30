"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const scopeId='web:history', records=Array.from({length:1005},(_,i)=>({id:String(i),scope_id:scopeId,role:'user',content:`text-${i}`}));
const state={scope:scopeId,page:'chat',scopes:[{id:scopeId,name:'history'}],settings:{},boot:{},images:[],draft:'',authenticated:true};
const input={value:'',disabled:false,focus(){}},feed={innerHTML:'',scrollHeight:1000,scrollTop:12};
let gate=null,renders=0,posts=0,bootstraps=0,reply=null;
const sandbox={window:{},state,AbortController,URLSearchParams,Map,Set,Date,setTimeout,clearTimeout,
 $:s=>s==='#chat-text'?input:s==='#chat-feed'?feed:null,rows:v=>Array.isArray(v)?v:v.items||[],
 query:v=>new URLSearchParams(v).toString(),scope:()=>({}),e:String,icon:()=>'',personaName:()=>'',toolRunCard:()=>'',
 heading:()=>'',notice:()=>'',button:(title,action)=>`<button data-action="${action}">${title}</button>`,renderMessage:m=>`<p>${m.content}</p>`,toast:()=>{},
 renderPage:async()=>{renders++;throw Error('management unavailable')},bootstrap:async()=>{bootstraps++},
 api:async(url,options={})=>{
  if(url==='/api/chat'){posts++;return reply?reply():{reply:'ok'}}
  const u=new URL(url,'http://localhost');if(u.pathname!=='/api/messages')return [];
  const before=u.searchParams.get('before');if(before&&gate)return gate;
  return records.filter(r=>before===null||Number(r.id)<Number(before)).slice(-Number(u.searchParams.get('limit')));
 }};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/experience-chat.js'),'utf8'),sandbox);
const ui=sandbox.window.ShioriExperienceChat.create({attachments:[],attachmentScope:scopeId},{inactive:()=>true,jobDetail:()=>''});
(async()=>{
 let html=await ui.renderChat();assert.equal(state.messages.length,80);assert.equal(state.messages[0].id,'925');assert.match(html,/chat-history-older/);
 await ui.handleAction('chat-history-older',{});assert.equal(state.messages.length,160);assert.equal(state.messages[0].id,'845');assert.match(feed.innerHTML,/chat-history-latest/);
 assert.equal(ui.refreshChatMessages(records.slice(-81),scopeId),null,'background task completion must not displace historical reading');
 for(let i=0;i<6;i++)await ui.handleAction('chat-history-older',{});assert.equal(state.messages.length,500);
 assert.equal(new Set(state.messages.map(m=>m.id)).size,500);
 for(let i=0;i<6;i++)await ui.handleAction('chat-history-older',{});assert.equal(state.messages[0].id,'0');assert.doesNotMatch(feed.innerHTML,/chat-history-older/);
 await ui.renderChat();let release;gate=new Promise(resolve=>release=resolve);const loading=ui.handleAction('chat-history-older',{});
 state.scope='web:other';state.messages=[{id:'other'}];release(records.slice(800,881));await loading;assert.equal(state.messages[0].id,'other');gate=null;
 // Sending has no dependency on rendering the page or its management APIs.
 state.scope=scopeId;input.value='hello';await ui.sendChat();assert.equal(posts,1);assert.equal(renders,0);
 // Finishing a Chat request while editing another page must not bootstrap it.
 input.value='second';reply=async()=>{state.page='persona';return{reply:'ok'}};const before=bootstraps;await ui.sendChat();assert.equal(posts,2);assert.equal(bootstraps,before);
 console.log('Chat: full cursor history, bounded window, stale results, immediate dispatch and settings-page preservation passed');
})().catch(e=>{console.error(e);process.exitCode=1});
