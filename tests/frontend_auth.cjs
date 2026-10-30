"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const handlers = {}, calls = [], classes = new Set();
const error = {hidden:true,textContent:""}, root = {innerHTML:"",hidden:false,querySelector:selector=>selector === ".auth-error" ? error : null};
let current = {mode:"setup",setup_required:true,local_setup_allowed:false,authenticated:false}, entered = 0;
let access = {remote_enabled:false,host:"0.0.0.0",port:8765};
let responseError = "";
let transportHook=null;
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const sandbox = {
  window:{},URLSearchParams,location:{hash:"#bootstrap=local-proof",pathname:"/",search:""},
  history:{replaceState(){sandbox.location.hash="";}},
  FormData:class {constructor(form){this.form=form;}[Symbol.iterator](){return Object.entries(this.form.values)[Symbol.iterator]();}},
  document:{getElementById:id=>id === "auth-root" ? root : id === "modal" ? {close(){}} : null,body:{classList:{add:value=>classes.add(value),remove:value=>classes.delete(value)}},addEventListener:(name,fn)=>handlers[name]=fn},
  fetch:async (url,options)=>{calls.push([url,options]);let data={};
    if(transportHook){const intercepted=transportHook(url,options);if(intercepted)return await intercepted;}
    if(url==="/api/auth/status")data=current;
    if(url==="/api/auth/access")data=access;
    if(responseError)return{ok:false,status:401,json:async()=>({detail:responseError})};
    if(url==="/api/auth/login" || url==="/api/auth/setup")current={...current,mode:"password",authenticated:true,setup_required:false};
    if(url==="/api/auth/logout")current={...current,authenticated:false};
    return{ok:true,status:200,json:async()=>data};},
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../shiori/static/auth.js"),"utf8"),sandbox);
const auth=sandbox.window.ShioriAuth;
const form=(kind,values)=>({dataset:{authForm:kind},values,reset(){this.values={};},querySelector:selector=>selector === ".auth-form-error" ? error : {disabled:false},closest(){return this;}});
const submit=async item=>handlers.submit({target:item,preventDefault(){},stopImmediatePropagation(){}});

async function main(){
  await auth.init(async()=>{entered++;});
  assert.equal(entered,0,"startup proof must not authenticate an existing account");
  assert.equal(sandbox.location.hash,"");
  assert.deepEqual(calls.map(item=>item[0]),["/api/auth/bootstrap","/api/auth/status"]);
  assert.match(root.innerHTML,/先在运行电脑上设置密码/);
  assert.ok(!root.innerHTML.includes('data-auth-form="setup"'),"remote users cannot race to initialize");
  assert.ok(!root.innerHTML.includes('name="password"'));
  assert.ok(!classes.has("auth-ready"));

  current={mode:"setup",setup_required:true,local_setup_allowed:true,authenticated:false,password_file:'D:\\private\\<secret>.json',data_dir:"D:\\private"};
  await auth.refresh();
  assert.match(root.innerHTML,/data-auth-form="setup"/);
  assert.match(root.innerHTML,/仅本机免密/);
  assert.match(root.innerHTML,/&lt;secret&gt;/);
  assert.ok(!root.innerHTML.includes("<secret>"));
  const before=calls.length;
  await submit(form("setup",{password:"correct-password",confirm_password:"different-password"}));
  assert.equal(calls.length,before,"mismatched confirmation must not write credentials");
  assert.match(error.textContent,/两次输入/);

  const setup=form("setup",{password:"correct-password",confirm_password:"correct-password"});
  await submit(setup);
  assert.equal(entered,1);assert.ok(classes.has("auth-ready"));assert.equal(root.innerHTML,"");
  const write=calls.find(item=>item[0]==="/api/auth/setup");
  assert.deepEqual(JSON.parse(write[1].body),{password:"correct-password",mode:"password"});
  assert.equal(write[1].credentials,"same-origin");
  assert.equal(Object.keys(setup.values).length,0,"successful submit clears the password form");

  current={mode:"password",password_set:true,authenticated:true,actual_remote_enabled:false,restart_required:true,urls:["http://127.0.0.1:8765/"]};
  const panel=await auth.settingsPanel();
  assert.match(panel,/登录与远程管理/);assert.match(panel,/重启 shiori 后生效/);
  assert.ok(!panel.includes("D:\\private"),"remote status does not preserve an earlier local path");
  await submit(form("network",{remote_enabled:"on",host:"0.0.0.0",port:"9123"}));
  const network=calls.find(item=>item[0]==="/api/auth/access"&&item[1].method==="PUT");
  assert.deepEqual(JSON.parse(network[1].body),{remote_enabled:true,host:"0.0.0.0",port:9123,require_https:true,public_url:'',allowed_hosts:[],trusted_proxy_ips:[]});

  current={mode:"password",password_set:true,authenticated:false};await auth.requireLogin();
  responseError="密码不正确";const login=form("login",{password:"wrong-password"});
  await submit(login);assert.match(error.textContent,/密码不正确/);assert.ok(!classes.has("auth-ready"));

  // The isolated auth module remains usable without app.js; when the app hook
  // exists, every transition to the login screen tears it down immediately.
  responseError="";let exits=0;sandbox.window.ShioriAppLifecycle={leaveAuthenticated(){exits++;}};
  current={mode:"password",authenticated:true};await auth.refresh();const oldEntered=entered;
  let gate=deferred();transportHook=url=>url==="/api/auth/status"?gate.promise:null;
  const oldStatus=auth.refresh();auth.screen();assert.equal(exits,1);assert.ok(!classes.has("auth-ready"));
  gate.resolve({ok:true,status:200,json:async()=>({mode:"password",authenticated:true})});await oldStatus;
  assert.equal(entered,oldEntered,"an old authenticated status cannot enter after the login screen invalidated it");assert.equal(auth.getStatus().authenticated,false);

  transportHook=null;current={mode:"password",authenticated:true};await auth.refresh();
  gate=deferred();transportHook=url=>url==="/api/auth/logout"?gate.promise:null;
  const logout=handlers.click({target:{closest:()=>({dataset:{authAction:"logout"}})},preventDefault(){}});
  assert.equal(exits,2,"explicit logout tears down the app before waiting for the network");assert.ok(!classes.has("auth-ready"));
  current={mode:"password",authenticated:false};gate.resolve({ok:true,status:200,json:async()=>({})});await logout;
  assert.equal(auth.getStatus().authenticated,false);

  transportHook=null;current={mode:"password",authenticated:true};await auth.refresh();
  gate=deferred();transportHook=url=>url==="/api/auth/status"?gate.promise:null;
  const beforeRequire=exits,relogin=auth.requireLogin();assert.equal(exits,beforeRequire+1);assert.ok(!classes.has("auth-ready"));
  gate.resolve({ok:true,status:200,json:async()=>({mode:"password",authenticated:true})});await relogin;
  assert.equal(auth.getStatus().authenticated,false,"requireLogin never trusts a status snapshot to bypass the login screen");

  gate=deferred();transportHook=url=>url==="/api/auth/login"?gate.promise:null;
  const staleLogin=submit(form("login",{password:"old-login-password"})),beforeLate=entered;
  auth.screen();const requestsBefore=calls.length;gate.resolve({ok:true,status:200,json:async()=>({})});await staleLogin;
  assert.equal(entered,beforeLate);assert.equal(calls.length,requestsBefore,"a login response invalidated by a later logout does not refresh or re-enter");
  transportHook=null;
  const script=fs.readFileSync(path.join(__dirname,"../shiori/static/auth.js"),"utf8");
  assert.ok(!script.includes("localStorage."));assert.ok(!script.includes("sessionStorage."));
  assert.ok(!script.includes("/api/session"),"legacy token auth must not bypass the password gate");
  console.log("Password UI trust-boundary checks passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
