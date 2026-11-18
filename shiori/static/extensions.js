"use strict";
// 扩展: trusted add-ons that run inside Shiori (extension_host.py). Lists them,
// turns them on and off, edits their declared settings and embeds their pages.
window.ShioriExtensions=(()=>{
  const ui=window.ShioriExperience,oldAction=ui.handleAction,oldSubmit=ui.handleSubmit;
  const endpoint=id=>'/api/extensions/'+encodeURIComponent(id);
  const items=()=>state.extensionItems || [];
  const find=id=>items().find(item=>item.id===id);

  function status(item){
    if(item.error)return `加载失败：${item.error}`;
    if(item.enabled&&item.loaded)return '已启用';
    if(!item.builtin&&!item.trusted)return '已停用 · 尚未确认可信';
    return '已停用';
  }

  function card(item){
    const facts=[item.builtin?'Shiori 自带':'数据目录',item.version?'版本 '+item.version:'',item.author?'作者 '+item.author:''].filter(Boolean).join(' · ');
    const running=item.enabled&&item.loaded;
    const pages=running?item.pages.map(page=>button('打开「'+page.title+'」','extension-open','primary small','',`data-id="${e(item.id)}" data-extension-page="${e(page.id)}"`)).join(''):'';
    const settings=Object.keys(item.config_schema||{}).length?button('设置','extension-settings','ghost small','',`data-id="${e(item.id)}"`):'';
    const toggleButton=item.error&&!item.loaded?'':button(running?'停用':'启用','extension-toggle',running?'ghost small':'primary small','',`data-id="${e(item.id)}" data-enabled="${!running}"`);
    return `<article class="panel settings-section"><h2>${e(item.name)}</h2><p class="help-caption">${e(facts)} · <code>${e(item.id)}</code></p><p>${e(item.description || '')}</p><p><strong>${e(status(item))}</strong></p>${running&&item.tools.length?`<p class="help-caption">给聊天模型的工具：${e(item.tools.join('、'))}</p>`:''}<div class="section-actions">${pages}${settings}${toggleButton}</div></article>`;
  }

  async function render(){
    const data=await api('/api/extensions');
    state.extensionItems=data.items || [];
    const open=state.extensionPage&&find(state.extensionPage.id);
    const page=open&&open.enabled&&open.loaded&&open.pages.find(item=>item.id===state.extensionPage.page);
    if(page){
      return heading('扩展',`${open.name} · ${page.title}`,open.description,button('返回扩展列表','extension-back','ghost'))
        +`<iframe class="extension-frame" src="${e(page.url)}" title="${e(open.name+' · '+page.title)}" style="display:block;width:100%;height:calc(100vh - 190px);min-height:480px;border:0;background:transparent" referrerpolicy="no-referrer"></iframe>`;
    }
    state.extensionPage=null;
    const intro=`<section class="panel settings-section"><h2>什么是扩展</h2><p>扩展可以给聊天模型增加新本领（例如生图），并带有自己的设置页。和“插件”不同，扩展直接在 Shiori 里运行，权限和 Shiori 相同，所以只启用 Shiori 自带的或你信任的扩展。</p><p class="help-caption">其他扩展放在数据目录的 extensions 文件夹里（每个扩展一个文件夹，里面有 extension.json），重启 Shiori 后出现在这里；第一次启用要确认可信，扩展的文件改动后需要重新确认。</p></section>`;
    const cards=state.extensionItems.length?`<div class="card-grid">${state.extensionItems.map(card).join('')}</div>`:'<p>还没有扩展。</p>';
    return heading('','扩展','给机器人增加新本领，并在这里管理它们的设置。')+intro+cards;
  }

  function settingsForm(item){
    const schema=item.config_schema||{},values=item.config||{},secrets=item.secrets_set||{};
    const body=Object.entries(schema).map(([key,rule])=>{
      const name='cfg_'+key,hint=rule.description||'';
      if(rule.type==='bool')return toggle(name,rule.title,hint,values[key]===true);
      if(rule.type==='int'||rule.type==='float')return field(name,rule.title,values[key]??rule.default,'number',hint,`required step="${rule.type==='int'?1:'any'}"${rule.min!==undefined?` min="${e(rule.min)}"`:''}${rule.max!==undefined?` max="${e(rule.max)}"`:''}`);
      if(rule.type==='text')return textarea(name,rule.title,values[key]??'',hint,'rows="4"');
      if(rule.type==='select')return select(name,rule.title,rule.options,values[key]??rule.default);
      if(rule.type==='secret')return field(name,rule.title,'','password',secrets[key]?'已保存；留空继续使用，页面不会回显。':hint,'autocomplete="new-password" spellcheck="false"')+(secrets[key]?toggle('clear_'+key,'删除已保存的'+rule.title,'勾选并保存后删除。',false):'');
      return field(name,rule.title,values[key]??'','text',hint);
    }).join('');
    openModal(`${item.name} · 设置`,'保存后立即生效。',`<div class="form-grid">${body}</div>`,'extension-config',item,'保存设置');
  }

  ui.handleAction=async(action,target)=>{
    if(action==='extension-back'){state.extensionPage=null;await renderPage();return true;}
    if(['extension-open','extension-settings','extension-toggle'].includes(action)){
      const item=find(target.dataset.id);
      if(!item)throw new Error('请刷新扩展列表后重试。');
      if(action==='extension-open'){state.extensionPage={id:item.id,page:target.dataset.extensionPage};await renderPage();return true;}
      if(action==='extension-settings'){settingsForm(item);return true;}
      const enabled=target.dataset.enabled==='true';
      const save=async confirm=>{await api(endpoint(item.id),{method:'PUT',body:{enabled,...(confirm?{confirm:true}:{})}});toast(enabled?`已启用「${item.name}」`:`已停用「${item.name}」`);await renderPage();};
      if(enabled&&!item.builtin&&!item.trusted)await confirmAction(`启用「${item.name}」？`,'这个扩展会在 Shiori 进程里运行，可以读写 Shiori 的数据、联网和执行代码，权限和 Shiori 相同。只启用你自己写的或来自可信来源的扩展。',()=>save(true),'确认可信并启用');
      else await save(false);
      return true;
    }
    return oldAction(action,target);
  };

  ui.handleSubmit=async(kind,form)=>{
    if(kind!=='extension-config')return oldSubmit(kind,form);
    const item=state.modalRecord,data=getForm(form),config={},clear=[];
    for(const [key,rule] of Object.entries(item.config_schema||{})){
      const value=data['cfg_'+key];
      if(rule.type==='bool')config[key]=value===true;
      else if(rule.type==='int'||rule.type==='float'){const number=Number(value);if(value===''||!Number.isFinite(number))throw new Error(`${rule.title}须为数字。`);config[key]=number;}
      else if(rule.type==='secret'){if(value)config[key]=value;if(data['clear_'+key]===true)clear.push(key);}
      else config[key]=value??'';
    }
    await api(endpoint(item.id),{method:'PUT',body:{config,clear}});
    closeModal();toast(`「${item.name}」的设置已保存`);await renderPage();return true;
  };
  return {render};
})();
