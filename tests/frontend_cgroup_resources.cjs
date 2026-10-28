"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const escape=value=>String(value??"").replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const data={timezone:"UTC",days:1,process:{},usage:{},platforms:[],models:[],trend:[{label:"00:00",messages:0,tokens:0}],
  resources:{available:true,cgroup_version:1,memory_bytes:null,memory_limit_bytes:1048576,memory_limit_hits:2,cpu_usage_seconds:1,cpu_percent:null,cpu_limit:.5,oom_kills:null},
  connector_resources:{available:false,message:"<script>external</script>"},
  memory_indexing:{completed:3,failed:1,skipped:2,queued_batches:4,pending_chunks:5,running:true}};
const sandbox={window:{ShioriExperience:{handleAction:async()=>false}},state:{},e:escape,api:async()=>data,heading:()=>"",button:()=>""};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static/statistics.js"),"utf8"),sandbox);
(async()=>{
 let html=await sandbox.window.ShioriStatistics.render();
 assert.match(html,/cgroup v1/);assert.match(html,/内存 未采集/);assert.match(html,/资源组 CPU 采样中/);
 assert.match(html,/不等同于OOM终止/);assert.match(html,/&lt;script&gt;external/);assert.ok(!html.includes("<script>"));
 assert.match(html,/记忆向量后台索引/);assert.match(html,/正在处理 · 排队 4 批 \/ 5 个片段/);assert.match(html,/仅统计当前shiori进程/);
 data.resources.cpu_percent=50;data.resources.cpu_usage_cores=.25;data.resources.memory_pressure=true;
 html=await sandbox.window.ShioriStatistics.render();assert.match(html,/资源组 CPU 50.0%/);assert.match(html,/平均使用 0.25 核/);assert.match(html,/资源组内存接近限额/);
 data.resources.available=false;
 html=await sandbox.window.ShioriStatistics.render();assert.ok(!html.includes("cgroup v1"));assert.match(html,/QQ连接器资源/);
 console.log("cgroup usage samples, unknown values and connector boundaries rendered safely");
})().catch(error=>{console.error(error);process.exitCode=1;});
