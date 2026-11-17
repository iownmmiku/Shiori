"use strict";
window.ShioriStickers=(()=>{
  const ui=window.ShioriExperience,oldAction=ui.handleAction,oldSubmit=ui.handleSubmit;
  // Fixed categories; the server sends the same list with a guide for each.
  const fallbackCategories=['开心','赞同','感谢','安慰','疑惑','惊讶','无语','难过','生气','问候','害羞','得意','嫌弃','委屈','其他','待标注'];
  const MAX_UPLOAD=32*1024*1024;
  const percent=value=>Math.round((Number(value)||0)*100)+'%';
  const categoriesOf=page=>(page && Array.isArray(page.categories) && page.categories.length)?page.categories:fallbackCategories;
  function capacityPanel(capacity,cfg){
    const total=capacity.total ?? 0,limit=capacity.limit ?? cfg.max_items ?? 1000,over=capacity.over ?? 0,auto=capacity.auto_prune!==false;
    const status=over?`已超出上限 ${over} 张。${auto?'下次收集时会先清理最不合适的未锁定表情，也可以现在整理。':'自动清理已关闭，达到上限后停止收集。'}`
      :total>=limit?(auto?'已达上限：新收集的表情会替换最不合适的未锁定表情。':'已达上限，自动清理已关闭，停止收集新表情。')
      :`还可收集 ${limit-total} 张。`;
    return `<section class="panel settings-section"><h3>表情包库 ${e(total)} / ${e(limit)} 张 · 已锁定 ${e(capacity.locked ?? 0)} 张</h3><p>${e(status)}</p><p class="help-caption">清理顺序：语义不明确的 → 已关闭自动匹配的 → 语义匹配度最低的。匹配度来自每次选图时表情含义与聊天内容的重合程度，以及被选中的比例；新收集的明确表情有 3 天保护期。锁定的表情（包括自己上传的）永远不会被自动清理。</p>${over?`<div class="record-actions">${button('立即整理','sticker-prune','primary small','')}</div>`:''}</section>`;
  }
  function analysisText(a){
    a=a || {};
    if(a.running)return `正在分析：${a.done ?? 0} / ${a.total ?? 0} 张已完成${a.failed?`，${a.failed} 张失败`:''}。可以离开这个页面，分析会在后台继续。`;
    const last=a.finished_at && a.message?`上次：${a.message}。`:'';
    return `${last}有 ${a.pending ?? 0} 张自己上传的表情还没分析语义。`;
  }
  function uploadPanel(categories,analysis,guide){
    const options=[['','按压缩包里的文件夹归类，否则待标注'],...categories.filter(x=>x!=='待标注').map(x=>[x,x])];
    const guideList=categories.map(name=>`<li><b>${e(name)}</b>：${e((guide || {})[name] || '')}</li>`).join('');
    const actions=analysis.running?button('停止分析','sticker-analysis-stop','small','')
      :button(`分析待处理的上传表情（${analysis.pending ?? 0} 张）`,'sticker-analyze-pending','primary small','spark',(analysis.pending ?? 0)?'':'disabled')+button('重新分析当前筛选的表情','sticker-analyze-filtered','small','refresh');
    return `<section class="panel settings-section"><h3>上传自己的表情包</h3><p>支持 PNG、JPEG、WebP、GIF 图片和 ZIP 压缩包，单张最大 5MB，单次最多 32MB。压缩包里用分类命名的文件夹（如“害羞”，或 shy、happy 这类英文名）会自动归到对应分类。上传的表情自动锁定，所有聊天都能用；分析出明确语义后才会自动发送。</p><form data-form="sticker-upload"><div class="form-grid"><label class="field"><span>选择图片或压缩包</span><input type="file" name="files" multiple accept="image/png,image/jpeg,image/webp,image/gif,.zip,application/zip" required></label>${select('category','放入分类',options,'')}</div><div class="section-actions"><button type="submit" class="btn primary">上传</button></div></form></section><section class="panel settings-section"><h3>语义分析</h3><p id="sticker-analysis-status">${e(analysisText(analysis))}</p><p class="help-caption">用识图模型为表情写含义、分类、标签和图中文字，动图会取几帧一起看；上传时选的分类会作为参考。手动分析不占每日自动识图次数，但每张都会调用一次模型并计入用量。分析过程中你手动改过的表情不会被覆盖。</p><div class="record-actions">${actions}</div><details><summary>固定分类说明</summary><ul class="help-caption">${guideList}</ul></details></section>`;
  }
  function source(item){
    if(item.origin==='upload')return '自己上传'+(item.filename?` · ${item.filename}`:'');
    return '来自 '+scopeName(item.scope_id);
  }
  function card(item){
    const match=item.match || {},usable=item.clear&&item.enabled,uploaded=item.origin==='upload';
    const progress=uploaded&&item.analysis_status==='pending'?tag('待分析','gray'):uploaded&&item.analysis_status==='failed'?tag('分析失败','gray'):'';
    const tags=[tag(usable?'语义明确':'不自动发送',usable?'':'gray'),progress,item.locked?tag('已锁定','gold'):'',item.prune_next?tag('下次优先清理','gray'):''].join('');
    const matchText=!usable&&!match.seen?'未参与匹配（不会自动发送）':`语义匹配度 ${percent(match.score)}（内容重合 ${match.matched ?? 0}/${match.seen ?? 0}，被选中 ${match.chosen ?? 0}/${match.offered ?? 0}）`;
    return `<article class="record-card"><img class="sticker-preview" src="/api/stickers/${encodeURIComponent(item.id)}/image" alt="${e(item.meaning || '待标注表情')}" loading="lazy"><div class="record-card-head"><h3>${e(item.category)}</h3><div class="inline-stack">${tags}</div></div><p class="record-text">${e(item.meaning || (uploaded?'还没有分析语义。':'语义不明确，请手动标注。'))}</p><p class="help-caption">${e(item.tags.join('、'))}${item.visible_text?` · 图中文字：${e(item.visible_text)}`:''}</p><small>${e(source(item))} · 已发送 ${e(item.use_count)} 次 · ${e(matchText)}</small><div class="record-actions">${button(item.locked?'解除锁定':'锁定','sticker-lock','small',item.locked?'':'check',`data-id="${e(item.id)}"`)}${button('编辑语义与分类','sticker-edit','small','edit',`data-id="${e(item.id)}"`)}${button('分析语义','sticker-analyze-one','small','spark',`data-id="${e(item.id)}"`)}${button('删除','sticker-delete','ghost small','trash',`data-id="${e(item.id)}"`)}</div></article>`;
  }
  function watchAnalysis(){
    if(typeof setTimeout!=='function')return;
    clearTimeout(state.stickerPoll);
    state.stickerPoll=setTimeout(async()=>{
      if(state.page!=='persona' || state.botTab!=='stickers')return;
      let status;
      try{status=await api('/api/stickers/analysis');}catch{return;}
      const node=typeof document!=='undefined'?document.getElementById('sticker-analysis-status'):null;
      if(status.running){if(node)node.textContent=analysisText(status);watchAnalysis();}
      else{toast(status.message || '语义分析已结束');await renderPage();}
    },2500);
  }
  function filterParams(filters){
    return {scope_id:filters.mine?state.scope:'',category:filters.category || '',q:filters.q || '',locked:filters.locked || '',origin:filters.origin || ''};
  }
  async function render(){
    const cfg=state.settings.stickers || {},filters=state.stickerFilters || {};
    const key=JSON.stringify([state.scope,filters]);
    if(state.stickerPageKey!==key){state.stickerOffset=0;state.stickerPageKey=key;}
    const page=await api('/api/stickers?'+query({...filterParams(filters),offset:state.stickerOffset || 0,limit:50}));
    if(page.offset>0 && page.offset>=page.total){state.stickerOffset=page.total?Math.floor((page.total-1)/50)*50:0;return render();}
    state.stickerItems=page.items;state.stickerTotal=page.total;
    const categories=categoriesOf(page),analysis=page.analysis || {};
    state.stickerCategories=categories;state.stickerGuide=page.guide || {};state.stickerAnalysis=analysis;
    if(analysis.running)watchAnalysis();
    const models=(state.settings.providers || []).filter(x=>x.model_type!=="embedding"&&x.enabled!==false&&x.vision).map(x=>[x.id,x.name || x.model]);
    const controls=`<section class="panel settings-section"><h2>表情包</h2><p>表情包有两个来源：收到群聊图片后按概率识图收集，以及你自己上传。所有聊天共用一个表情包库。在 QQ 群聊和私聊里，对方要表情包时，聊天模型会按想表达的情绪挑一张语义明确的发出；另外按发送概率，允许它在合适的回复后主动配一张。“待标注”不会发送。</p><form data-form="sticker-settings"><div class="form-grid">${toggle('enabled','启用表情包','收集与发送的总开关。',cfg.enabled!==false)}${toggle('collect_enabled','收集群聊表情包','识图会使用所选模型，受每日识图次数与全局预算限制。',cfg.collect_enabled!==false)}${field('collect_probability','收集概率',cfg.collect_probability ?? .1,'number','0.1 表示10%；0关闭采样，1每张都尝试。','min="0" max="1" step="0.01"')}${toggle('send_enabled','发送表情包','开启后聊天模型可以发表情包，在文字回复送达后单独发出；会话需要允许使用工具。同一个聊天里刚发过的表情，会先换别的。',cfg.send_enabled!==false)}${field('send_probability','主动发送概率',cfg.send_probability ?? .1,'number','每次回复有这个概率允许模型主动配一张，它觉得不合适就不发；对方要表情包时不受概率限制。0 表示只在被要求时发。','min="0" max="1" step="0.01"')}${select('model_id','识图模型',[['','使用默认聊天模型（需支持视觉）'],...models],cfg.model_id || '','data-full')}${field('daily_model_calls','每日模型调用总上限',cfg.daily_model_calls ?? 60,'number','用于自动识图；发表情包由聊天模型在回复时一并决定，不额外调用模型。0停止自动识图。失败也占一次尝试。手动分析不计入。','min="0" max="10000"')}${field('daily_recognitions','每日最多识图次数',cfg.daily_recognitions ?? 30,'number','只限制自动收集时的识图；0表示停止。模型失败也占一次识图尝试，避免反复计费。','min="0" max="1000"')}${field('max_items','表情包上限',cfg.max_items ?? 1000,'number','超过后按顺序清理未锁定的表情：语义不明确、已关闭匹配、语义匹配度低。图片总大小另有128MB上限。','min="1" max="5000"')}${toggle('auto_prune','超出上限时自动清理','关闭后，达到上限就停止收集，不删除任何表情。锁定的表情始终保留。',cfg.auto_prune!==false)}</div><div class="section-actions"><button type="submit" class="btn primary">保存表情包设置</button></div></form><p class="help-caption">请在模型供应商中为识图模型开启视觉能力。群聊收集单张最大2MB，自己上传单张最大5MB。官方 QQ 通道发送静态首帧。表情包库需要单独备份；普通应用备份不包含这些图片。</p><a class="btn ghost small" href="/api/stickers/backup" download="shiori-stickers.sqlite3">备份表情包库</a></section>`;
    const filter=`<form data-form="sticker-filter" class="toolbar">${field('q','搜索语义、标签或图中文字',filters.q || '','search')}${select('category','分类',[['','全部分类'],...categories.map(x=>[x,x])],filters.category || '')}${select('origin','来源',[['','全部来源'],['upload','自己上传'],['collected','群聊收集']],filters.origin || '')}${select('locked','锁定',[['','全部'],['true','只看已锁定'],['false','只看未锁定']],filters.locked || '')}${toggle('mine','只看当前会话收集的','表情包库是所有聊天共用的；打开后只列出从当前会话收集到的。',filters.mine===true)}<button type="submit" class="btn small">筛选</button></form>`;
    const cards=page.items.map(card).join('');
    const settings=`<details class="panel settings-section"><summary>收集与发送设置 · 收集 ${Math.round((cfg.collect_probability ?? .1)*100)}% · 发送 ${Math.round((cfg.send_probability ?? .1)*100)}% · 上限 ${e(cfg.max_items ?? 1000)} 张</summary>${controls}</details>`;
    const quota=page.quota || {};
    const usage=`<section class="panel settings-section"><h3>今日表情模型调用 ${e(quota.total ?? 0)} / ${e(quota.limit ?? cfg.daily_model_calls ?? 60)}</h3><p>识图 ${e(quota.vision ?? 0)} 次 · 已记录 ${e(quota.tokens ?? 0)} Token</p><p class="help-caption">${e(quota.timezone || state.settings.time?.timezone || 'Asia/Shanghai')} 自然日；自动识图受这个上限限制，手动分析只计入 Token 用量；发表情包不调用模型。模型用量可能包含估算，失败尝试不代表免费；服务商账单为准。</p></section>`;
    const empty=filters.q || filters.category || filters.locked || filters.origin ? '<p>没有符合筛选的表情包，请更换条件。</p>' : '<p>还没有表情包。可以在上面上传自己的表情，或连接群聊并选好视觉模型后按概率收集。</p>';
    return usage+capacityPanel(page.capacity || {},cfg)+uploadPanel(categories,analysis,page.guide)+settings+filter+`<p>共 ${e(page.total)} 张 · 第 ${Math.floor(page.offset/50)+1} 页</p><div class="record-grid">${cards || empty}</div><div class="record-actions">${button('上一页','sticker-page','ghost small','',`data-offset="${Math.max(0,page.offset-50)}" ${page.offset===0?'disabled':''}`)}${button('下一页','sticker-page','ghost small','',`data-offset="${page.offset+50}" ${page.has_more?'':'disabled'}`)}</div>`;
  }
  async function startAnalysis(body,title,text){
    await confirmAction(title,text,async()=>{
      const status=await api('/api/stickers/analysis',{method:'POST',body});
      toast(`已开始分析 ${status.total} 张表情`);await renderPage();
    },'开始分析');
  }
  ui.handleAction=async(action,target)=>{
    if(action==='sticker-page'){state.stickerOffset=Number(target.dataset.offset)||0;await renderPage();return true;}
    if(action==='sticker-prune'){
      const preview=await api('/api/stickers/prune',{method:'POST',body:{dry_run:true}});
      if(!preview.removed){toast(preview.blocked?'超出的部分全部已锁定，没有可清理的表情。':'表情包库没有超出上限。');await renderPage();return true;}
      await confirmAction(`清理 ${preview.removed} 张表情包？`,`将按顺序清理最不合适的未锁定表情${preview.blocked?'；其余超出的表情都已锁定，会保留':''}。清理后同一张图片不会再被收集。`,async()=>{const result=await api('/api/stickers/prune',{method:'POST',body:{}});toast(`已清理 ${result.removed} 张表情包`);await renderPage();},'清理');
      return true;
    }
    if(action==='sticker-analyze-pending'){
      const count=(state.stickerAnalysis || {}).pending ?? 0;
      await startAnalysis({},`分析 ${count} 张上传的表情？`,'每张调用一次识图模型，写出含义、分类、标签和图中文字；分析在后台进行，可以随时停止。');
      return true;
    }
    if(action==='sticker-analyze-filtered'){
      const count=Math.min(state.stickerTotal ?? 0,300);
      if(!count)throw new Error('当前筛选下没有表情包。');
      await startAnalysis({filters:filterParams(state.stickerFilters || {})},`重新分析当前筛选的 ${count} 张表情？`,'会覆盖这些表情现在的含义、分类和标签（一次最多 300 张），每张调用一次识图模型。分析过程中你手动改过的不会被覆盖。');
      return true;
    }
    if(action==='sticker-analysis-stop'){const status=await api('/api/stickers/analysis/stop',{method:'POST',body:{}});toast(status.running?'正在停止：当前这张分析完就结束':'分析已结束');await renderPage();return true;}
    if(action==='sticker-edit'||action==='sticker-delete'||action==='sticker-lock'||action==='sticker-analyze-one'){
      const item=(state.stickerItems || []).find(x=>x.id===target.dataset.id);
      if(!item)throw new Error('请刷新表情包库后重试。');
      if(action==='sticker-analyze-one'){const status=await api('/api/stickers/analysis',{method:'POST',body:{ids:[item.id]}});toast(`已开始分析 ${status.total} 张表情`);await renderPage();}
      else if(action==='sticker-lock'){await api('/api/stickers/'+encodeURIComponent(item.id),{method:'PUT',body:{locked:!item.locked}});toast(item.locked?'已解除锁定，超出上限时可能被清理':'已锁定，不会被自动清理');await renderPage();}
      else if(action==='sticker-delete')await confirmAction('删除这个表情包？',item.origin==='upload'?'删除后它不会再被发送；以后重新上传同一张图片可以恢复。':'删除后，在任何聊天里都不会再收集这张图片。',async()=>{await api('/api/stickers/'+encodeURIComponent(item.id),{method:'DELETE'});await renderPage();},'删除表情包');
      else{
        const categories=state.stickerCategories || fallbackCategories,guide=state.stickerGuide || {};
        openModal('编辑表情包','明确语义后才能用于自动回复；分类只能从固定分类中选。',`<img class="sticker-preview" src="/api/stickers/${encodeURIComponent(item.id)}/image" alt="表情包预览">${textarea('meaning','含义',item.meaning,'描述表达的情绪、态度及适合回应什么样的话。','maxlength="240"')}${select('category','分类',categories.map(x=>[x,guide[x]?`${x}（${guide[x]}）`:x]),item.category)}${field('tags','标签',item.tags.join('，'),'text','逗号分隔，最多8个。')}${toggle('clear','语义明确','不明确或无意义的表情请关闭。',item.clear)}${toggle('enabled','允许自动匹配',item.origin==='upload'?'仍受发送开关、概率和上下文判断约束。':'仍受发送开关、概率和上下文判断约束；原消息被删除后不再使用。',item.enabled)}${toggle('locked','锁定','超出上限时不会被自动清理；仍可手动删除。',item.locked===true)}`,'sticker-edit',item,'保存语义');
      }
      return true;
    }
    return oldAction(action,target);
  };
  ui.handleSubmit=async(kind,form)=>{
    if(kind==='sticker-upload'){
      const data=new FormData(form),files=data.getAll('files').filter(file=>file && file.size);
      if(!files.length)throw new Error('请选择图片或 ZIP 压缩包。');
      if(files.reduce((sum,file)=>sum+file.size,0)>MAX_UPLOAD)throw new Error('单次上传不能超过 32MB，请分批上传。');
      toast('正在上传并检查图片…');
      const result=await api('/api/stickers/upload',{method:'POST',body:data});
      const skipped=result.rejected_count?`；${result.rejected_count} 项未导入：${result.rejected.slice(0,3).map(x=>`${x.name}（${x.reason}）`).join('；')}${result.rejected_count>3?' 等':''}`:'';
      toast(`已添加 ${result.added} 张${result.duplicates?`，${result.duplicates} 张已在库中`:''}${skipped}`,Boolean(result.rejected_count && !result.added));
      await renderPage();return true;
    }
    const data=getForm(form);
    if(kind==='sticker-settings'){
      await api('/api/settings',{method:'PUT',body:{stickers:{...data,collect_probability:Number(data.collect_probability),send_probability:Number(data.send_probability),daily_recognitions:Number(data.daily_recognitions),...(data.daily_model_calls!==undefined?{daily_model_calls:Number(data.daily_model_calls)}:{}),max_items:Number(data.max_items)}}});
      toast('表情包设置已保存');await bootstrap();return true;
    }
    if(kind==='sticker-filter'){state.stickerFilters=data;await renderPage();return true;}
    if(kind==='sticker-edit'){
      await api('/api/stickers/'+encodeURIComponent(state.modalRecord.id),{method:'PUT',body:{...data,tags:splitNames(data.tags)}});
      closeModal();toast('语义与分类已更新');await renderPage();return true;
    }
    return oldSubmit(kind,form);
  };
  return {render};
})();
