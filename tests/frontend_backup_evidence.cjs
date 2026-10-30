"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=name=>fs.readFileSync(path.join(__dirname,'../shiori/static',name),'utf8');
const escape=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
(async()=>{
  const calls=[],state={scope:'qq:group:1',renderId:1,authenticated:true},dialog={open:true};let markup='',error='',pending=null;
  const node={set outerHTML(value){markup=value;}};
  const sandbox={window:{},state,e:escape,rows:x=>x||[],fmt:String,label:String,query:o=>new URLSearchParams(o).toString(),
    $:selector=>selector==='#modal'?dialog:selector==='#backup-progress' && markup.includes('backup-progress')?node:null,
    button:(label,action,style,icon,extra='')=>`<button data-action="${action}" ${extra}>${label}</button>`,
    openModal:(title,description,body,kind='',record=null)=>{markup=body;state.modalRecord=record;dialog.open=true;},
    modalError:message=>{error=message;},setTimeout:()=>1,clearTimeout(){},
    api:async(url,options)=>{calls.push({url,options});if(pending)return pending;return {id:'a'.repeat(32),status:'running',progress:30,can_cancel:true,message:'正在处理'};}};
  vm.createContext(sandbox);vm.runInContext(read('backup-ui.js'),sandbox);
  const backup=sandbox.window.ShioriBackup;
  await backup.handleAction('download-backup');
  assert.equal(calls.length,1,'reopen resumes existing job, not another export');
  assert.match(markup,/取消备份/);
  await backup.handleAction('backup-cancel');assert.match(calls.at(-1).url,/\/cancel$/);
  sandbox.api=async(url,options)=>{calls.push({url,options});return {id:'b'.repeat(32),status:'completed',size:123,progress:100,message:'完成'};};
  await backup.handleAction('backup-status');assert.match(markup,/\/api\/backup\/jobs\/b{32}\/file/);
  assert.match(markup,/data-action="backup-start"/);assert.match(markup,/0\.1 KiB/);
  await backup.handleAction('backup-start');assert.equal(calls.at(-1).url,'/api/backup/jobs');assert.equal(calls.at(-1).options.method,'POST');
  let resolve;pending=new Promise(done=>{resolve=done;});sandbox.api=()=>pending;
  const opening=backup.handleAction('download-backup');dialog.open=false;const before=markup;
  resolve({status:'completed',id:'c'.repeat(32)});await opening;assert.equal(markup,before,'closed modal is not overwritten');
  backup.close();pending=null;

  const source=read('app.js');vm.runInContext(source.slice(source.indexOf('async function showJargonEvidence('),source.indexOf('\nasync function saveModal(')),sandbox);
  sandbox.api=async(url)=>{calls.push({url});return {total:51,items:[{id:'old',status:'available',message:{user_id:'u',created_at:'2026',content:'<script>old</script>'}},{id:'gone',status:'missing'},{id:'other',status:'unavailable'}],has_more:true,next_offset:50};};
  const item={id:'term/one',scope_id:state.scope,term:'测试',meaning:'含义',status:'approved'};
  await sandbox.showJargonEvidence(item);
  assert.match(calls.at(-1).url,/\/api\/jargon\/term%2Fone\/evidence/);
  assert.match(markup,/&lt;script&gt;old&lt;\/script&gt;/);assert.match(markup,/已删除或不存在/);assert.match(markup,/不属于此词条/);assert.match(markup,/data-offset="50"/);
  let finish;sandbox.api=()=>new Promise(done=>finish=done);const old=sandbox.showJargonEvidence(item);
  state.modalRecord={id:'different'};markup='new modal';finish({items:[],total:0});await old;assert.equal(markup,'new modal');
  console.log('Backup job progress, cancellation, reopen, stale modal and canonical evidence checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
