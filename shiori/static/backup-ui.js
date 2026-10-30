"use strict";
window.ShioriBackup = (() => {
  let timer=null, revision=0, current=null, busy=false;
  const close=()=>{revision++;clearTimeout(timer);timer=null;};
  const live=token=>revision===token && !!$("#backup-progress") && state.authenticated && !state.stopped && $("#modal")?.open;
  function content(job){
    const progress=Math.max(0,Math.min(100,Number(job.progress)||0));
    const complete=job.status==='completed' && /^[a-f0-9]{32}$/.test(job.id || '');
    const size=Math.max(0,Number(job.size)||0), sizeText=size<1048576 ? `${(size/1024).toFixed(1)} KiB` : `${(size/1048576).toFixed(1)} MiB`;
    const restart=button("重新生成","backup-start","ghost","refresh");
    const actions=complete ? `<a class="btn primary" href="/api/backup/jobs/${encodeURIComponent(job.id)}/file" download>下载备份</a>${restart}` : job.can_cancel ? button("取消备份","backup-cancel","ghost","stop") : job.status==='running' ? '' : restart;
    return `<div id="backup-progress" aria-live="polite"><p>${e(job.message || '正在准备备份')}</p><progress aria-label="备份进度" max="100" value="${progress}"></progress><p>${progress}%${job.total ? ` · ${Number(job.processed)||0}/${Number(job.total)||0}` : ''}</p>${complete ? `<p>备份已通过格式与容量检查，共 ${sizeText}。不包含密钥、原始附件、插件工作目录或表情包图片；请按使用指南单独保存这些文件。</p><p class="help-caption">重新生成会替换此临时下载文件，请先保存需要的副本。</p>` : '<p class="help-caption">生成在后台进行，可以关闭窗口稍后查看。取消不会删除现有聊天和记忆。</p>'}<div class="section-actions">${actions}${button('刷新进度','backup-status','ghost','refresh')}</div></div>`;
  }
  function paint(job){current=job;const node=$("#backup-progress");if(node)node.outerHTML=content(job);}
  function poll(token){
    clearTimeout(timer);
    if(!live(token) || current?.status!=='running')return;
    timer=setTimeout(async()=>{
      try{const job=await api('/api/backup/jobs/current');if(live(token))paint(job);}
      catch(error){if(live(token))modalError(error.message);}
      finally{poll(token);}
    },800);
  }
  async function open(start=false){
    close(); const token=revision;
    openModal('生成数据备份','后台生成与校验；完成后下载到你的设备。',content({status:'running',message:'正在读取备份任务…'}));
    try{
      let job=await api('/api/backup/jobs/current');
      if(!live(token))return;
      if(start || job.status==='idle' || !job.id)job=await api('/api/backup/jobs',{method:'POST',body:{}});
      if(!live(token))return;
      paint(job);poll(token);
    }catch(error){if(live(token)){paint({status:'failed',message:error.message});modalError(error.message);}}
  }
  async function action(name){
    if(!['download-backup','backup-start','backup-status','backup-cancel'].includes(name))return false;
    if(busy)return true;
    busy=true;
    try{
      if(name==='download-backup' || name==='backup-start')await open(name==='backup-start');
      else{
        const token=revision;
        const job=name==='backup-cancel' && current?.can_cancel ? await api(`/api/backup/jobs/${encodeURIComponent(current.id)}/cancel`,{method:'POST',body:{}}) : await api('/api/backup/jobs/current');
        if(live(token)){paint(job);poll(token);}
      }
    }finally{busy=false;}
    return true;
  }
  return {handleAction:action,close};
})();
