"use strict";

window.ShioriExperienceChat={create(runtime,helpers){
  const {inactive,jobDetail}=helpers;
    let collected=null, collectTimer=null, activeDraft=null,activeReader=null,lifecycle=0;
    const savedDrafts=new Map();
    let renderRevision=0;
    const workspace=window.ShioriChatProjects?.create({activateScope,saveDraft});
    const uniqueAttachments=items=>[...new Map(items.map(item=>[item.id,item])).values()];
    function saveDraft(){
      if(state.page==="chat")state.draft=$("#chat-text")?.value ?? state.draft ?? "";
    }
    function parkDraft(){
      saveDraft();
      // Switching parks a collected batch instead of leaving a timer that can
      // send it to a conversation the user is no longer viewing.
      if(collected){
        clearTimeout(collectTimer);collectTimer=null;
        const batch=collected;collected=null;state.debouncePending=false;
        restoreDraft(batch.scope,{text:batch.messages.map(item=>item.text).filter(Boolean).join("\n"),images:batch.images,attachments:batch.attachments});
      }
      if(String(state.scope).startsWith("web:") && !(state.page!=="chat"&&savedDrafts.has(state.scope)))savedDrafts.set(state.scope,{text:state.draft||"",images:[...state.images],attachments:runtime.attachmentScope===state.scope?[...runtime.attachments]:[]});
      clearComposer();
    }
    function activateScope(scopeId){
      if(!String(scopeId).startsWith("web:"))return false;
      saveDraft();
      if(state.scope===scopeId&&!savedDrafts.has(scopeId))return true;
      if(state.scope!==scopeId)parkDraft();
      const draft=savedDrafts.get(scopeId)||{text:"",images:[],attachments:[]};savedDrafts.delete(scopeId);
      state.scope=scopeId;state.draft=draft.text;state.images=[...draft.images];runtime.attachments=[...draft.attachments];runtime.attachmentScope=scopeId;
      runtime.attachmentRevision=(runtime.attachmentRevision||0)+1;
      // navigate() reads the mounted textarea before replacing the page.
      const input=$("#chat-text");if(input)input.value=state.draft;
      localStorage.setItem("shiori-scope",scopeId);
      return true;
    }
    function composerDraft(scopeId=state.scope) {
      if(state.scope!==scopeId)return {text:"",images:[],attachments:[]};
      return {text:$("#chat-text")?.value ?? state.draft ?? "",images:[...state.images],
        attachments:runtime.attachmentScope===scopeId ? [...runtime.attachments] : []};
    }
    function joinDrafts(...drafts) {
      return {text:drafts.map(item=>item.text).filter(Boolean).join("\n"),
        images:drafts.flatMap(item=>item.images),attachments:uniqueAttachments(drafts.flatMap(item=>item.attachments))};
    }
    function clearComposer() {
      state.draft="";state.images=[];runtime.attachments=[];
      const input=$("#chat-text");if(input)input.value="";
    }
    function restoreDraft(scopeId,draft) {
      if(state.scope!==scopeId || state.page!=="chat") {
        savedDrafts.set(scopeId,joinDrafts(savedDrafts.get(scopeId) || {text:"",images:[],attachments:[]},draft));
        return;
      }
      const restored=joinDrafts(draft,composerDraft(scopeId));
      state.draft=restored.text;state.images=restored.images;
      runtime.attachments=restored.attachments;runtime.attachmentScope=scopeId;
    }
    function syncComposer(scopeId) {
      if(state.scope!==scopeId || state.page!=="chat")return;
      const input=$("#chat-text");if(input){input.value=state.draft;input.disabled=false;input.focus?.();}
      const chips=$("#attachment-chips");if(chips)chips.innerHTML=attachmentChips();
      if(typeof renderImagePreviews==="function")renderImagePreviews();
      // A failed bootstrap leaves the original pending form mounted. Restore its
      // controls without a second network request or another accepted chat send.
      const form=$(".chat-composer");
      form?.querySelectorAll?.("textarea,button,select").forEach(node=>{node.disabled=false;});
      const cancel=form?.querySelector?.('[data-action="cancel-chat"]');
      if(cancel){cancel.removeAttribute("data-action");cancel.type="submit";cancel.textContent="发送";}
    }
    function debounceConfig() {
      const config=state.settings?.debounce || {}, current=typeof scope === "function" ? scope() : {};
      const seconds=Number(current.debounce_seconds ?? config.seconds ?? 3);
      return {enabled:current.debounce_enabled ?? config.enabled ?? false,seconds,
        maxWait:Math.max(seconds,Number(current.debounce_max_wait_seconds ?? config.max_wait_seconds ?? 30))};
    }
    function collectedPreview() {
      if(!collected || collected.scope!==state.scope)return "";
      return `<section class="chat-collected" role="status"><strong>正在收集 ${collected.messages.length} 条消息</strong><p>最后一条之后等待 ${e(debounceConfig().seconds)} 秒，再统一回复。</p><ol>${collected.messages.map(item=>`<li>${e(item.text || "[图片]")}${item.images.length?` · ${item.images.length} 张图片`:""}${item.attachments.length?` · ${item.attachments.length} 份参考资料`:""}</li>`).join("")}</ol><div class="section-actions">${button("立即回复","flush-chat-debounce","small primary","send")}${button("取消收集","cancel-chat-debounce","small ghost","stop")}</div></section>`;
    }
    function pendingPreview() {
      if (!state.sending) return collectedPreview();
      if(activeDraft?.scope!==state.scope)return '<p class="chat-other-pending" role="status">另一对话正在回复。你可以先编辑草稿，回复完成后即可发送。</p>';
      const text = runtime.preview?.scope === state.scope ? runtime.preview.text : "";
      return `<div class="pending-message" role="status"><span class="spinner small"></span><span id="chat-preview-status">${text ? "正在生成，完成后以最终回复为准…" : "正在处理你的消息…"}</span></div><div id="chat-preview" class="chat-preview" style="white-space:pre-wrap;overflow-wrap:anywhere" aria-live="off">${e(text || "")}</div>`;
    }
    function updatePreview(scopeId, text) {
      runtime.preview = {scope:scopeId,text};
      if (state.scope !== scopeId || state.page !== "chat") return;
      const node = $("#chat-preview"), label = $("#chat-preview-status"), feed = $("#chat-feed");
      const nearEnd = feed && feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
      if (node) node.textContent = text;
      if (label) label.textContent = text ? "正在生成，完成后以最终回复为准…" : "正在处理你的消息…";
      if (nearEnd) feed.scrollTop = feed.scrollHeight;
    }
    async function readChatResult(response, scopeId, generation=lifecycle) {
      // The same endpoint remains compatible with ordinary JSON clients and
      // older servers. Never replay an accepted request to negotiate streaming.
      if (!response?.headers?.get) return response;
      if (!response.headers.get("content-type")?.includes("text/event-stream")) return response.json();
      const reader = response.body?.getReader();
      if (!reader) throw new Error("浏览器未提供增量响应，请重新加载页面后重试。");
      activeReader=reader;
      const decoder = new TextDecoder("utf-8",{fatal:true});
      let buffer = "", received = 0, preview = "", result, complete = false;
      function consume(block) {
        const data = block.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
        if (!data) return;
        const event = JSON.parse(data);
        if (event.type === "reset") { preview = ""; updatePreview(scopeId,""); }
        else if (event.type === "delta") {
          if (typeof event.text !== "string" || preview.length + event.text.length > 120000) throw new Error("增量回复超过显示上限，已停止等待。");
          preview += event.text; updatePreview(scopeId,preview);
        } else if (event.type === "result") {
          result = event.data; complete = true;
          updatePreview(scopeId,String(result?.reply || ""));
        } else if (event.type === "error") {
          updatePreview(scopeId,"");
          if (event.code === "authentication_required") {
            state.authenticated=false;toast(typeof event.message==="string"?event.message:"登录已失效",true);close();
            if(typeof showAuth==="function")showAuth();else window.ShioriAuth?.requireLogin?.();
          }
          throw new Error(typeof event.message === "string" ? event.message : "本次回复未能完成，请查看运行日志。");
        }
      }
      try {
        while (!complete) {
          const {value,done} = await reader.read();
          if(generation!==lifecycle){const error=new Error("登录状态已变化。");error.name="AbortError";throw error;}
          if (done) {
            buffer += decoder.decode();
            if (buffer.trim()) consume(buffer);
            break;
          }
          received += value.byteLength;
          if (received > 8 * 1024 * 1024) throw new Error("增量响应超过接收上限，已停止等待。");
          buffer += decoder.decode(value,{stream:true});
          let boundary;
          while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
            const block = buffer.slice(0,boundary.index);
            buffer = buffer.slice(boundary.index + boundary[0].length);
            consume(block);
          }
          if (buffer.length > 2 * 1024 * 1024) throw new Error("增量响应格式超过接收上限。");
        }
        if (!complete) throw new Error("回复传输中断，未将预览当作完整回复。请查看聊天记录后再决定是否重试。");
        return result;
      } finally {
        if(activeReader===reader)activeReader=null;
        await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    }
    async function renderChat() {
      const scopeId=state.scope,revision=++renderRevision;
      const [messages,jobs,runs,modelControl] = await Promise.all([api(`/api/messages?${query({scope_id:scopeId})}`),api(`/api/jobs?${query({scope_id:scopeId})}`),api(`/api/tool-runs?${query({scope_id:scopeId,status:"pending"})}`),window.ShioriPages ? window.ShioriPages.chatModelControl() : "",workspace?.load()]);
      if(revision!==renderRevision||state.scope!==scopeId||state.page!=="chat")return "";
      state.messages = rows(messages); state.liveJobs = rows(jobs); state.toolRuns = rows(runs);
      const local = state.scope.startsWith("web:"), job = state.liveJobs.find(item => !inactive(item));
      const current=workspace?.conversation(scopeId),project=workspace?.project(current?.project_id),sendingHere=state.sending&&activeDraft?.scope===scopeId;
      if (runtime.attachmentScope !== state.scope) {
        await releaseAttachments(runtime.attachments, runtime.attachmentScope);
        if(revision!==renderRevision||state.scope!==scopeId||state.page!=="chat")return "";
        runtime.attachments = []; runtime.attachmentScope = state.scope;
      }
      if(!sendingHere && savedDrafts.has(state.scope)) {
        const saved=savedDrafts.get(state.scope);savedDrafts.delete(state.scope);restoreDraft(state.scope,saved);
      }
      const pending = state.toolRuns.filter(item => item.status === "pending" && !item.read_only);
      return `<div class="chat-workspace-page"><header class="chat-workspace-bar"><div class="chat-workspace-bar-title"><button type="button" class="icon-button chat-mobile-toggle" data-action="chat-toggle-sidebar" aria-label="打开项目与对话">${icon("chat")}</button><span class="chat-workspace-kicker">LOCAL / CHAT</span><span class="chat-workspace-status"><i></i>个人工作台</span></div><div class="inline-stack">${button("后台任务","go-jobs","ghost small","tasks")}${button("返回机器人管理","chat-back-management","small","arrow")}</div></header>${state.boot?.setup_required ? notice("连接语言模型，开始你的第一段对话。","warning","quick-model","连接模型") : ""}<div class="chat-workspace chat-workspace-standalone">${sessionSidebar()}<section class="chat-layout"><header class="chat-top"><div class="chat-identity"><div class="chat-avatar">S</div><div><p class="chat-project-breadcrumb">${e(project?.name||"个人对话")}${current?.archived?" / 已归档":""}</p><h3>${e(current?.name||scope().name||"新对话")}</h3><p>${e(personaName(scope().persona_id))}</p></div></div><div class="inline-stack chat-top-actions">${modelControl}${button("人格","go-persona","ghost small","persona")}${current?button("管理对话","chat-conversation-menu","ghost small","edit",`data-id="${e(scopeId)}"`):""}${button("清空","clear-messages","ghost small","trash")}</div></header><div class="chat-feed" id="chat-feed">${state.messages.length ? state.messages.map(renderMessage).join("") : `<div class="empty-state chat-workspace-welcome"><div class="chat-welcome-glyph">S</div><span class="chat-welcome-label">A SPACE FOR YOUR IDEAS</span><h3>今天，我们从哪里开始？</h3><p>聊一个想法，放入一份资料，或一起完成一件事。</p><div class="chat-suggestions"><button type="button" data-action="chat-suggestion" data-text="你好，介绍一下你自己吧">开始聊聊 ${icon("arrow")}</button><button type="button" data-action="chat-suggestion" data-text="我想整理一下思路，你能帮我吗？">整理思路 ${icon("arrow")}</button><button type="button" data-action="chat-suggestion" data-text="添加待办：周末整理文档">记下待办 ${icon("arrow")}</button></div></div>`}${pendingPreview()}<div id="live-job-detail">${job ? jobDetail(job,true) : ""}</div></div>${pending.length ? `<section class="jobs-mini" aria-label="等待审批的工具请求"><strong>有 ${pending.length} 项工具请求等待确认</strong>${pending.map(toolRunCard).join("")}</section>` : ""}<form class="chat-composer" data-form="chat"><div class="image-previews" id="image-previews"></div><div id="attachment-chips" class="attachment-chips">${attachmentChips()}</div><textarea id="chat-text" name="text" placeholder="${state.chatMode === "computer" ? "描述任务，例如：整理这份文档，或运行项目并找出错误…" : "发消息，或把想完成的事交给我…"}" aria-label="发送消息" rows="2" ${!local || sendingHere ? "disabled" : ""}>${e(state.draft)}</textarea><div class="composer-bottom"><div class="composer-tools"><button class="icon-button" type="button" data-action="attach-image" title="添加图片" aria-label="添加图片" ${!local || state.sending ? "disabled" : ""}>${icon("image")}</button><button class="icon-button" type="button" data-action="attachment-options" title="上传资料" aria-label="上传资料" ${!local || state.sending ? "disabled" : ""}>${icon("file")}</button><label class="mode-picker">模式<select id="chat-mode" aria-label="聊天模式" ${!local || state.sending || state.debouncePending ? "disabled" : ""}><option value="chat" ${state.chatMode !== "computer" ? "selected" : ""}>普通聊天</option><option value="computer" ${state.chatMode === "computer" ? "selected" : ""}>电脑任务</option></select></label><span class="composer-hint">Enter 发送 · Shift + Enter 换行</span></div>${sendingHere ? button("停止等待","cancel-chat","small","stop") : `<button type="submit" class="btn primary" ${!local || state.sending ? "disabled" : ""}>发送 ${icon("send")}</button>`}</div><p class="chat-composer-note">${state.chatMode==="computer"?"电脑任务在已配置的工作目录中执行，可在任务卡片随时停止。":"项目用于分类，不改变会话记忆与资料设置。"}</p></form></section></div></div>`;
    }

    function sessionSidebar() {
      if(workspace)return workspace.renderSidebar();
      const web = (state.scopes||[]).filter(item => item.id.startsWith("web:"));
      return `<aside class="conversations" aria-label="聊天会话">${button("新对话","new-chat","small","plus")}${web.map(item=>`<button type="button" class="conversation-item ${item.id===state.scope?"active":""}" data-action="scope-chat" data-id="${e(item.id)}">${icon("chat")}<span>${e(item.name||"新对话")}</span></button>`).join("")}</aside>`;
    }

    async function releaseAttachments(items=runtime.attachments, scopeId=runtime.attachmentScope || state.scope) {
      if (!items.length || !scopeId) return;
      const held=new Set([
        ...(collected?.scope===scopeId ? collected.attachments : []),
        ...(activeDraft?.scope===scopeId ? activeDraft.unsent.attachments : []),
        ...(savedDrafts.get(scopeId)?.attachments || [])
      ].map(item=>item.id));
      const ids=[...new Set(items.map(item=>item.id))].filter(id=>!held.has(id));
      if(ids.length) {
        runtime.attachmentRevision=(runtime.attachmentRevision || 0)+1;
        return api("/api/chat/attachments/release",{method:"POST",body:{scope_id:scopeId,ids}});
      }
    }

    async function removeAttachment(index) {
      const generation=lifecycle;
      if(state.sending)throw new Error("请先等待当前回复结束。");
      const selected=runtime.attachments,scopeId=runtime.attachmentScope||state.scope,item=selected[index];
      if(!item)return;
      const id=item.id,heldElsewhere=[
        ...(collected?.scope===scopeId?collected.attachments:[]),
        ...(activeDraft?.scope===scopeId?activeDraft.unsent.attachments:[])
      ].some(ref=>ref.id===id);
      // Keep the selection until release succeeds. The user may leave and even
      // return while the request is pending; array indices no longer identify
      // the original draft once activateScope has installed another composer.
      await releaseAttachments([item],scopeId);
      if(generation!==lifecycle)return;
      const withoutRemoved=items=>items.filter(ref=>ref.id!==id);
      if(runtime.attachmentScope===scopeId&&(runtime.attachments===selected||!heldElsewhere))runtime.attachments=withoutRemoved(runtime.attachments);
      const saved=savedDrafts.get(scopeId);
      if(saved&&!heldElsewhere)saved.attachments=withoutRemoved(saved.attachments);
      if(state.scope===scopeId&&state.page==="chat"){
        const chips=$("#attachment-chips");if(chips)chips.innerHTML=attachmentChips();
      }
    }

    function attachmentChips() { return runtime.attachments.map((item,index) => `<span class="attachment-chip">${icon("file")}${e(item.name || item.filename || "资料")}<small>本次参考</small><button type="button" data-action="remove-attachment" data-index="${index}" aria-label="移除附件" ${state.sending ? "disabled" : ""}>×</button></span>`).join(""); }

    function collectedPayload(batch) {
      return {scope_id:batch.scope,text:batch.messages.map(item=>item.text).join("\n").trim(),
        images:batch.messages.flatMap(item=>item.images),attachments:[...new Set(batch.messages.flatMap(item=>item.attachments))],
        messages:batch.messages,mode:"chat",stream:true};
    }
    async function flushCollected() {
      if(!collected || state.sending)return;
      clearTimeout(collectTimer);collectTimer=null;
      const batch=collected;
      if(batch.scope!==state.scope) {await cancelCollected(false);toast("聊天已切换，未提交原会话的消息。",true);return;}
      const unsent=composerDraft(batch.scope);
      collected=null;state.debouncePending=false;
      clearComposer();
      await dispatchChat(collectedPayload(batch),{text:batch.messages.map(item=>item.text).filter(Boolean).join("\n"),
        images:[...batch.images],attachments:uniqueAttachments(batch.attachments)},unsent);
    }
    async function cancelCollected(restore=true) {
      clearTimeout(collectTimer);collectTimer=null;
      const batch=collected;collected=null;state.debouncePending=false;
      if(batch){
        if(restore){restoreDraft(batch.scope,{text:batch.messages.map(item=>item.text).filter(Boolean).join("\n"),images:batch.images,attachments:batch.attachments});}
        else {
          const pending=new Set(composerDraft(batch.scope).attachments.map(item=>item.id));
          await releaseAttachments(batch.attachments.filter(item=>!pending.has(item.id)),batch.scope);
        }
      }
      await renderPage();
    }
    async function sendChat() {
      if (state.sending || activeDraft || !state.scope.startsWith("web:")) return;
      const text = $("#chat-text")?.value.trim() || "";
      if (!text && !state.images.length && !runtime.attachments.length) return;
      const scopeId = state.scope, payload = {scope_id:scopeId,text,images:state.images.map(item => item.data),attachments:runtime.attachments.map(item => item.id),mode:state.chatMode || "chat",stream:true};
      if(collected && /^(?:停止|停下|取消|别回了|不用回复了)[。！!\s]*$/.test(text)){state.draft="";await cancelCollected(false);toast("已取消收集，未调用模型。");return;}
      if((debounceConfig().enabled || collected) && payload.mode==="chat") {
        const config=debounceConfig(), original={text,images:payload.images,attachments:payload.attachments,sent_at:new Date().toISOString()};
        if(collected && collected.scope!==scopeId)throw new Error("请先处理另一聊天中待发送的消息。");
        const proposed={scope:scopeId,messages:[...(collected?.messages || []),original]};
        const joined=collectedPayload(proposed);
        if(joined.text.length>12000 || joined.images.length>(state.settings?.context?.max_images ?? 3) || joined.attachments.length>5){toast("合并后内容过多，请先点击立即回复，再发送这条补充。",true);return;}
        collected ||= {scope:scopeId,first:Date.now(),messages:[],images:[],attachments:[]};
        collected.messages.push(original);collected.images.push(...state.images);collected.attachments.push(...runtime.attachments);
        state.debouncePending=true;state.draft="";state.images=[];runtime.attachments=[];
        const composer=$("#chat-text");if(composer)composer.value="";
        clearTimeout(collectTimer);
        const delay=config.enabled ? Math.max(0,Math.min(config.seconds*1000,collected.first+config.maxWait*1000-Date.now())) : 0;
        collectTimer=setTimeout(()=>flushCollected().catch(error=>toast(error.message,true)),delay);
        if(collected.messages.length>=8){await flushCollected();return;}
        await renderPage();$("#chat-text")?.focus();return;
      }
      const submitted=composerDraft(scopeId);submitted.text=text;clearComposer();
      await dispatchChat(payload,submitted);
    }
    async function dispatchChat(payload,submitted,unsent={text:"",images:[],attachments:[]}) {
      const scopeId=payload.scope_id,generation=lifecycle;
      runtime.attachmentRevision=(runtime.attachmentRevision || 0)+1;
      activeDraft={scope:scopeId,unsent};
      updatePreview(scopeId,""); state.sending = true; state.controller = new AbortController(); const controller = state.controller;
      let completed=false;
      try {
        await renderPage();
        if(generation!==lifecycle)return;
        const response = await api("/api/chat",{method:"POST",body:payload,signal:controller.signal,raw:true});
        if(generation!==lifecycle){await response?.body?.cancel?.().catch(()=>{});return;}
        const result = await readChatResult(response,scopeId,generation);
        if(generation!==lifecycle)return;
        completed=true;
        const pending=state.scope===scopeId ? composerDraft(scopeId).attachments : [];
        const held=new Set([...unsent.attachments,...pending].map(item=>item.id));
        try { await releaseAttachments(payload.attachments.filter(id=>!held.has(id)).map(id=>({id})),scopeId); }
        catch { if(generation===lifecycle)toast("回复已完成；参考资料暂未清理，可在资料管理中释放。",true); }
        if(generation!==lifecycle)return;
        if (result.job) toast("电脑任务已创建，执行进度与结果会显示在当前聊天，可随时停止。");
        else if (result.pending_actions?.length) toast(`有 ${result.pending_actions.length} 项工具请求等待你确认。`);
      } catch (error) { controller.abort(); if(generation===lifecycle){updatePreview(scopeId,""); toast(error.name === "AbortError" ? "已停止等待。后台任务请在任务页查看并停止。" : error.message,error.name !== "AbortError");} }
      finally {
        if(generation!==lifecycle)return;
        runtime.preview=null;state.sending=false;state.controller=null;
        saveDraft();
        // Refresh is independent from the accepted result: its failure must not
        // discard drafts or cause the completed request to be submitted again.
        try {await bootstrap();}
        catch {if(generation===lifecycle)toast("聊天状态刷新失败；草稿已保留，请刷新查看最新记录。",true);}
        finally {
          if(generation===lifecycle){restoreDraft(scopeId,completed ? unsent : joinDrafts(submitted,unsent));
          activeDraft=null;syncComposer(scopeId);}
        }
      }
    }
    function close(){
      lifecycle++;renderRevision++;clearTimeout(collectTimer);collectTimer=null;collected=null;activeDraft=null;
      savedDrafts.clear();state.debouncePending=false;state.controller?.abort();state.controller=null;state.sending=false;
      if(activeReader){Promise.resolve(activeReader.cancel()).catch(()=>{});activeReader=null;}
      runtime.preview=null;runtime.attachmentRevision=(runtime.attachmentRevision||0)+1;clearComposer();runtime.attachmentScope="";
    }
  return {renderChat,sessionSidebar,attachmentChips,sendChat,releaseAttachments,removeAttachment,flushCollected,cancelCollected,saveDraft,parkDraft,activateScope,
    handleAction:(action,target)=>workspace?.handleAction(action,target)||false,
    handleSubmit:(kind,form)=>workspace?.handleSubmit(kind,form)||false,
    handleInput:target=>workspace?.handleInput(target)||false,
    close};
}};
