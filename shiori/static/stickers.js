"use strict";
window.ShioriStickers=(()=>{
  const ui=window.ShioriExperience,oldAction=ui.handleAction,oldSubmit=ui.handleSubmit;
  const categories=['开心','赞同','感谢','安慰','疑惑','惊讶','无语','难过','生气','问候','其他','待标注'];
  const percent=value=>Math.round((Number(value)||0)*100)+'%';
  function capacityPanel(capacity,cfg){
    const total=capacity.total ?? 0,limit=capacity.limit ?? cfg.max_items ?? 1000,over=capacity.over ?? 0,auto=capacity.auto_prune!==false;
    const status=over?`已超出上限 ${over} 张。${auto?'下次收集时会先清理最不合适的未锁定表情，也可以现在整理。':'自动清理已关闭，达到上限后停止收集。'}`
      :total>=limit?(auto?'已达上限：新收集的表情会替换最不合适的未锁定表情。':'已达上限，自动清理已关闭，停止收集新表情。')
      :`还可收集 ${limit-total} 张。`;
    return `<section class="panel settings-section"><h3>表情包库 ${e(total)} / ${e(limit)} 张 · 已锁定 ${e(capacity.locked ?? 0)} 张</h3><p>${e(status)}</p><p class="help-caption">清理顺序：语义不明确的 → 已关闭自动匹配的 → 语义匹配度最低的。匹配度来自每次选图时表情含义与聊天内容的重合程度，以及被选中的比例；新收集的明确表情有 3 天保护期。锁定的表情永远不会被自动清理。</p>${over?`<div class="record-actions">${button('立即整理','sticker-prune','primary small','')}</div>`:''}</section>`;
  }
  function card(item){
    const match=item.match || {},usable=item.clear&&item.enabled;
    const tags=[tag(usable?'语义明确':'不自动发送',usable?'':'gray'),item.locked?tag('已锁定','gold'):'',item.prune_next?tag('下次优先清理','gray'):''].join('');
    return `<article class="record-card"><img class="sticker-preview" src="/api/stickers/${encodeURIComponent(item.id)}/image" alt="${e(item.meaning || '待标注表情')}" loading="lazy"><div class="record-card-head"><h3>${e(item.category)}</h3><div class="inline-stack">${tags}</div></div><p class="record-text">${e(item.meaning || '语义不明确，请手动标注。')}</p><p class="help-caption">${e(item.tags.join('、'))}</p><small>来自 ${e(scopeName(item.scope_id))} · 已发送 ${e(item.use_count)} 次 · ${!usable&&!match.seen?'未参与匹配（不会自动发送）':`语义匹配度 ${e(percent(match.score))}（内容重合 ${e(match.matched ?? 0)}/${e(match.seen ?? 0)}，被选中 ${e(match.chosen ?? 0)}/${e(match.offered ?? 0)}）`}</small><div class="record-actions">${button(item.locked?'解除锁定':'锁定','sticker-lock','small',item.locked?'':'check',`data-id="${e(item.id)}"`)}${button('编辑语义与分类','sticker-edit','small','edit',`data-id="${e(item.id)}"`)}${button('删除','sticker-delete','ghost small','trash',`data-id="${e(item.id)}"`)}</div></article>`;
  }
  async function render(){
    const cfg=state.settings.stickers || {},filters=state.stickerFilters || {};
    const key=JSON.stringify([state.scope,filters]);
    if(state.stickerPageKey!==key){state.stickerOffset=0;state.stickerPageKey=key;}
    const page=await api('/api/stickers?'+query({scope_id:filters.mine?state.scope:'',category:filters.category,q:filters.q,locked:filters.locked || '',offset:state.stickerOffset || 0,limit:50}));
    if(page.offset>0 && page.offset>=page.total){state.stickerOffset=page.total?Math.floor((page.total-1)/50)*50:0;return render();}
    state.stickerItems=page.items;
    const models=(state.settings.providers || []).filter(x=>x.model_type!=="embedding"&&x.enabled!==false&&x.vision).map(x=>[x.id,x.name || x.model]);
    const controls=`<section class="panel settings-section"><h2>表情包</h2><p>收到群聊图片后按概率识图，保存图片、语义与分类。所有聊天共用一个表情包库，在任何群或私聊里，回复内容合适时都可以发送语义明确的表情；“待标注”不会自动发送。</p><form data-form="sticker-settings"><div class="form-grid">${toggle('enabled','启用表情包','收集与发送的总开关。',cfg.enabled!==false)}${toggle('collect_enabled','收集群聊表情包','识图会使用所选模型，受每日识图次数与全局预算限制。',cfg.collect_enabled!==false)}${field('collect_probability','收集概率',cfg.collect_probability ?? .1,'number','0.1 表示10%；0关闭采样，1每张都尝试。','min="0" max="1" step="0.01"')}${toggle('send_enabled','自动发送表情包','仅在文字回复送达、语义明确且符合上下文时尝试；所有聊天共用表情包库。',cfg.send_enabled!==false)}${field('send_probability','发送概率',cfg.send_probability ?? .1,'number','每次回复最多一张；概率命中也不保证一定发送。','min="0" max="1" step="0.01"')}${select('model_id','识图模型',[['','使用默认聊天模型（需支持视觉）'],...models],cfg.model_id || '','data-full')}${field('daily_model_calls','每日模型调用总上限',cfg.daily_model_calls ?? 60,'number','识图与发送前选图共用；0停止这两类调用。失败也占一次尝试。','min="0" max="10000"')}${field('daily_recognitions','每日最多识图次数',cfg.daily_recognitions ?? 30,'number','0表示停止识图。模型失败也占一次识图尝试，避免反复计费。','min="0" max="1000"')}${field('max_items','表情包上限',cfg.max_items ?? 1000,'number','超过后按顺序清理未锁定的表情：语义不明确、已关闭匹配、语义匹配度低。图片总大小另有128MB上限。','min="1" max="5000"')}${toggle('auto_prune','超出上限时自动清理','关闭后，达到上限就停止收集，不删除任何表情。锁定的表情始终保留。',cfg.auto_prune!==false)}</div><div class="section-actions"><button type="submit" class="btn primary">保存表情包设置</button></div></form><p class="help-caption">请在模型供应商中为识图模型开启视觉能力。单张支持 PNG/JPEG/WebP/GIF，最大2MB。官方 QQ 通道发送静态首帧。表情包库需要单独备份；普通应用备份不包含这些图片。</p><a class="btn ghost small" href="/api/stickers/backup" download="shiori-stickers.sqlite3">备份表情包库</a></section>`;
    const filter=`<form data-form="sticker-filter" class="toolbar">${field('q','搜索语义或标签',filters.q || '','search')}${select('category','分类',[['','全部分类'],...[...new Set([...categories,...page.categories])].map(x=>[x,x])],filters.category || '')}${select('locked','锁定',[['','全部'],['true','只看已锁定'],['false','只看未锁定']],filters.locked || '')}${toggle('mine','只看当前会话收集的','表情包库是所有聊天共用的；打开后只列出从当前会话收集到的。',filters.mine===true)}<button type="submit" class="btn small">筛选</button></form>`;
    const cards=page.items.map(card).join('');
    const settings=`<details class="panel settings-section"><summary>收集与发送设置 · 收集 ${Math.round((cfg.collect_probability ?? .1)*100)}% · 发送 ${Math.round((cfg.send_probability ?? .1)*100)}% · 上限 ${e(cfg.max_items ?? 1000)} 张</summary>${controls}</details>`;
    const quota=page.quota || {};
    const usage=`<section class="panel settings-section"><h3>今日表情模型调用 ${e(quota.total ?? 0)} / ${e(quota.limit ?? cfg.daily_model_calls ?? 60)}</h3><p>识图 ${e(quota.vision ?? 0)} 次 · 发送前选图 ${e(quota.selection ?? 0)} 次 · 已记录 ${e(quota.tokens ?? 0)} Token</p><p class="help-caption">${e(quota.timezone || state.settings.time?.timezone || 'Asia/Shanghai')} 自然日；两类调用共享上限。模型用量可能包含估算，失败尝试不代表免费；服务商账单为准。</p></section>`;
    return usage+capacityPanel(page.capacity || {},cfg)+settings+filter+`<p>共 ${e(page.total)} 张 · 第 ${Math.floor(page.offset/50)+1} 页</p><div class="record-grid">${cards || (filters.q || filters.category || filters.locked ? '<p>没有符合筛选的表情包，请更换条件。</p>' : '<p>还没有表情包。连接群聊并选好视觉模型后，收到的图片会按概率尝试收集。</p>')}</div><div class="record-actions">${button('上一页','sticker-page','ghost small','',`data-offset="${Math.max(0,page.offset-50)}" ${page.offset===0?'disabled':''}`)}${button('下一页','sticker-page','ghost small','',`data-offset="${page.offset+50}" ${page.has_more?'':'disabled'}`)}</div>`;
  }
  ui.handleAction=async(action,target)=>{
    if(action==='sticker-page'){state.stickerOffset=Number(target.dataset.offset)||0;await renderPage();return true;}
    if(action==='sticker-prune'){
      const preview=await api('/api/stickers/prune',{method:'POST',body:{dry_run:true}});
      if(!preview.removed){toast(preview.blocked?'超出的部分全部已锁定，没有可清理的表情。':'表情包库没有超出上限。');await renderPage();return true;}
      await confirmAction(`清理 ${preview.removed} 张表情包？`,`将按顺序清理最不合适的未锁定表情${preview.blocked?'；其余超出的表情都已锁定，会保留':''}。清理后同一张图片不会再被收集。`,async()=>{const result=await api('/api/stickers/prune',{method:'POST',body:{}});toast(`已清理 ${result.removed} 张表情包`);await renderPage();},'清理');
      return true;
    }
    if(action==='sticker-edit'||action==='sticker-delete'||action==='sticker-lock'){
      const item=(state.stickerItems || []).find(x=>x.id===target.dataset.id);
      if(!item)throw new Error('请刷新表情包库后重试。');
      if(action==='sticker-lock'){await api('/api/stickers/'+encodeURIComponent(item.id),{method:'PUT',body:{locked:!item.locked}});toast(item.locked?'已解除锁定，超出上限时可能被清理':'已锁定，不会被自动清理');await renderPage();}
      else if(action==='sticker-delete')await confirmAction('删除这个表情包？','删除后，在任何聊天里都不会再收集这张图片。',async()=>{await api('/api/stickers/'+encodeURIComponent(item.id),{method:'DELETE'});await renderPage();},'删除表情包');
      else openModal('编辑表情包','明确语义后才能用于自动回复；分类和语义可自由修改。',`<img class="sticker-preview" src="/api/stickers/${encodeURIComponent(item.id)}/image" alt="表情包预览">${textarea('meaning','含义',item.meaning,'描述表达的情绪、态度及使用场景。','maxlength="240"')}${field('category','分类',item.category,'text','可填写自定义分类。','required maxlength="20"')}${field('tags','标签',item.tags.join('，'),'text','逗号分隔，最多8个。')}${toggle('clear','语义明确','不明确或无意义的表情请关闭。',item.clear)}${toggle('enabled','允许自动匹配','仍受发送开关、概率和上下文判断约束；原消息被删除后不再使用。',item.enabled)}${toggle('locked','锁定','超出上限时不会被自动清理；仍可手动删除。',item.locked===true)}`,'sticker-edit',item,'保存语义');
      return true;
    }
    return oldAction(action,target);
  };
  ui.handleSubmit=async(kind,form)=>{
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
