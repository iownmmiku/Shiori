"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const calls=[];
const sandbox={window:{},state:{scope:'web:default',settings:{output:{}},personas:[]},e:escape,scope:()=>({persona_id:'default'}),personaName:()=> '初音未来',fmt:()=>'',imageURL:x=>x,
  icon:()=>'',tag:escape,getForm:form=>form.values,toast:()=>{},bootstrap:async()=>{},
  api:async(url,options)=>{calls.push([url,options]);return {};},
  field:(name,title,value,type,hint)=>`<input name="${name}" value="${escape(value)}">${escape(title)}${escape(hint)}`,
  toggle:(name,title,hint,checked)=>`<input type="checkbox" name="${name}" ${checked?'checked':''}>${escape(title)}${escape(hint)}`,
  select:(name,title,options,current)=>`<select name="${name}">${options.map(([value,label])=>`<option value="${value}" ${value===current?'selected':''}>${escape(label)}</option>`)}</select>`,
  closeModal:()=>{},splitNames:()=>[],scopeModalBase:()=>{}};
vm.createContext(sandbox);
for(const name of ['workspace-features.js','settings-page.js','chat-page.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static',name),'utf8'),sandbox,{filename:name});
async function main(){
  const html=sandbox.window.ShioriFeatures.outputSection();assert.match(html,/value="model" selected/);assert.match(html,/模型在生成时决定/);assert.match(html,/<details[^>]*>.*旧版长度参数/s);
  assert.match(html,/name="reply_max_chars" value="0"/);assert.match(html,/name="message_max_chars" value="0"/);
  assert.match(html,/删除括号及其中内容/);assert.match(html,/均衡合并相邻/);assert.match(html,/代码和链接不会截成残缺片段/);
  await sandbox.saveSettings('output',{values:{enabled:false,mode:'model',reply_max_chars:'600',message_max_chars:'100',remove_parentheses:true,target_chars:'160',max_chars:'320',interval_ms:'600',max_parts:'8',platform_max_chars:'1800'}});
  assert.equal(calls[0][0],'/api/settings');assert.equal(calls[0][1].body.output.mode,'model');
  assert.equal(calls[0][1].body.output.reply_max_chars,600);assert.equal(calls[0][1].body.output.message_max_chars,100);assert.equal(calls[0][1].body.output.remove_parentheses,true);assert.equal(calls[0][1].body.output.enabled,false);
  sandbox.state.settings.output={reply_max_chars:800,message_max_chars:200,remove_parentheses:true,enabled:false};
  let scopeHTML='';const body={insertAdjacentHTML:(_position,html)=>{scopeHTML+=html;}};
  sandbox.$=selector=>selector==='#modal .modal-body'?body:null;
  sandbox.window.ShioriFeatures.scopeModal({id:'web:default',output:{message_max_chars:80,reply_max_chars:null}});
  assert.match(scopeHTML,/name="out_reply_max_chars" value="800"/);assert.match(scopeHTML,/name="out_message_max_chars" value="80"/);
  assert.match(scopeHTML,/name="out_remove_parentheses" checked/);
  const appSource=fs.readFileSync(path.join(__dirname,'../shiori/static/app.js'),'utf8');
  vm.runInContext(appSource.slice(appSource.indexOf('async function saveModal('),appSource.indexOf('function modelPayload(')),sandbox,{filename:'app-scope-output.js'});
  sandbox.state.modalRecord={id:'web:default',output_reply_max_chars:30,settings:{output:{message_max_chars:20},output_remove_parentheses:true,unrelated:true}};
  const values={output_override:true,out_enabled:false,out_mode:'model',out_reply_max_chars:'500',out_message_max_chars:'90',out_remove_parentheses:true,out_target_chars:'160',out_max_chars:'320',out_interval_ms:'600',out_max_parts:'8',out_platform_max_chars:'1800'};
  await sandbox.saveModal('scope',{values});
  let payload=calls.at(-1)[1].body;assert.equal(payload.output.reply_max_chars,500);assert.equal(payload.output.message_max_chars,90);assert.equal(payload.output.remove_parentheses,true);assert.equal(payload.output.enabled,false);
  assert.equal(payload.output_reply_max_chars,null);assert.equal(payload.settings.output,null);assert.equal(payload.settings.output_remove_parentheses,null);assert.ok(!('unrelated' in payload.settings));assert.ok(!('out_reply_max_chars' in payload));assert.ok(!('output_override' in payload));
  await sandbox.saveModal('scope',{values:{...values,output_override:false}});
  payload=calls.at(-1)[1].body;assert.equal(payload.output,null);assert.equal(payload.output_reply_max_chars,null);assert.equal(payload.settings.output,null);
  const record={id:'fixture',role:'assistant',content:'嗯。\n\n<script>不执行</script>\n这是同一条',metadata:{reply_parts:['嗯。','<script>不执行</script>\n这是同一条']}};
  let rendered=sandbox.renderMessageBase(record);assert.equal((rendered.match(/class="message-content"/g)||[]).length,2);assert.ok(!rendered.includes('<script>'));assert.match(rendered,/&lt;script&gt;/);
  rendered=sandbox.renderMessageBase({...record,metadata:{reply_parts:['不属于原文']}});assert.equal((rendered.match(/class="message-content"/g)||[]).length,1);assert.ok(!rendered.includes('不属于原文'));
  rendered=sandbox.renderMessageBase({...record,role:'user'});assert.equal((rendered.match(/class="message-content"/g)||[]).length,1);
  console.log('model output settings, legacy foldout, saved boundaries, escaping and canonical-text checks passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
