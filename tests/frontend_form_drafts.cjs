"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
let mounted=[];
const state={page:'persona',botTab:'reply',scope:'web:a',authenticated:true,authGeneration:1};
const input=(name,value,type='text')=>({name,value,type,tagName:'INPUT',checked:!!value});
const form=(kind,...fields)=>({dataset:{form:kind},id:'',querySelectorAll:()=>fields});
let synced=[];
const sandbox={window:{},state,document:{querySelectorAll:()=>mounted},syncRangePair:node=>synced.push(node)};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../shiori/static/form-drafts.js'),'utf8'),sandbox);
const ui=sandbox.window.ShioriFormDrafts;
let length=input('max_chars','100'),memory=input('enabled',false,'checkbox');
mounted=[form('output',length),form('memory',memory)];ui.restore();
length.value='200';memory.checked=true;
const ticket=ui.begin(mounted[0]);ui.capture();
// A successful output save rebuilds the page with server-normalized values,
// but must preserve the separately edited memory form.
length=input('max_chars','180');memory=input('enabled',false,'checkbox');
mounted=[form('output',length),form('memory',memory)];ui.restore();ui.end(ticket,true);
assert.equal(length.value,'180');assert.equal(memory.checked,true);
assert.ok(synced.includes(memory),'restored paired controls are resynchronized');
ui.capture();state.botTab='qq';const qq=input('enabled',false,'checkbox');mounted=[form('memory',qq)];ui.restore();assert.equal(qq.checked,false);
ui.capture();state.botTab='reply';memory=input('enabled',false,'checkbox');mounted=[form('memory',memory)];ui.restore();assert.equal(memory.checked,true);
// Failed saves keep edits; they never become a success merely due to refresh.
const failed=ui.begin(mounted[0]);ui.end(failed,false);mounted=[form('memory',input('enabled',false,'checkbox'))];ui.restore();assert.equal(mounted[0].querySelectorAll()[0].checked,true);
// Editing back to the new baseline removes the saved draft.
mounted[0].querySelectorAll()[0].checked=false;ui.capture();mounted=[form('memory',input('enabled',false,'checkbox'))];ui.restore();assert.equal(mounted[0].querySelectorAll()[0].checked,false);
// A save without page replacement adopts its submitted baseline; later edits
// during the request still remain unsaved and survive subsequent page refresh.
let inline=input('name','before');mounted=[form('inline',inline)];ui.restore();inline.value='saved';
const inlineTicket=ui.begin(mounted[0]);inline.value='typed-during-save';ui.end(inlineTicket,true);ui.capture();
inline=input('name','saved');mounted=[form('inline',inline)];ui.restore();assert.equal(inline.value,'typed-during-save');
inline.value='saved';ui.capture();mounted=[form('inline',input('name','saved'))];ui.restore();assert.equal(mounted[0].querySelectorAll()[0].value,'saved');
// In-memory secrets and other drafts cannot outlive an authenticated session.
const secret=input('api_key','');mounted=[form('provider',secret)];ui.restore();secret.value='draft-secret';ui.capture();
state.authenticated=false;state.authGeneration++;ui.clear();state.authenticated=true;
const fresh=input('api_key','');mounted=[form('provider',fresh)];ui.restore();assert.equal(fresh.value,'');
console.log('Form drafts: independent saves, tab ownership, failure preservation, reverting and logout clearing passed');
