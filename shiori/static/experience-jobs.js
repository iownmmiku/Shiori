"use strict";

window.ShioriExperienceJobs={create(runtime,helpers){
  const {jobNames,inactive,safeId,safeLink}=helpers;
    function jobDetail(job,compact = false) {
      const approval = job.approval, result = job.result || {}, events = Array.isArray(job.events) ? job.events : [];
      const eventHTML = item => {
        const data = item.data && typeof item.data === "object" ? item.data : item;
        const names = {started:"开始处理",progress:"处理进度",authorized:"按已开启权限执行",approval:"等待审批",approved:"已批准",tool_result:"操作结果",completed:"完成",stopped:"已停止",failed:"处理失败"};
        const output = data.output ?? data.content ?? data.result;
        const text = typeof output === "string" ? output : output == null ? "" : toolJSON(output,10000);
        return `<div class="job-step"><strong>${e(data.stage || names[item.type] || item.type || "进度")}</strong> ${e(data.message || data.summary || data.tool || "")}${text ? `<pre>${e(String(text).slice(0,10000))}</pre>` : ""}</div>`;
      };
      return `<article class="${compact ? "chat-inline-progress" : "record-card job-card"}" data-job-id="${e(job.id)}"><div class="record-card-head"><div><h3>${e(job.title || (job.kind === "distillation" ? "人格蒸馏" : "电脑任务"))}</h3><p class="help-caption">${e(job.stage || "")} ${job.message ? "· " + e(job.message) : ""}</p></div>${tag(jobNames[job.status] || job.status,job.status === "awaiting_approval" ? "gold" : job.status === "failed" ? "red" : "gray")}</div>${job.error ? notice(job.error,"danger") : ""}${result.warnings?.length ? notice(result.warnings.join("；"),"warning") : ""}${approval && job.status === "awaiting_approval" ? `<div class="notice warning">${icon("shield")}<p>${e(approval.summary || "下一步需要你的确认")}</p></div><pre class="tool-argument-preview">${e(toolJSON(approval.details || {},4000))}</pre>${button("查看并批准本次动作","job-approve","primary small","shield",`data-id="${e(job.id)}" data-approval="${e(approval.id)}"`)}` : ""}${events.length && !compact ? `<details class="details" ${!inactive(job) ? "open" : ""}><summary>执行记录（${events.length}）</summary>${events.slice(-30).map(eventHTML).join("")}</details>` : ""}${result.reply || result.summary ? `<p class="record-text">${e(result.reply || result.summary)}</p>` : ""}${result.persona || result.system_prompt || result.persona_text || result.personality || result.prompt ? `<div class="persona-draft">${e(result.persona?.system_prompt || result.system_prompt || result.persona_text || result.personality || result.prompt)}</div>` : ""}<div class="record-actions">${!inactive(job) ? button("停止任务","job-cancel","ghost small","stop",`data-id="${e(job.id)}"`) : ""}${job.resumable ? button("从检查点继续","job-resume","small","refresh",`data-id="${e(job.id)}"`) : ""}${compact ? button("查看完整任务","view-job","ghost small","arrow",`data-id="${e(job.id)}"`) : ""}${job.kind === "distillation" && job.status === "completed" ? button("试聊人格草稿","trial-persona","small","chat",`data-id="${e(job.id)}"`) + button("保存为人格副本","apply-distill","primary small","persona",`data-id="${e(job.id)}"`) + (result.artifact || result.skill_available ? `<a class="btn small" href="/api/persona/distill/${safeId(job.id)}/artifact" download>下载 Skill 包</a>` : "") : ""}${(result.files || result.artifacts || []).map(file => { const url = safeLink(file.url || file.download_url); return url ? `<a class="job-file-link" href="${e(url)}" download>${icon("file")}${e(file.name || "下载结果")}</a>` : ""; }).join("")}</div><p class="help-caption">${e(fmt(job.updated_at || job.created_at,true))}${job.id ? " · 任务 " + e(job.id) : ""}</p></article>`;
    }

    async function renderJobs() {
      const jobs = rows(await api("/api/jobs")); state.liveJobs = jobs;
      return heading("TASKS","后台任务","查看执行步骤与实际结果，正在运行的任务可随时停止。",button("刷新任务","refresh","","refresh")) + (jobs.length ? `<div id="jobs-list">${jobs.map(job => jobDetail(job)).join("")}</div>` : empty("tasks","还没有后台任务","在聊天中选择“电脑任务”，或在人格页开始一次蒸馏。","go-chat","开始聊天"));
    }
  return {jobDetail,renderJobs};
}};
