"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const node={innerHTML:''},calls=[];let confirm=null,polls=0;
const release={id:12,tag:'v0.1.2',name:'Release <script>x</script>',notes:'<img src=x onerror=bad()>',published_at:'2026-10-19',relation:'newer',prerelease:false,
  url:'https://github.com/iownmmiku/Shiori/releases/tag/v0.1.2',assets:{installer:{name:'setup.exe',size:123,verified_download_available:true},source:{name:'source.zip',size:20,verified_download_available:true}}};
let result={current_version:'0.1.1',mode:'installed',default_kind:'installer',install_supported:true,local_install_allowed:true,
  releases:[],download:{status:'idle'},releases_url:'https://github.com/iownmmiku/Shiori/releases',page:1,include_prerelease:false};
let fail=false;
const sandbox={window:{},state:{page:'settings',authenticated:true},document:{hidden:false},e:esc,fmt:String,icon:()=>'',notice:text=>esc(text),
  button:(label,action,style,symbol,extra='')=>`<button data-action="${action}" ${extra}>${esc(label)}</button>`,
  select:(name,label,options,value)=>`<select name="${name}" aria-label="${label}">${options.map(([id,text])=>`<option value="${id}" ${id===value?'selected':''}>${esc(text)}</option>`).join('')}</select>`,
  $:selector=>selector==='#updates-section'?node:null,toast(){},openModal:(title,description,html)=>{node.innerHTML=html;},setTimeout:()=>++polls,clearTimeout(){},
  confirmAction:async(a,b,callback)=>{confirm={a,b,callback};},
  api:async(url,options)=>{calls.push({url,options});if(fail)throw Error('network failed');return structuredClone(result);}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/updates.js'),'utf8'),sandbox);
(async()=>{
  const ui=sandbox.window.ShioriUpdates;
  assert.match(await ui.settingsPanel(),/0\.1\.1/);assert.deepEqual(calls.map(x=>x.url),['/api/updates'],'render does not contact GitHub');
  result={...result,releases:[release],checked_at:'2026-10-19'};
  await ui.handleAction('update-check',{});
  assert.equal(calls.at(-1).url,'/api/updates/check');assert.equal(calls.at(-1).options.body.include_prerelease,false);
  assert.ok(node.innerHTML.includes('&lt;script&gt;') && node.innerHTML.includes('&lt;img'));
  assert.ok(!node.innerHTML.includes('<script>') && !node.innerHTML.includes('<img'));
  assert.match(node.innerHTML,/data-action="update-apply"/);
  const applyBefore=calls.length;await ui.handleAction('update-apply',{});
  assert.equal(calls.length,applyBefore,'automatic update still waits for explicit product confirmation');
  await confirm.callback();assert.equal(calls.at(-1).url,'/api/updates/apply');assert.equal(calls.at(-1).options.body.release_id,12);
  await ui.handleChange({name:'update-kind',value:'source'});
  await ui.handleAction('update-download',{});
  assert.equal(calls.at(-1).options.body.kind,'source');assert.equal(calls.at(-1).options.body.release_id,12);
  result={...result,download:{status:'ready',id:'verified',kind:'installer',name:'setup.exe',file_url:'/api/updates/files/verified',can_install:true}};
  await ui.handleAction('update-status',{});
  const before=calls.length;await ui.handleAction('update-install',{});
  assert.equal(calls.length,before,'install waits for explicit product confirmation');
  await confirm.callback();assert.equal(calls.at(-1).url,'/api/updates/install');assert.equal(sandbox.state.stopped,true);
  result.local_install_allowed=false;sandbox.state.stopped=false;
  await ui.handleAction('update-status',{});
  assert.ok(!node.innerHTML.includes('data-action="update-install"'));
  await assert.rejects(ui.handleAction('update-install',{}),/运行电脑/);
  fail=true;await ui.handleAction('update-check',{});assert.match(node.innerHTML,/network failed/);
  fail=false;result.mode='source';result.download.can_install=false;
  await ui.handleAction('update-status',{});assert.match(node.innerHTML,/未提交改动不会被覆盖/);
  await ui.handleChange({id:'update-prerelease',checked:true});assert.equal(calls.at(-1).options.body.include_prerelease,true);
  result={...result,switch_supported:true,switch_allowed:true,mode:'source',install_result:{},download:{status:'idle'}};
  await ui.handleAction('update-status',{});assert.match(node.innerHTML,/data-action="update-switch"/);
  assert.ok(!node.innerHTML.includes('data-action="update-apply"'));
  const switchBefore=calls.length;await ui.handleAction('update-switch',{});assert.equal(calls.length,switchBefore);
  await confirm.callback();assert.equal(calls.at(-1).url,'/api/updates/switch');assert.equal(calls.at(-1).options.body.release_id,12);
  result={items:[{id:'a'.repeat(32),fingerprint:'exact-preview',tag:'<bad>',bytes:1024,backup_bytes:0,can_delete:true},{id:'b'.repeat(32),bytes:512,backup_bytes:512,reason:'最新恢复备份',can_delete:false}],cleanup_allowed:true,total_bytes:1536,reclaimable_bytes:1024};
  await ui.handleAction('update-storage',{});
  assert.match(node.innerHTML,/&lt;bad&gt;/);assert.match(node.innerHTML,/最新恢复备份/);
  const cleanupBefore=calls.length;await ui.handleAction('update-cleanup',{dataset:{id:'a'.repeat(32)}});
  assert.equal(calls.length,cleanupBefore,'cleanup requires review of exact historical artifact');
  result={released_bytes:1024};await confirm.callback();
  assert.equal(calls.at(-1).url,'/api/updates/storage/cleanup');
  assert.equal(calls.at(-1).options.body.items[0].fingerprint,'exact-preview');
  ui.close();console.log('Update UI selection, escaping, direct switch, storage cleanup and explicit install checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
