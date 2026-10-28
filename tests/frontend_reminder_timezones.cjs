"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
let modal;
const state={settings:{time:{timezone:"Asia/Tokyo"}}};
const sandbox={state,Date,Intl,
  openModal:(...args)=>{modal=args;},scopesField:()=>"",
  field:(name,title,value,type,help)=>`<input name="${name}" value="${value ?? ""}"><small>${help}</small>`,
  select:()=>""
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static/tasks-page.js"),"utf8"),sandbox);
assert.equal(sandbox.toLocal("2026-11-01T14:00:00Z","America/New_York"),"2026-11-01T09:00");
assert.equal(sandbox.toLocal("2026-10-31T13:00:00Z","America/New_York"),"2026-10-31T09:00");
const saved={id:"reminder-1",title:"开会",kind:"reminder",repeat:"daily",timezone:"America/New_York",due_at:"2026-11-01T14:00:00Z"};
sandbox.taskModal(saved);
assert.match(modal[2],/name="timezone" value="America\/New_York"/);
assert.match(modal[2],/name="due_at" value="2026-11-01T09:00"/);
assert.match(modal[2],/不存在的时间会拒绝/);
assert.equal(sandbox.taskTime(saved),"2026-11-01 09:00 (America/New_York)");
sandbox.taskModal();
assert.match(modal[2],/name="timezone" value="Asia\/Tokyo"/);
console.log("Reminder timezone UI regression passed");
