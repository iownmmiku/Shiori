"use strict";

window.ShioriExperienceHelp={create(runtime,helpers){
  const {safeLink}=helpers;
    function markdown(text) {
      const lines = String(text || "").split(/\r?\n/), result = [], toc = [];
      let code = [], inCode = false, paragraph = [], list = false, count = 0;
      const inline = value => e(value).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\[([^\]]+)\]\(([^\s)]+)\)/g,(all,title,url) => { const decoded = url.replaceAll("&amp;","&"); return safeLink(decoded) || /^#[a-zA-Z0-9_-]+$/.test(decoded) ? `<a href="${e(decoded)}" ${decoded.startsWith("http") ? 'target="_blank" rel="noopener noreferrer"' : ""}>${title}</a>` : title; });
      const flush = () => { if (paragraph.length) result.push(`<p>${inline(paragraph.join("\n"))}</p>`); paragraph = []; if (list) result.push("</ul>"); list = false; };
      for (const line of lines) {
        if (/^```/.test(line)) { flush(); if (inCode) { result.push(`<pre>${e(code.join("\n"))}</pre>`); code=[]; } inCode = !inCode; continue; }
        if (inCode) { code.push(line); continue; }
        const headingMatch = /^(#{1,4})\s+(.+)$/.exec(line), bullet = /^\s*(?:[-*]|\d+\.)\s+(.+)$/.exec(line);
        if (headingMatch) { flush(); const id = `guide-${++count}`, level = Math.max(2,Math.min(3,headingMatch[1].length)); result.push(`<h${level} id="${id}">${inline(headingMatch[2])}</h${level}>`); if (headingMatch[1].length <= 2) toc.push(`<a href="#${id}">${e(headingMatch[2])}</a>`); }
        else if (bullet) { if (paragraph.length) { result.push(`<p>${inline(paragraph.join("\n"))}</p>`); paragraph=[]; } if (!list) { result.push("<ul>"); list=true; } result.push(`<li>${inline(bullet[1])}</li>`); }
        else if (!line.trim()) flush(); else { if (list) { result.push("</ul>"); list=false; } paragraph.push(line); }
      }
      flush(); if (code.length) result.push(`<pre>${e(code.join("\n"))}</pre>`);
      return `<nav class="guide-toc" aria-label="指南目录">${toc.join("")}</nav><article class="guide-document">${result.join("")}</article>`;
    }

    async function renderHelp() { const response = await api("/static/guide.md",{raw:true}), text = await response.text(); return heading("HELP","使用指南","从第一次聊天，到 QQ 连接、人格蒸馏、电脑任务和源码部署。",button("运行诊断","go-diagnostics","","shield")) + markdown(text); }

    async function renderOverview() {
      const diagnostics = await api(`/api/diagnostics?${query({scope_id:state.scope})}`), model = configuredModel(), qq = diagnostics.qq || state.boot?.qq || {};
      const connected = qq.status?.connected || qq.connected;
      return heading("SHIORI","机器人管理","先完成一次聊天，再按需要接入 QQ、创建人格和开启电脑任务。",button("开始聊天","go-chat","primary","chat")) + `<section class="home-intro"><div class="home-identity"><div class="shiori-mark" aria-hidden="true">m<span>♪</span></div><div><h2>${e(state.settings.identity?.name || "初音未来")}，已就绪。</h2><p>陪你聊天，记住值得记住的事，也能在你的授权下完成任务。默认人格是初音未来，配置随时可以调整。</p></div></div>${button("使用指南","go-help","","arrow")}</section><div class="status-cards">${[
        ["spark","语言模型",diagnostics.model?.configured ? "已配置" : "等待连接",model.model || "填入自己的服务地址和密钥，就能开始自然聊天。","quick-model","配置模型"],
        ["groups","QQ 连接",connected ? "已连接" : qq.enabled ? "正在等待连接" : "尚未启用","本机 NapCat、SnowLuma 支持自动配对，也可以使用 QQ 官方机器人。","local-qq","连接 QQ"],
        ["tools","电脑任务",state.settings.computer?.enabled ? "已开启" : "尚未开启","仅已认证的本地账户和你指定的 QQ 管理员可以发动。","computer-settings","管理权限"]
      ].map(([symbol,title,status,description,action,actionText]) => `<article class="status-card"><h3>${icon(symbol)}${e(title)}</h3><strong>${e(status)}</strong><p>${e(description)}</p>${button(actionText,action,"small")}</article>`).join("")}</div>${window.ShioriQuickStart.diagnosticsPanel(diagnostics)}<section class="panel" style="margin-top:20px"><div class="panel-head"><div><h2>按需要，逐步添加能力</h2><p>模型分工、自动学习与群聊规划都可以稍后设置。</p></div></div><div class="panel-body"><div class="distill-options"><button class="distill-option" data-action="start-distill">${icon("persona")}<strong>蒸馏一个人格</strong><small>从作品角色或你的 ZIP 语料中提炼人格，先预览与试聊，再应用。</small></button><button class="distill-option" data-page="jobs">${icon("tasks")}<strong>查看后台任务</strong><small>蒸馏、电脑任务的执行步骤、审批与结果，关闭网页后也能回来查看。</small></button></div></div></section>`;
    }
  return {markdown,renderHelp,renderOverview};
}};
