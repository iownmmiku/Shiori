"use strict";
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const calls=[];
const sandbox={window:{ShioriExperience:{renderPersonas:async()=>'',renderSettings:async()=>'',handleAction:async()=>false}},
  state:{botTab:'personas',scopes:[{id:'qq:group:456',enabled:true},{id:'web:default',enabled:true}],settings:{onebot:{chat_policy:'blacklist',blocked_groups:[]}}},
  api:async(...args)=>{calls.push(args);return {};},bootstrap:async()=>calls.push(['bootstrap']),toast:()=>{},
  navigate:async page=>calls.push(['navigate',page]),renderPage:async()=>calls.push(['render'])};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/bot-config.js'),'utf8'),sandbox);
const action=sandbox.window.ShioriExperience.handleAction;
const plain=value=>JSON.parse(JSON.stringify(value));
async function main(){
  assert.equal(await action('bot-default',{}),true);
  assert.equal(sandbox.state.botTab,'basic','default config shortcut must leave a previous persona-library tab');
  assert.deepEqual(calls.pop(),['navigate','persona']);
  calls.length=0;
  await action('scope-inherit',{dataset:{id:'qq:group:456'}});
  assert.deepEqual(plain(calls[0]),['/api/scopes/qq%3Agroup%3A456/inherit',{method:'POST',body:{}}]);
  calls.length=0;
  await action('scope-admission',{dataset:{id:'qq:group:456',blocked:'false'}});
  assert.deepEqual(plain(calls[0]),['/api/settings',{method:'PUT',body:{onebot:{blocked_groups:['456']}}}]);
  assert.deepEqual(plain(calls[1][1].body),{enabled:false});
  assert.ok(!('access' in calls[0][1].body),'chat admission cannot grant administrator identity');
  sandbox.state.settings.onebot={chat_policy:'whitelist',allowed_groups:[]};
  calls.length=0;
  await action('scope-admission',{dataset:{id:'qq:group:456',blocked:'true'}});
  assert.deepEqual(plain(calls[0][1].body),{onebot:{allowed_groups:['456']}});
  assert.deepEqual(plain(calls[1][1].body),{enabled:true});
  calls.length=0;
  await action('scope-admission',{dataset:{id:'web:default',blocked:'false'}});
  assert.equal(calls[0][0],'/api/scopes/web%3Adefault');
  assert.ok(!calls.some(c=>c[0]==='/api/settings'),'local chat uses its switch, not QQ lists');
  assert.equal(await action('unrelated',{}),false);
  console.log('bot configuration navigation, inheritance and blacklist/whitelist actions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
