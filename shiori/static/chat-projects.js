"use strict";

// Projects organize the sidebar only. Scope identity, memories and computer
// workspaces continue to be owned by the existing conversation APIs.
window.ShioriChatProjects={create({activateScope,saveDraft}){
  let projects=[],conversations=[],search="",view="all",selectedProject="",loading=null,sidebarOpen=false;
  let viewRevision=0,loadRevision=0,loadingGeneration=-1;
  const pending=new Set();
  const actions=new Set(["chat-toggle-sidebar","chat-filter","chat-back-management","scope-chat","new-chat","chat-project-new","chat-project-edit","chat-project-delete","chat-conversation-menu","chat-conversation-archive"]);
  const forms=new Set(["chat-project-save","chat-conversation-save"]);
  const encode=value=>encodeURIComponent(String(value || ""));
  const conversation=id=>conversations.find(item=>item.id===id);
  const project=id=>projects.find(item=>item.id===id);
  const modalIdentity=()=>$("#modal-content")?.firstElementChild||null;
  const capture=()=>({auth:state.authGeneration||0,page:state.page,scope:state.scope,render:state.renderId,view:viewRevision,
    modal:modalIdentity(),modalOpen:!!$("#modal")?.open,modalKind:state.modalKind});
  const current=context=>context.auth===(state.authGeneration||0)&&state.authenticated!==false&&context.page===state.page&&context.scope===state.scope&&context.render===state.renderId&&context.view===viewRevision&&
    context.modal===modalIdentity()&&context.modalOpen===!!$("#modal")?.open&&context.modalKind===state.modalKind;
  function mergeScopes(items){
    const known=new Set(state.scopes.map(item=>item.id));
    // A late organization request must not replace effective configuration that
    // bootstrap/model controls may have updated while it was pending.
    state.scopes=[...state.scopes,...items.filter(item=>!known.has(item.id))];
  }
  function invalidate(){loadRevision++;loading=null;}
  async function mutate(key,context,perform){
    const pendingKey=`${context.auth}:${key}`;
    if(pending.has(pendingKey))return true;
    pending.add(pendingKey);
    try{await perform(context);}
    catch(error){if(context.auth!==(state.authGeneration||0)||state.authenticated===false)return true;if(current(context))throw error;toast(error.message,true);}
    finally{pending.delete(pendingKey);}
    return true;
  }
  async function load(fresh=false){
    if(fresh)invalidate();
    if(loading&&loadingGeneration!==(state.authGeneration||0))invalidate();
    if(loading)return loading;
    const revision=++loadRevision,generation=state.authGeneration||0;
    loadingGeneration=generation;
    const request=(async()=>{
      const data=await api("/api/chat/workspace");
      if(generation!==(state.authGeneration||0)||state.authenticated===false)return data;
      const nextProjects=Array.isArray(data.projects)?data.projects:[],nextConversations=(Array.isArray(data.conversations)?data.conversations:[]).filter(item=>String(item.id).startsWith("web:"));
      // Workspace rows contain organization metadata only. Load complete scopes
      // before using a conversation created here or in another browser tab.
      const scopes=nextConversations.some(item=>!state.scopes.some(scope=>scope.id===item.id))?rows(await api("/api/scopes")):[];
      if(revision!==loadRevision||generation!==(state.authGeneration||0)||state.authenticated===false)return data;
      mergeScopes(scopes);projects=nextProjects;conversations=nextConversations;
      for(const item of conversations){
        const existing=state.scopes.find(scope=>scope.id===item.id);
        if(existing)existing.name=item.name;
      }
      if(selectedProject&&!project(selectedProject)){selectedProject="";view="all";}
      return data;
    })();
    loading=request;
    try{return await request;}finally{if(loading===request)loading=null;}
  }
  function filtered(){
    const needle=search.trim().toLocaleLowerCase();
    return conversations.filter(item=>view==="archived"?item.archived:!item.archived)
      .filter(item=>view!=="project"||item.project_id===selectedProject)
      .filter(item=>view!=="unfiled"||!item.project_id)
      .filter(item=>!needle||[item.name,project(item.project_id)?.name].some(value=>String(value||"").toLocaleLowerCase().includes(needle)))
      .sort((a,b)=>Number(b.pinned)-Number(a.pinned)||String(b.updated_at||"").localeCompare(String(a.updated_at||""))||a.id.localeCompare(b.id));
  }
  function conversationRows(){
    const items=filtered();
    return items.length?items.map(item=>`<div class="chat-conversation-row ${item.id===state.scope?"active":""}"><button type="button" class="conversation-item ${item.id===state.scope?"active":""}" data-action="scope-chat" data-id="${e(item.id)}" title="${e(item.name)}" ${item.id===state.scope?'aria-current="true"':""}>${icon("chat")}<span><strong>${e(item.name||"新对话")}</strong><small>${e(project(item.project_id)?.name||"未分组")}${item.pinned?" · 置顶":""}</small></span></button><button type="button" class="chat-row-menu" data-action="chat-conversation-menu" data-id="${e(item.id)}" aria-label="管理对话：${e(item.name||"新对话")}" title="管理对话">···</button></div>`).join(""):`<p class="chat-list-empty">${search?"没有匹配的对话":"这里还没有对话"}</p>`;
  }
  function listTitle(){return view==="archived"?"已归档":view==="project"?(project(selectedProject)?.name||"项目对话"):view==="unfiled"?"未分组对话":"全部对话";}
  function renderSidebar(){
    const count=id=>conversations.filter(item=>!item.archived&&(id===undefined||item.project_id===id)).length;
    return `<aside class="conversations chat-project-sidebar ${sidebarOpen?"open":""}" id="chat-project-sidebar" aria-label="项目与对话"><div class="chat-sidebar-brand"><span class="chat-brand-mark">S</span><div><strong>SHIORI</strong><small>CHAT WORKSPACE</small></div><button type="button" class="icon-button chat-sidebar-close" data-action="chat-toggle-sidebar" aria-label="关闭会话列表">${icon("close")}</button></div><button type="button" class="btn primary chat-new-button" data-action="new-chat">${icon("plus")}<span>新对话</span></button><label class="chat-search">${icon("search")}<input id="chat-conversation-search" type="search" value="${e(search)}" placeholder="搜索对话或项目" aria-label="搜索对话或项目" autocomplete="off"></label><div class="chat-project-nav"><button type="button" class="chat-project-link ${view==="all"?"active":""}" data-action="chat-filter" data-view="all">${icon("chat")}<span>全部对话</span><small>${count()}</small></button><button type="button" class="chat-project-link ${view==="unfiled"?"active":""}" data-action="chat-filter" data-view="unfiled">${icon("file")}<span>未分组</span><small>${count(null)+conversations.filter(item=>!item.archived&&item.project_id===undefined).length}</small></button></div><div class="chat-project-heading"><span>项目</span><button type="button" class="icon-button" data-action="chat-project-new" aria-label="新建项目" title="新建项目">${icon("plus")}</button></div><div class="chat-project-list">${projects.filter(item=>!item.archived).map(item=>`<div class="chat-project-row ${view==="project"&&selectedProject===item.id?"active":""}"><button type="button" class="chat-project-link" data-action="chat-filter" data-view="project" data-id="${e(item.id)}" title="${e(item.description||item.name)}">${icon("memory")}<span>${e(item.name)}</span><small>${count(item.id)}</small></button><button type="button" class="chat-row-menu" data-action="chat-project-edit" data-id="${e(item.id)}" aria-label="管理项目：${e(item.name)}" title="管理项目">···</button></div>`).join("")||'<p class="chat-project-empty">为不同主题整理对话</p>'}</div><div class="chat-conversation-heading"><span id="chat-conversation-list-title">${e(listTitle())}</span><span id="chat-conversation-list-count">${filtered().length}</span></div><nav class="chat-conversation-list" id="chat-conversation-list" aria-label="对话列表">${conversationRows()}</nav><div class="chat-sidebar-footer"><button type="button" class="chat-project-link ${view==="archived"?"active":""}" data-action="chat-filter" data-view="archived">${icon("clock")}<span>已归档</span><small>${conversations.filter(item=>item.archived).length}</small></button><p>项目仅用于整理对话</p></div></aside>${sidebarOpen?'<button class="chat-sidebar-scrim" type="button" data-action="chat-toggle-sidebar" aria-label="关闭会话列表"></button>':""}`;
  }
  function refreshSidebar(){
    const sidebar=$("#chat-project-sidebar");
    if(sidebar){const scrim=$(".chat-sidebar-scrim");scrim?.remove();sidebar.outerHTML=renderSidebar();}
  }
  function handleInput(target){
    if(target.id!=="chat-conversation-search")return false;
    viewRevision++;search=target.value;const list=$("#chat-conversation-list"),count=$("#chat-conversation-list-count");
    if(list)list.innerHTML=conversationRows();if(count)count.textContent=String(filtered().length);return true;
  }
  function projectForm(item={}){
    openModal(item.id?"管理项目":"新建项目","项目只整理对话，不会合并记忆，也不会改变电脑任务的工作目录。",`<input type="hidden" name="id" value="${e(item.id||"")}">${field("name","项目名称",item.name||"","text","","required maxlength=\"100\" autofocus")}${textarea("description","项目说明",item.description||"","可选，用一句话描述项目。","maxlength=\"1000\" rows=\"3\"")}${item.id?`<div class="chat-project-delete">${button("删除项目分组","chat-project-delete","danger small","trash",`data-id="${e(item.id)}"`)}<small>其中的对话和记录会保留。</small></div>`:""}`,"chat-project-save",null,item.id?"保存项目":"创建项目");
  }
  async function refresh(context,selectProject){
    if(context.auth!==(state.authGeneration||0)||state.authenticated===false)return;
    await load(true);
    if(!current(context))return;
    saveDraft();
    if(selectProject){view="project";selectedProject=selectProject;}
    if(context.modalOpen)closeModal();
    await renderPage();
  }
  async function handleAction(action,target){
    const id=target.dataset.id;
    if(action==="chat-toggle-sidebar"){sidebarOpen=!sidebarOpen;refreshSidebar();return true;}
    if(action==="chat-filter"){
      viewRevision++;
      view=["all","unfiled","archived","project"].includes(target.dataset.view)?target.dataset.view:"all";
      selectedProject=view==="project"?id:"";refreshSidebar();return true;
    }
    if(action==="chat-back-management"){viewRevision++;saveDraft();await navigate("overview");return true;}
    if(action==="scope-chat"){
      if(!String(id).startsWith("web:"))return false;
      viewRevision++;activateScope(id);sidebarOpen=false;await navigate("chat");return true;
    }
    if(action==="new-chat"){
      const context=capture(),projectId=view==="project"?selectedProject:null;
      return mutate("conversation:new",context,async()=>{
        saveDraft();const result=await api("/api/chat/conversations",{method:"POST",body:{name:"新对话",project_id:projectId}}),item=result.conversation||result;
        if(context.auth!==(state.authGeneration||0)||state.authenticated===false)return;
        if(!String(item.id).startsWith("web:"))throw new Error("新对话未返回有效标识，请刷新列表。");
        invalidate();
        const scopes=rows(await api("/api/scopes"));
        if(context.auth!==(state.authGeneration||0)||state.authenticated===false)return;
        if(!scopes.some(scope=>scope.id===item.id))throw new Error("对话已创建，但配置尚未读取完整，请刷新列表后打开。");
        mergeScopes(scopes);conversations=[...conversations.filter(row=>row.id!==item.id),item];
        if(!current(context)){toast("新对话已创建，可在对话列表中打开。");return;}
        activateScope(item.id);sidebarOpen=false;
        if(view==="archived")view="all";await navigate("chat");
      });
    }
    if(action==="chat-project-new"){viewRevision++;projectForm();return true;}
    if(action==="chat-project-edit"){const item=project(id);if(!item)throw new Error("项目已变化，请刷新列表。");viewRevision++;projectForm(item);return true;}
    if(action==="chat-project-delete"){
      const context=capture();return mutate(`project:${id}`,context,async()=>{
        await api(`/api/chat/projects/${encode(id)}`,{method:"DELETE"});await refresh(context);if(context.auth===(state.authGeneration||0)&&state.authenticated!==false)toast("项目分组已删除，对话和记录已保留。");
      });
    }
    if(action==="chat-conversation-menu"){
      const item=conversation(id);if(!item)throw new Error("对话已变化，请刷新列表。");
      viewRevision++;
      openModal("管理对话",item.name||"新对话",`<input type="hidden" name="id" value="${e(item.id)}">${field("name","对话名称",item.name||"新对话","text","","required maxlength=\"100\"")}${select("project_id","所属项目",[["","未分组"],...projects.filter(p=>!p.archived).map(p=>[p.id,p.name])],item.project_id||"")}${select("pinned","置顶",[["false","不置顶"],["true","置顶此对话"]],String(!!item.pinned))}<div class="chat-conversation-actions">${button(item.archived?"取消归档":"归档对话","chat-conversation-archive","small ghost","clock",`data-id="${e(item.id)}" data-archived="${item.archived?"false":"true"}"`)}<small>归档后仍可从「已归档」中找回。</small></div>`,"chat-conversation-save",null,"保存更改");return true;
    }
    if(action==="chat-conversation-archive"){
      const context=capture(),archived=target.dataset.archived==="true";return mutate(`conversation:${id}`,context,async()=>{
        await api(`/api/chat/conversations/${encode(id)}`,{method:"PATCH",body:{archived}});
        await refresh(context);if(context.auth===(state.authGeneration||0)&&state.authenticated!==false)toast(archived?"对话已归档，可在已归档列表找回。":"对话已恢复。");
      });
    }
    return false;
  }
  async function handleSubmit(kind,form){
    if(!["chat-project-save","chat-conversation-save"].includes(kind))return false;
    const data=getForm(form),name=String(data.name||"").trim();if(!name)throw new Error("请填写名称。");
    const context=capture(),key=kind==="chat-project-save"?`project:${data.id||"new"}`:`conversation:${data.id}`;
    return mutate(key,context,async()=>{
      saveDraft();let projectId;
      if(kind==="chat-project-save"){
        const result=await api(`/api/chat/projects${data.id?"/"+encode(data.id):""}`,{method:data.id?"PATCH":"POST",body:{name,description:String(data.description||"")}}),item=result.project||result;
        projectId=item.id;
      }else await api(`/api/chat/conversations/${encode(data.id)}`,{method:"PATCH",body:{name,project_id:data.project_id||null,pinned:data.pinned==="true"}});
      await refresh(context,projectId);if(context.auth===(state.authGeneration||0)&&state.authenticated!==false)toast(kind==="chat-project-save"?"项目已保存。":"对话已更新。");
    });
  }
  return {load,renderSidebar,handleInput,conversation,project,
    // Unrelated controls must retain their synchronous scope capture. Returning
    // a resolved false Promise would add a yield before attachment ownership is
    // recorded by the main experience handler.
    handleAction:(action,target)=>actions.has(action)&&(action!=="scope-chat"||String(target.dataset.id).startsWith("web:"))?handleAction(action,target):false,
    handleSubmit:(kind,form)=>forms.has(kind)?handleSubmit(kind,form):false};
}};
