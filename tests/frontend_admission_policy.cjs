"use strict";
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const calls=[],e=value=>String(value??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const sandbox={state:{settings:{onebot:{},qq_official:{}}},e,
  select:(name,title,options,value)=>`<label>${e(title)}<select name="${name}">${options.map(([v,t])=>`<option value="${v}" ${v===value?'selected':''}>${e(t)}</option>`).join('')}</select></label>`,
  textarea:(name,title,value,hint)=>`<label>${e(title)}<textarea name="${name}">${e(value)}</textarea><small>${e(hint)}</small></label>`,
  getForm:form=>form.values,splitIds:value=>String(value||'').split(/[\s,，;；]+/).filter(Boolean),
  api:async(...args)=>{calls.push(args);return {};},toast:()=>{},bootstrap:async()=>{}};
vm.createContext(sandbox);
const source=fs.readFileSync(path.join(__dirname,'../mikubot/static/settings-page.js'),'utf8');vm.runInContext(source,sandbox);
const plain=value=>JSON.parse(JSON.stringify(value));
function hidden(html,mode){return /\bhidden\b/.test(html.match(new RegExp(`data-admission-mode="${mode}"([^>]*)>`))[1]);}
async function main(){
  for(const [section,prefix] of [['onebot','onebot'],['qq_official','official']]){
    let html=sandbox.chatAdmissionPanel(section,prefix);
    assert.match(html,/value="blacklist" selected/);assert.equal(hidden(html,'blacklist'),false);assert.equal(hidden(html,'whitelist'),true);
    assert.match(html,/当前为黑名单/);assert.match(html,new RegExp(`name="${prefix}_blocked_groups"`));
    sandbox.state.settings[section]={chat_policy:'whitelist',allowed_users:['123'],allowed_groups:['456']};
    html=sandbox.chatAdmissionPanel(section,prefix);
    assert.match(html,/value="whitelist" selected/);assert.equal(hidden(html,'whitelist'),false);assert.equal(hidden(html,'blacklist'),true);
    assert.match(html,/当前为白名单/);assert.match(html,/>123<\/textarea>/);assert.match(html,/>456<\/textarea>/);
  }
  const blocks=['blacklist','whitelist'].map(mode=>({dataset:{admissionMode:mode},hidden:mode==='blacklist'})),summary={textContent:''};
  const panel={querySelectorAll:()=>blocks,querySelector:()=>summary};
  assert.equal(sandbox.updateAdmissionPolicy({name:'onebot_chat_policy',value:'blacklist',closest:()=>panel}),true);
  assert.equal(blocks[0].hidden,false);assert.equal(blocks[1].hidden,true);assert.match(summary.textContent,/空名单不限制/);
  assert.equal(sandbox.updateAdmissionPolicy({name:'unrelated'}),false);
  for(const policy of ['blacklist','whitelist']){
    calls.length=0;
    await sandbox.saveSettings('qq',{values:{adapter:'snowluma',enabled:true,ws_url:'',owner_ids:'123',allowed_groups:'456',
      onebot_chat_policy:policy,onebot_blocked_users:'777',onebot_blocked_groups:'888',
      official_chat_policy:'blacklist',official_allowed_groups:'',allowed_users:'',
      onebot_max_message_chars:'0',official_max_message_chars:'0',app_id:''}});
    const payload=plain(calls[0][1].body);assert.equal(payload.onebot.chat_policy,policy);
    assert.deepEqual(payload.onebot.blocked_groups,['888']);assert.deepEqual(payload.onebot.allowed_groups,['456']);
    assert.ok(!('access' in payload));assert.ok(!('access_token' in payload.onebot));
  }
  sandbox.state.settings={onebot:{},qq_official:{}};calls.length=0;
  await sandbox.saveSettings('qq',{values:{adapter:'napcat',enabled:false,onebot_max_message_chars:0,official_max_message_chars:0}});
  assert.equal(calls[0][1].body.onebot.chat_policy,'blacklist');assert.equal(calls[0][1].body.qq_official.chat_policy,'blacklist');
  assert.match(source,/\$\{chatAdmissionPanel\("onebot","onebot"\)\}/);assert.match(source,/\$\{chatAdmissionPanel\("qq_official","official"\)\}/);
  console.log('QQ policy rendering, switching, legacy preservation and saved admission verified');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
