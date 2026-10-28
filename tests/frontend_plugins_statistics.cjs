"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const calls=[],fallback=[],timers=new Map();let serial=0,modal;
const plugin={id:'example_plugin',name:'<script>test</script>',description:'safe',revision:1,enabled:false,code:'def run(input):\n    return input'};
const metrics={timezone:'UTC',days:1,online_platforms:1,messages_total:3,period_messages:3,tokens_today:10,process:{memory_bytes:null,cpu_percent:null,uptime_seconds:2},usage:{unknown_price_calls:1},platforms:[{platform:'web',messages:3}],models:[{model:'<img src=x>',provider_id:'fixture',calls:1,tokens:10}],trend:[{label:'01-01 00:00',messages:3,tokens:10}]};
const box={innerHTML:''};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false,handleSubmit:async(kind,form)=>{fallback.push([kind,form]);return false;}}},state:{scope:'web:default',page:'statistics',authenticated:true},
  document:{hidden:false},$:selector=>selector==='#statistics-root'?box:null,e:escape,heading:(a,b,c)=>a+b+c,
  button:(text,action,style,symbol,attrs='')=>`<button data-action="${action}" ${attrs}>${escape(text)}</button>`,
  field:(n,t,v)=>`<input name="${n}" value="${escape(v)}">`,textarea:(n,t,v)=>`<textarea name="${n}">${escape(v)}</textarea>`,
  FormData:class{constructor(form){this.values=form.values;}entries(){return Object.entries(this.values)[Symbol.iterator]();}[Symbol.iterator](){return this.entries();}get(key){return this.values[key];}},
  api:async(url,options)=>{calls.push([url,options]);if(url==='/api/plugins')return {items:[plugin],runtime:{enabled:false,runtime:'docker'}};if(url==='/api/plugins/example_plugin')return plugin;if(url.endsWith('/run'))return {id:'job1'};return {...metrics,days:Number(url.split('days=')[1]||1)};},
  openModal:(...args)=>{modal=args;},closeModal:()=>{},toast:()=>{},confirmAction:async(t,d,fn)=>fn(),renderPage:async()=>{},navigate:async page=>{sandbox.state.page=page;},
  setTimeout:fn=>{const id=++serial;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id)};
vm.createContext(sandbox);
for(const name of ['statistics.js','plugins.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static',name),'utf8'),sandbox,{filename:name});
async function main(){
  const html=await sandbox.window.ShioriPlugins.render();assert.ok(!html.includes('<script>'));assert.match(html,/&lt;script&gt;/);
  const actions=sandbox.window.ShioriExperience;
  await actions.handleAction('plugin-edit',{dataset:{id:plugin.id}});assert.match(modal[2],/data-form="plugin-editor"/);
  await actions.handleSubmit('plugin-editor',{values:{id:plugin.id,name:'new',description:'safe',code:plugin.code}});
  assert.equal(calls.at(-1)[0],'/api/plugins');assert.equal(calls.at(-1)[1].body.name,'new');
  await actions.handleSubmit('chat',{values:{}});assert.equal(fallback.at(-1)[0],'chat');
  await actions.handleAction('plugin-enable',{dataset:{id:plugin.id,enabled:'true'}});assert.equal(calls.at(-1)[1].body.enabled,true);
  await actions.handleSubmit('plugin-run',{dataset:{id:plugin.id},values:{input:'{"test":1}'}});assert.equal(calls.at(-1)[1].body.input.test,1);assert.equal(sandbox.state.page,'jobs');
  let stats=await sandbox.window.ShioriStatistics.render();assert.match(stats,/采样中/);assert.match(stats,/&lt;img src=x&gt;/);assert.match(stats,/首个有效内容延迟：未采集/);assert.match(stats,/待核实消费/);
  await actions.handleAction('statistics-period',{dataset:{days:'7'}});assert.equal(sandbox.state.statisticsDays,7);
  sandbox.state.page='statistics';sandbox.window.ShioriStatistics.afterRender();assert.equal(timers.size,1);
  sandbox.state.page='chat';sandbox.window.ShioriStatistics.afterRender();assert.equal(timers.size,0);
  const index=fs.readFileSync(path.join(__dirname,'../shiori/static/index.html'),'utf8');
  const scripts=[...index.matchAll(/<script[^>]*src="\/static\/([^?"]+)[^>]*>([\s\S]*?)<\/script>/g)];
  assert.equal(scripts.length,(index.match(/<script /g)||[]).length,'every external script has a matching closing tag');
  for(const script of scripts){assert.equal(script[2].trim(),'');assert.ok(fs.existsSync(path.join(__dirname,'../shiori/static',script[1])));}
  console.log('plugin CRUD dispatch, statistics unknown metrics, polling and HTML script boundaries passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
