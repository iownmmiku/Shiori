"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.join(__dirname,'../shiori/static');
const source=name=>fs.readFileSync(path.join(root,name),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function setup(){
  const nodes=[], theme={id:'theme'};
  const document={currentScript:{src:'https://local.invalid/static/asset-loader.js?v=0.2.1-beta&brand=shiori'},
    createElement:tag=>({tag,remove(){this.removed=true;}}),
    querySelector:()=>theme,
    head:{appendChild(node){nodes.push(node);},insertBefore(node,anchor){assert.equal(anchor,theme);node.beforeTheme=true;nodes.push(node);}}};
  const fallback=[];
  const window={ShioriExperience:{handleAction:async action=>{fallback.push(action);return false;},handleSubmit:async()=>false}};
  const sandbox={window,document,URL,Map,Object,Error,setTimeout,clearTimeout,console};vm.createContext(sandbox);
  vm.runInContext(source('asset-loader.js'),sandbox);
  const complete=node=>{if(node.tag==='script')vm.runInContext(source(node.src.split('/').at(-1).split('?')[0]),sandbox);node.onload();};
  return {nodes,window,sandbox,complete,fallback};
}

async function loadAndFailure(){
  const {nodes,window,complete,fallback}=setup();
  await window.ShioriAssets.ensure('chat');await window.ShioriAssets.ensure('__proto__');
  await window.ShioriAssets.ensure('https://evil.invalid/code.js');
  assert.equal(nodes.length,0,'unlisted pages cannot load URLs');
  const first=window.ShioriAssets.ensure('statistics'),second=window.ShioriAssets.ensure('statistics');
  assert.equal(nodes.length,1,'concurrent loads share one CSS request');assert.equal(nodes[0].beforeTheme,true);
  complete(nodes[0]);await tick();assert.equal(nodes.length,2);
  complete(nodes[1]);await Promise.all([first,second]);
  assert.ok(window.ShioriStatistics);await window.ShioriAssets.ensure('statistics');assert.equal(nodes.length,2);
  // Run the real shipped wrapper after lazy load: unrelated dispatch is retained.
  await window.ShioriExperience.handleAction('unrelated',{dataset:{}});assert.deepEqual(fallback,['unrelated']);

  const failed=window.ShioriAssets.ensure('characterSchedule');
  const rejection=assert.rejects(failed,/加载失败/);nodes[2].onerror();await rejection;
  assert.equal(nodes[2].removed,true);
  assert.equal(nodes.length,3,'a failed stylesheet must not start its controller');
  const retry=window.ShioriAssets.ensure('characterSchedule');complete(nodes[3]);await tick();complete(nodes[4]);await retry;
  assert.ok(window.ShioriCharacterSchedule);assert.ok(!nodes[3].beforeTheme,'schedule CSS keeps its prior order after the theme');

  const pluginFail=window.ShioriAssets.ensure('plugins');const rejected=assert.rejects(pluginFail,/加载失败/);
  nodes[5].onerror();await rejected;
  const pluginRetry=window.ShioriAssets.ensure('plugins');complete(nodes[6]);await pluginRetry;
  assert.ok(window.ShioriPlugins);await window.ShioriExperience.handleAction('unrelated-two',{dataset:{}});
  assert.deepEqual(fallback,['unrelated','unrelated-two'],'both lazily installed wrappers retain the existing handler chain');
}

async function navigationRace(){
  const app=source('app.js'),main={innerHTML:''},renders=[],pending=[];
  const sandbox={state:{page:'statistics',renderId:0},window:{},pageNames:{statistics:'统计',plugins:'插件'},
    $:()=>main,e:value=>value,heading:()=>'',button:()=>'',updateAdapterFields:()=>{},renderImagePreviews:()=>{}};
  const noop=()=>{};
  for(const name of ['renderMemory','renderCulture','renderKnowledge','renderPersonaKnowledge','renderTasks','renderTools','renderGroups','renderDiagnostics'])sandbox[name]=noop;
  sandbox.window={ShioriAssets:{ensure:page=>new Promise((resolve,reject)=>pending.push({page,resolve,reject}))},
    ShioriExperience:{renderOverview:noop,renderChat:noop,renderPersonas:noop,renderSettings:noop,renderJobs:noop,renderHelp:noop,afterRender:noop},
    ShioriPages:{renderModels:noop,renderLogs:noop,afterRender:noop},ShioriFeatures:{afterRender:noop},
    ShioriStatistics:{render:async()=>{renders.push('statistics');return 'OLD';},afterRender:noop},
    ShioriPlugins:{render:async()=>{renders.push('plugins');return 'NEW';}}};
  vm.createContext(sandbox);vm.runInContext(app.slice(app.indexOf('async function renderPage()'),app.indexOf('function imageURL(')),sandbox);
  const old=sandbox.renderPage();sandbox.state.page='plugins';const current=sandbox.renderPage();
  pending[1].resolve();await current;pending[0].resolve();await old;
  assert.deepEqual(renders,['plugins'],'stale lazy-load completion must not render or fetch the old page');
  assert.match(main.innerHTML,/NEW/);assert.ok(!main.innerHTML.includes('OLD'));
  const failed=sandbox.renderPage();pending[2].reject(new Error('offline'));await failed;assert.match(main.innerHTML,/offline/);
  const retried=sandbox.renderPage();pending[3].resolve();await retried;assert.match(main.innerHTML,/NEW/);
}

(async()=>{
  await loadAndFailure();await navigationRace();
  const index=source('index.html');assert.match(index,/asset-loader\.js\?v=/);
  for(const file of ['statistics.js','statistics.css','plugins.js','character-schedule.js','character-schedule.css'])
    assert.ok(!index.includes('/static/'+file+'?'),file+' should not be requested at startup');
  console.log('Lazy assets: allowlist, shared loads, real handler chains, theme order, retry and navigation races passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
