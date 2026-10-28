"use strict";
// Exercise the real shell functions and layout module together. The DOM shim
// tracks control identity: replacing a form between blur and click loses the
// clicked submit button, even when the replacement markup looks identical.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname,"..","shiori","static");
const app = fs.readFileSync(path.join(root,"app.js"),"utf8");
const layout = fs.readFileSync(path.join(root,"sidebar-layout.js"),"utf8");
const escape = value => String(value ?? "").replace(/[&<>"']/g,c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const decode = value => String(value || "").replace(/&quot;|&#39;|&lt;|&gt;|&amp;/g,c => ({"&quot;":'"',"&#39;":"'","&lt;":"<","&gt;":">","&amp;":"&"}[c]));
const section = (start,end) => {
  const from=app.indexOf(start),to=app.indexOf(end,from);
  assert.ok(from>=0 && to>from,`shell section exists: ${start}`);
  return app.slice(from,to);
};

function boot(saved) {
  const storage=new Map(saved ? Object.entries(saved) : []),nodes=new Map(),listeners=new Map(),messages=[];
  let controls=[],panel=null,form=null,done=null,opens=0,replacements=0,renders=0;
  const generic=()=>({scrollTop:0,innerHTML:"",textContent:"",classList:{remove(){},add(){},toggle(){}}});
  const body=generic(),dialog={...generic(),open:false},document={body,activeElement:body,
    querySelectorAll(selector){
      if(selector.includes(".sidebar-icon-picker[open]"))return [];
      if(selector==='[data-sidebar-field="title"]')return controls.filter(x=>x.dataset.sidebarField==="title");
      return controls;
    },
    addEventListener(name,listener){listeners.set(name,listener);}
  };
  function replacePanel(html) {
    const old=controls;
    for(const control of old)control.isConnected=false;
    if(old.includes(document.activeElement))document.activeElement=body;
    controls=[];
    const current={contains:control=>controls.includes(control)};
    Object.defineProperty(current,"outerHTML",{set(value){replacements++;replacePanel(value);}});
    panel=current;
    for(const match of html.matchAll(/<(input|button|select)\b([^>]*)(?:>([\s\S]*?)<\/\1>|\/?\s*>)/g)){
      const [,tag,attrs,inside=""]=match,attributes={};
      for(const attr of attrs.matchAll(/([\w-]+)="([^"]*)"/g))attributes[attr[1]]=decode(attr[2]);
      const dataset={};
      for(const [key,value] of Object.entries(attributes))if(key.startsWith("data-"))dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
      const options=[...inside.matchAll(/<option value="([^"]*)"([^>]*)>/g)];
      const selected=options.find(x=>x[2].includes("selected")) || options[0];
      const control={tagName:tag.toUpperCase(),dataset,attributes,isConnected:true,disabled:/\sdisabled(?:\s|$)/.test(attrs),
        value:attributes.value ?? (selected ? decode(selected[1]) : ""),
        selectionStart:tag==="input"?0:undefined,selectionEnd:tag==="input"?0:undefined,selectionDirection:"none",
        focus(){if(!this.picker || this.picker.open)document.activeElement=this;},
        setSelectionRange(start,end,direction){this.selectionStart=start;this.selectionEnd=end;this.selectionDirection=direction;}
      };
      controls.push(control);
    }
    const pickers=new Map();
    for(const control of [...controls])if(control.dataset.action==="sidebar-group-icon"){
      const id=control.dataset.id;
      if(!pickers.has(id)){
        const summary={tagName:"SUMMARY",dataset:{},pickerId:id,isConnected:true,focus(){document.activeElement=this;}};
        const picker={open:false,querySelector:selector=>selector==="summary"?summary:null};
        pickers.set(id,picker);controls.push(summary);
      }
      control.picker=pickers.get(id);
      control.closest=selector=>selector===".sidebar-icon-picker"?control.picker:null;
    }
    for(const [selector,css] of [["#modal .sidebar-manage-pages","sidebar-manage-pages"],["#modal .sidebar-manage-list","sidebar-manage-list"]]){
      if(html.includes(`class="${css}`))nodes.set(selector,generic());else nodes.delete(selector);
    }
  }
  function q(selector,within) {
    if(selector==="#modal")return dialog;
    if(selector==="#modal .sidebar-manage")return panel;
    if(selector==="#modal .manage-tab.active")return controls.find(x=>x.attributes.class?.split(" ").includes("active") && x.dataset.action==="sidebar-manage-view") || null;
    if(selector==='[type="submit"]' && within===form)return done;
    if(selector==="#modal-error")return null;
    if(selector.startsWith("#modal .sidebar-manage-"))return nodes.get(selector) || null;
    if(!nodes.has(selector))nodes.set(selector,generic());
    return nodes.get(selector);
  }
  const state={page:"chat",scope:"web:default",filters:{},settings:{},boot:{},scopes:[],draft:"",modalKind:null};
  const sandbox={window:{scrollTo(){},ShioriPages:{handleSubmit:async()=>false},ShioriExperience:{handleSubmit:async()=>false},ShioriFeatures:{handleSubmit:async()=>false}},
    state,document,console,e:escape,icon:()=>"",configuredModel:()=>({}),$ : q,$$:selector=>document.querySelectorAll(selector),
    localStorage:{getItem:key=>storage.get(key) || null,setItem:(key,value)=>storage.set(key,String(value))},
    sessionStorage:{getItem:()=>null},toast:(...args)=>messages.push(args),renderPage:async()=>{renders++;},
    api:async()=>({settings:{},scopes:[{id:"web:default",name:"本地"}],personas:[]}),
    openModal(_title,_description,html,kind){
      opens++;state.modalKind=kind;dialog.open=true;
      done={isConnected:true,disabled:false};
      form={dataset:{form:kind},closest:selector=>selector.includes("form")?form:dialog};
      nodes.set("#modal .modal-body",generic());replacePanel(html);
    },
    closeModal(){dialog.open=false;state.modalKind=null;},
    setTimeout(){throw Error("structural sidebar edits must not schedule delayed focus stealing");},
    modalError(error){throw Error(error);}
  };
  vm.createContext(sandbox);
  vm.runInContext(layout,sandbox,{filename:"sidebar-layout.js"});
  vm.runInContext(section("const pageNames =","const state ="),sandbox);
  vm.runInContext(section("function renderNavigation()","async function bootstrap("),sandbox);
  vm.runInContext(section("async function bootstrap(","async function renderPage("),sandbox);
  vm.runInContext(section('document.addEventListener("submit",','let searchTimer;'),sandbox);
  sandbox.window.ShioriSidebar.prepare(sandbox.sidebarContext().pageNames);
  const action=(name,control)=>sandbox.window.ShioriSidebar.handleAction(name,control || {dataset:{}},sandbox.sidebarContext());
  const change=control=>sandbox.window.ShioriSidebar.handleChange(control,sandbox.sidebarContext());
  const find=predicate=>controls.find(predicate);
  const submit=async()=>listeners.get("submit")({target:form,preventDefault(){}});
  const savedLayout=()=>JSON.parse(storage.get(sandbox.window.ShioriSidebar.STORAGE_KEY));
  return {sandbox,state,document,dialog,nodes,messages,storage,action,change,find,submit,savedLayout,
    get form(){return form;},get done(){return done;},get panel(){return panel;},get opens(){return opens;},get replacements(){return replacements;},get renders(){return renders;}};
}

async function main(){
  {
    const ui=boot();ui.action("open-sidebar-manage");
    const input=ui.find(x=>x.dataset.sidebarField==="title" && x.dataset.id==="memory");
    input.focus();input.value="  我的记忆  ";
    const firstDone=ui.done,firstForm=ui.form,firstPanel=ui.panel;
    ui.change(input); // blur happens before the click reaches the footer
    assert.equal(ui.done,firstDone,"renaming must keep the first clicked Done button connected");
    assert.equal(ui.form,firstForm,"renaming must not replace the form");
    assert.equal(ui.panel,firstPanel,"renaming needs no panel replacement");
    assert.equal(input.value,"我的记忆","the normalized title stays in the existing input");
    assert.equal(ui.savedLayout().groups.find(x=>x.id==="memory").title,"我的记忆");
    assert.match(ui.nodes.get("#navigation").innerHTML,/我的记忆/,"the sidebar updates immediately");
    await ui.submit();assert.equal(ui.dialog.open,false,"the first click can close the dialog");
    assert.equal(ui.opens,1,"no dialog reopen while editing");
  }
  {
    const ui=boot();ui.action("open-sidebar-manage");
    const input=ui.find(x=>x.dataset.sidebarField==="title" && x.dataset.id==="memory");
    input.focus();input.value="按回车保存";
    await ui.submit(); // implicit Enter submit, without a prior change event
    assert.equal(ui.savedLayout().groups.find(x=>x.id==="memory").title,"按回车保存");
    assert.equal(ui.dialog.open,false);
  }
  {
    const ui=boot();ui.action("open-sidebar-manage");
    const input=ui.find(x=>x.dataset.sidebarField==="title");
    input.focus();input.value="新名称";ui.change(input);
    const tab=ui.find(x=>x.dataset.action==="sidebar-manage-view" && x.dataset.view==="pages");
    tab.focus();const done=ui.done,form=ui.form;ui.action("sidebar-manage-view",tab);
    assert.equal(ui.document.activeElement.dataset.view,"pages","focus stays on the clicked view, not its sibling");
    assert.equal(ui.done,done);assert.equal(ui.form,form);assert.equal(ui.opens,1);
    assert.ok(ui.find(x=>x.dataset.sidebarField==="page-group"),"the first click switches view");
    assert.ok(!ui.find(x=>x.dataset.targetPage==="models"),"the old models route is not a second page to manage");
    const select=ui.find(x=>x.dataset.sidebarField==="page-group" && x.dataset.targetPage==="plugins");
    select.focus();select.value="memory";
    ui.nodes.get("#modal .sidebar-manage-pages").scrollTop=180;
    ui.dialog.scrollTop=37;ui.change(select);
    assert.equal(ui.document.activeElement.dataset.targetPage,"plugins","a move keeps focus on its page");
    assert.equal(ui.document.activeElement.dataset.id,"memory","focus identity does not use the previous category");
    assert.equal(ui.nodes.get("#modal .sidebar-manage-pages").scrollTop,180);
    assert.equal(ui.dialog.scrollTop,37);
    assert.equal(ui.done,done);assert.equal(ui.form,form);
  }
  {
    const ui=boot();ui.action("open-sidebar-manage");
    const button=ui.find(x=>x.dataset.action==="sidebar-group-icon" && x.dataset.id==="memory" && x.dataset.icon==="shield");
    button.closest(".sidebar-icon-picker").open=true;
    button.focus();ui.action("sidebar-group-icon",button);
    assert.equal(ui.document.activeElement.tagName,"SUMMARY","a closed replacement palette must focus its visible summary");
    assert.equal(ui.document.activeElement.pickerId,"memory","icon focus stays with the edited category");
    const title=ui.find(x=>x.dataset.sidebarField==="title" && x.dataset.id==="memory");
    title.focus();title.setSelectionRange(1,3,"backward");ui.sandbox.sidebarContext().rerenderManage();
    assert.equal(ui.document.activeElement.selectionStart,1);
    assert.equal(ui.document.activeElement.selectionEnd,3);
    assert.equal(ui.document.activeElement.selectionDirection,"backward");
    const remove=ui.find(x=>x.dataset.action==="sidebar-group-delete" && x.dataset.id==="memory");
    remove.focus();ui.action("sidebar-group-delete",remove);
    assert.equal(ui.document.activeElement.dataset.view,"groups","deleting the focused row has a usable focus fallback");
  }
  {
    const ui=boot();assert.ok(!Object.hasOwn(ui.sandbox.sidebarContext().pageNames,"models"));
    ui.sandbox.renderNavigation();
    assert.ok(!ui.nodes.get("#navigation").innerHTML.includes('data-page="models"'));
    ui.nodes.set("#chat-text",{value:"尚未发送的草稿"});
    await ui.sandbox.navigate("models");
    assert.equal(ui.state.page,"persona");assert.equal(ui.state.botTab,"models");
    assert.equal(ui.state.draft,"尚未发送的草稿");
    ui.action("sidebar-page-toggle",{dataset:{targetPage:"persona"}});
    assert.equal(ui.state.page,"chat","hiding the current page returns to a reachable page");
    await ui.sandbox.navigate("models");
    assert.equal(ui.state.page,"chat","the legacy alias cannot bypass persona visibility");
    ui.action("sidebar-page-toggle",{dataset:{targetPage:"persona"}});
    ui.state.page="models";await ui.sandbox.bootstrap();
    assert.equal(ui.state.page,"persona");assert.equal(ui.state.botTab,"models");
    const module=ui.sandbox.window.ShioriSidebar;delete ui.sandbox.window.ShioriSidebar;
    ui.sandbox.renderNavigation();
    assert.ok(!ui.nodes.get("#navigation").innerHTML.includes('data-page="models"'),"the fallback uses the same page map");
    assert.ok(ui.nodes.get("#navigation").innerHTML.includes('data-page="persona"'));
    ui.sandbox.window.ShioriSidebar=module;
  }
  {
    const original=JSON.stringify({version:99,groups:[{id:"future",title:"新版分类",icon:"spark",pages:["plugins"],custom:true}],hidden:[],ungrouped:[],futureData:{preserve:true}});
    const ui=boot({"shiori-sidebar-layout-v1":original});ui.action("open-sidebar-manage");
    const input=ui.find(x=>x.dataset.sidebarField==="title" && x.dataset.id==="future");
    input.focus();input.value="仅本次改名";
    await ui.submit();
    assert.equal(ui.storage.get("shiori-sidebar-layout-v1"),original,"an older build must preserve the original newer-schema layout");
    assert.match(ui.messages.at(-1)[0],/仅临时生效/);
    assert.ok(!ui.messages.at(-1)[0].includes("已保存"),"temporary changes must not claim persistence");
  }
  console.log("sidebar rename, first-click submit/view changes, stable focus and model-route compatibility passed");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
