"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const sandbox = {state:{scope:"web:default"},e:escape,scope:()=>({persona_id:"default"}),personaName:()=>"初音未来",fmt:()=>"",icon:()=>"",imageURL:value=>value,button:(text,action,style,icon,attrs)=>`<button data-action="${action}" ${attrs}>${escape(text)}</button>`};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static/chat-page.js"),"utf8"),sandbox,{filename:"chat-page.js"});
const record = {id:"reply-1",role:"assistant",content:"保留第一段。\n\n保留第二段。",metadata:{reply_parts:["保留第一段。","保留第二段。"],
  original_reply:'保留第一段。（动作）\n\n保留第二段。\n<img src=x onerror="steal()">尾句。',
  output_adjustment:{limited:true,removed_parentheses:true},memory_ids:["memory-1"],expression_ids:["expression-1"]}};
let html = sandbox.renderMessage(record);
assert.match(html,/<details class="message-output-original"><summary>查看原文<\/summary>/);
assert.ok(!/<details[^>]*\bopen\b/.test(html),"Original text must start collapsed");
assert.match(html,/已应用字数限制 · 已去除括号内容/);
assert.ok(html.indexOf("已应用字数限制") < html.indexOf('<details class="message-output-original">'),"Adjustment notice should remain visible while the original is collapsed");
assert.ok(!html.includes('<img src=x'),"Original reply must never be interpreted as HTML");
assert.ok(html.includes('&lt;img src=x onerror=&quot;steal()&quot;&gt;'));
assert.equal((html.match(/class="message-content"/g)||[]).length,3,"Canonical reply boundaries must remain intact");
assert.match(html,/data-action="message-sources"/);
assert.match(html,/data-action="expression-sources"/);
assert.ok(html.endsWith("</div></article>"));
html = sandbox.renderMessageBase({...record,metadata:{...record.metadata,output_adjustment:{limited:true}}});
assert.match(html,/已应用字数限制/);assert.ok(!html.includes("已去除括号内容"));
html = sandbox.renderMessageBase({...record,metadata:{...record.metadata,output_adjustment:{removed_parentheses:true}}});
assert.match(html,/已去除括号内容/);assert.ok(!html.includes("已应用字数限制"));
for (const original_reply of [undefined,null,false,0,[],{},"","   ",record.content]) {
  html = sandbox.renderMessageBase({...record,metadata:{...record.metadata,original_reply}});
  assert.ok(!html.includes("message-output-original"),"Only a different, nonempty original string should display");
}
for (const role of ["user","tool","system"]) {
  html = sandbox.renderMessageBase({...record,role});
  assert.ok(!html.includes("message-output-original"),"Only AI assistant records expose original replies");
}
html = sandbox.renderMessageBase({...record,metadata:{reply_parts:["不匹配的旧分段"],original_reply:"旧原文",output_adjustment:{limited:"true"}}});
assert.equal((html.match(/class="message-content"/g)||[]).length,2);
assert.ok(!html.includes("不匹配的旧分段"));assert.ok(!html.includes("已应用字数限制"));
assert.match(html,/旧原文/);
html=sandbox.renderMessageBase({id:"qq-image",role:"user",content:"",metadata:{images:["onebot-image:secret-account:private-file"]}});
assert.match(html,/QQ图片（回复时读取）/);
assert.doesNotMatch(html,/secret-account|private-file|onebot-image:|<img|href=/);
console.log("original reply foldout, default collapse, safe escaping, canonical paragraphs and historical source controls passed");

html=sandbox.renderMessageBase({...record,metadata:{job_ids:["inline-1","inline-1","<script>bad</script>"],job_id:"inline-2"}});
assert.equal((html.match(/data-action="view-job"/g)||[]).length,2);assert.match(html,/data-id="inline-1"/);assert.ok(!html.includes("<script>bad</script>"));
