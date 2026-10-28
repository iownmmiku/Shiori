"use strict";

window.ShioriExperienceConfig={create(runtime,helpers){
  const {safeId}=helpers;
    async function renderSettings() {
      const [base,admins,security] = await Promise.all([window.ShioriExperience.baseSettings(),api("/api/admins"),window.ShioriAuth ? window.ShioriAuth.settingsPanel() : ""]);
      state.admins = rows(admins); const computer = state.settings.computer || {};
      const adminPanel = `<section class="panel settings-section section-anchor" id="admins-section"><div class="section-title">${icon("shield")}<div><h2>管理员账号</h2><p>谁可以在对话中发动电脑任务与管理操作。</p></div></div><div class="admin-row"><div><strong>Shiori ${tag("内置管理台管理员")}</strong><small>通过桌面或浏览器认证的管理台账户，始终保留。</small></div></div>${state.admins.filter(item => item.platform !== "local" && item.id !== "shiori").map(item => `<div class="admin-row"><div><strong>${e(item.label || item.name || "QQ 管理员")}</strong><small>${e(item.platform || "qq")} · ${e(item.user_id || item.account_id || "")}${item.instance_id ? " · " + e(item.instance_id) : ""}</small></div>${button("移除","remove-admin","ghost small","trash",`data-id="${e(item.id)}"`)}</div>`).join("")}<div class="section-actions">${button("60秒快速配对","pair-admin-auto","primary","link")}${button("手动核对配对","pair-admin","","link")}${button("手动添加账号","new-admin","","plus")}</div><p class="help-caption">使用真实发送者 ID 判断权限，昵称、群主身份、转发及引用不授予权限。允许聊天名单在 QQ 连接中单独设置。</p></section>`;
      const computerPanel = `<section class="panel settings-section section-anchor" id="computer-section"><div class="section-title">${icon("tools")}<div><h2>电脑任务</h2><p>本机命令、Python 与文件操作；任务步骤与运行结果保存在聊天中。</p></div></div>${notice("本机模式使用当前系统账户权限，工作目录不是沙箱。Docker 模式在本机 Linux 容器中断网运行代码，仅映射选定工作目录；需要先准备 Docker 与镜像，不可用时停止，不会降级为本机执行。Docker 隔离开启时默认禁止沙箱外 MCP 工具，可在高级设置明确放行。管理员任务按所选审批方式执行。","warning")}<form data-form="computer-settings">${toggle("enabled","开启电脑任务","仅管理员可发起，按下方执行环境与确认方式运行；可随时停止。",computer.enabled)}<div class="form-grid" style="margin-top:15px">${select("runtime","执行环境",[["local","本机账户权限"],["docker","Docker 隔离执行（默认断网）"]],computer.runtime || "docker","data-full")}${field("workspace","工作目录",computer.workspace || "","text","请填写已有的绝对路径；留空使用数据目录下的 workspace。文件工具限制在此目录；仅本机模式的 Shell 与 Python 使用本机账户权限。","data-full")}</div><details class="details"><summary>高级执行限制</summary><div class="form-grid" style="margin-top:12px">${toggle("allow_external_tools","允许沙箱外 MCP 工具","Docker 隔离时默认关闭。开启后 MCP 按服务自身权限运行，不受代码沙箱保护；其他管理员和服务允许列表仍有效。",computer.allow_external_tools)}${select("approval_mode","操作确认",[["automatic","自动执行（仅管理员）"],["per_action","每步确认"]],computer.approval_mode || "automatic")}${field("max_steps","每个任务最多步骤",computer.max_steps ?? 12,"number","达到上限后停止继续操作。","min=\"1\" max=\"100\"")}${field("timeout_seconds","单次操作超时（秒）",computer.timeout_seconds ?? 60,"number","较长任务可在记录中查看结果。","min=\"1\" max=\"600\"")}${field("max_output_chars","最多记录输出字符",computer.max_output_chars ?? 16000,"number","过长输出会限制展示。","min=\"1000\" max=\"100000\"")}</div></details><div class="section-actions"><button class="btn primary" type="submit">保存电脑任务设置</button>${button("检查隔离环境","runtime-check","","shield")}${button("打开任务聊天","computer-chat","","chat")}</div></form></section>`;
      const updates = window.ShioriUpdates ? await window.ShioriUpdates.settingsPanel() : "";
      return base.replace('<div class="settings-main">','<div class="settings-main">' + updates + security + adminPanel + computerPanel + schedulerPanel()).replace('<form data-form="qq">',localConnectorPanel() + (window.ShioriPages ? window.ShioriPages.qqIngress(state.boot?.qq) : "") + '<form data-form="qq">');
    }

    function localConnectorPanel() {
      return `<div class="quick-connector-guide"><strong>连接本机 NapCat / SnowLuma</strong><p>连接器已安装并登录 QQ 后，可自动创建反向 WebSocket，保留其他连接配置。</p>${button("自动配对本机 QQ","local-qq","primary small","link")} ${button("连通性自检","qq-self-test","ghost small","shield")}</div>`;
    }

    function schedulerPanel() {
      const cfg=state.settings.scheduler || {};
      return `<section class="panel settings-section"><h2>并发与排队</h2><p class="help-caption">同一会话保持顺序；前台聊天优先于后台学习。服务商有限流时保持默认并发1。</p><form data-form="queue-settings"><div class="form-grid">${field('model_concurrency','每个模型并发数',cfg.model_concurrency ?? 1,'number','','min="1" max="8"')}${field('model_queue_limit','每个模型最多排队请求',cfg.model_queue_limit ?? 32,'number','','min="1" max="200"')}${field('background_queue_limit','后台学习排队上限',cfg.background_queue_limit ?? 16,'number','','min="1" max="100"')}${field('scope_queue_limit','每个会话排队上限',cfg.scope_queue_limit ?? 8,'number','','min="1" max="32"')}</div><button class="btn" type="submit">保存调度设置</button></form></section><section class="panel settings-section"><h2>时间与日配额</h2><form data-form="timezone-settings"><div class="form-grid">${field('timezone','时区（IANA）',state.settings.time?.timezone || 'Asia/Shanghai','text','日预算、Planner、表情包配额和统计使用此时区；默认 UTC+8。','required list="timezone-list" data-full')}<datalist id="timezone-list"><option value="Asia/Shanghai"><option value="Asia/Hong_Kong"><option value="UTC"><option value="America/New_York"><option value="Europe/London"></datalist></div><button class="btn" type="submit">保存时区</button></form></section>`;
    }

    async function pollPairing() {
      if(!runtime.pair?.automatic || !$("#modal")?.open)return;
      const id=runtime.pair.id, left=Math.max(0,Math.ceil(runtime.pair.expires_at-Date.now()/1000));
      if(!left){$("#pairing-result").textContent="配对模式已结束，请重新开启。";return;}
      try {
        const result=await api(`/api/admins/pairing/${safeId(id)}`);
        if(runtime.pair?.id!==id || !$("#modal")?.open)return;
        if(result.status==='bound'){runtime.pair=null;closeModal();toast('QQ管理员已自动绑定，可在管理员列表撤销。');await bootstrap();return;}
        $("#pairing-result").textContent=`剩余 ${left} 秒。用目标QQ私聊发送上方完整命令，持码账号将自动成为管理员。`;
        runtime.pairTimer=setTimeout(pollPairing,1000);
      } catch(error){const node=$("#pairing-result");if(node)node.textContent=error.message;}
    }
  return {renderSettings,localConnectorPanel,schedulerPanel,pollPairing};
}};
