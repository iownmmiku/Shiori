"use strict";
window.MikubotPlugins=(()=>{
  const ui=window.MikubotExperience,oldAction=ui.handleAction,oldSubmit=ui.handleSubmit;
  const example='def run(input):\n    return {"message": "你好，" + str(input.get("name", "博士"))}\n';
  let preview=null;
  const endpoint=id=>'/api/plugins/'+encodeURIComponent(id);
  const exported=id=>`<a class="btn ghost" href="${endpoint(id)}/export" download>导出 ZIP</a>`;
  async function render(){
    const data=await api('/api/plugins');
    const preparation=`<section class="panel settings-section"><h2>运行准备</h2>${state.boot?.setup_required?`<p>聊天创建插件需要先连接支持工具调用的模型。</p>${button('连接模型','quick-model','ghost','settings')}`:'<p>使用本地聊天创建时，请选择支持工具调用的对话模型。</p>'}${!data.runtime.enabled?`<p>电脑能力尚未开启，当前可以导入和编辑草稿；运行前需要开启。</p>${button('开启电脑能力','computer-settings','ghost','settings')}`:'<p>电脑能力已开启，插件任务遵循当前权限与运行环境。</p>'}</section>`;
    return heading('LOCAL PLUGINS','插件','导入插件包，或把目标告诉本地聊天，让它生成可复用的插件。')+preparation+`<section class="panel settings-section"><h2>从这里开始</h2><p>支持 ZIP、tar.gz 和 tgz，自动识别外层文件夹与插件清单。导入只校验和保存，不执行代码；首次安装、替换和修改后均为停用草稿。</p><div class="section-actions">${button('导入插件包','plugin-import','primary','upload')}${button('用聊天创建','plugin-chat','ghost','chat')}</div><details><summary>运行要求与高级编辑</summary><p>运行遵循电脑能力的授权、时限与停止设置，不自动安装依赖。自动模式下明确说“创建插件并运行”可以连续完成；仅要求创建时保存草稿。</p><p>${data.runtime.enabled?'电脑能力已开启':'运行前请先开启电脑能力'} · ${e(data.runtime.runtime==='docker'?'Docker 隔离':'本机权限')} · ${e(data.runtime.approval_mode==='automatic'?'自动执行':'逐项确认')}</p><p class="help-caption">Docker 沙箱隔离主机系统，不保护工作目录内的文件；/workspace 是可写映射，脚本可在其中增删文件。本机运行拥有当前系统账户权限。</p>${button('手动编写 Python','plugin-new','ghost','plus')}</details></section><div class="card-grid">${data.items.map(item=>`<article class="panel settings-section"><h2>${e(item.name)}</h2><p><code>${e(item.id)}</code> · ${item.enabled?'已启用':'已停用'} · 修订 ${e(item.revision || '未知')}${item.manifest?.version?' · 版本 '+e(item.manifest.version):''}</p><p>${e(item.description || item.error || '')}</p>${item.manifest?.capabilities?.length?`<p>用途声明：${e(item.manifest.capabilities.join('、'))}；声明不授予额外权限。</p>`:''}<div class="section-actions">${button('查看 / 编辑','plugin-edit','ghost','',`data-id="${e(item.id)}"`)}${item.error?'':button(item.enabled?'停用':'启用','plugin-enable','ghost','',`data-id="${e(item.id)}" data-enabled="${!item.enabled}"`)}${item.enabled?button('运行','plugin-run','primary','',`data-id="${e(item.id)}"`):''}${item.error?'':exported(item.id)}${button('回退旧修订','plugin-rollback','ghost','',`data-id="${e(item.id)}"`)}${button('删除','plugin-delete','danger','',`data-id="${e(item.id)}"`)}</div></article>`).join('') || '<p>还没有插件。上传一个规范插件包，或点击“用聊天创建”。</p>'}</div>`;
  }
  function editor(item={}){
    const manifest=item.manifest||{};
    openModal(item.id?'编辑插件（保存后停用）':'手动编写插件','',`<form data-form="plugin-editor"><div class="form-grid">${field('id','插件 ID',item.id || 'hello_plugin','text','小写字母、数字、下划线；以字母开头。',item.id?'readonly':'')}${field('name','名称',item.name || '')}${textarea('description','用途说明',item.description || '','','rows=3')}${textarea('code',manifest.entrypoint || 'Python 入口代码',item.code || example,'','rows=14')}${textarea('input_schema','参数定义（JSON，可选）',manifest.input_schema?JSON.stringify(manifest.input_schema,null,2):'','填写 object 类型的 JSON Schema，运行时会生成参数表单。','rows=5')}</div>${item.files?`<details><summary>包内文件（${Object.keys(item.files).length}）</summary><ul>${Object.keys(item.files).map(name=>`<li><code>${e(name)}</code></li>`).join('')}</ul><p>这里编辑入口和参数，其余文件继续保留。修改资源或辅助模块可导出 ZIP 后重新导入。</p></details>`:''}<p>定义 run(input) 并返回可 JSON 序列化的结果。代码不会在服务进程中加载。</p><button class="btn primary" type="submit">保存草稿</button></form>`);
  }
  function inputFields(schema){
    const properties=Object.entries(schema?.properties||{});
    if(!properties.length&&schema?.additionalProperties===false)return '<p>此插件无需填写参数。</p><input type="hidden" name="input" value="{}">';
    if(!properties.length||properties.some(([,rule])=>!rule||typeof rule!=='object'||!['string','number','integer','boolean','array','object'].includes(rule.type)))return textarea('input','输入 JSON','{}','此插件使用复合参数定义，请按插件说明填写。','rows=7');
    return properties.map(([key,rule],index)=>{
      const name='arg_'+index,title=rule.title||key,required=(schema.required||[]).includes(key),extra=required?'required':'',hint=rule.description||'';
      if(rule.type==='boolean')return `<div class="field"><label><input type="checkbox" name="${name}" ${rule.default===true?'checked':''}> ${e(title)}</label><small>${e(hint)}</small></div>`;
      if(Array.isArray(rule.enum))return `<div class="field"><label>${e(title)}${required?' *':''}</label><select name="${name}" ${extra}>${!required?'<option value="">不填写</option>':''}${rule.enum.map((value,i)=>`<option value="${i}" ${value===rule.default?'selected':''}>${e(String(value))}</option>`).join('')}</select><small>${e(hint)}</small></div>`;
      if(rule.type==='object'||rule.type==='array')return textarea(name,title+(required?' *':'')+'（JSON）',rule.default!==undefined?JSON.stringify(rule.default):'',hint,'rows=4 '+extra);
      return field(name,title+(required?' *':''),rule.default??'',rule.type==='number'||rule.type==='integer'?'number':'text',hint,`${extra}${rule.type==='number'?' step="any"':''}${rule.minimum!==undefined?' min="'+e(rule.minimum)+'"':''}${rule.maximum!==undefined?' max="'+e(rule.maximum)+'"':''}`);
    }).join('')+`<details><summary>高级：直接输入 JSON</summary>${textarea('input_override','完整输入 JSON（填写后覆盖上方表单）','','','rows=5')}</details>`;
  }
  function runEditor(item){
    const schema=item.manifest?.input_schema||{};
    openModal('运行 '+item.name,'',`<form data-form="plugin-run" data-id="${e(item.id)}" data-schema="${e(JSON.stringify(schema))}"><div class="form-grid">${inputFields(schema)}</div><button class="btn primary" type="submit">创建运行任务</button></form>`);
  }
  function showCandidates(result){
    preview=result;
    openModal('选择插件','预览有效期10分钟。安装只保存草稿，不执行代码。',`<form data-form="plugin-install">${result.candidates.map((candidate,index)=>`<section class="panel settings-section"><label><input type="radio" name="candidate_id" value="${e(candidate.candidate_id)}" ${candidate.error?'disabled':index===result.candidates.findIndex(c=>!c.error)?'checked':''}> <strong>${e(candidate.manifest?.name||candidate.root)}</strong></label><p>${e(candidate.manifest?.description||'')}</p><p>目录：${e(candidate.root)} · ${candidate.file_count}个文件 · ${Math.ceil(candidate.bytes/1024)} KB</p>${candidate.error?`<p role="alert">${e(candidate.error)}</p>`:`<p><code>${e(candidate.manifest.id)}</code> · ${e(candidate.manifest.version)}${candidate.replaces_existing?' · 已存在同名插件':''}</p>${(candidate.warnings||[]).map(w=>`<p>${e(w)}</p>`).join('')}`}</section>`).join('')}<label><input type="checkbox" name="replace"> 如有同名插件，替换并保留旧修订</label><div class="section-actions"><button class="btn primary" type="submit" ${result.candidates.every(c=>c.error)?'disabled':''}>安装为草稿</button></div></form>`);
  }
  ui.handleAction=async(action,target)=>{
    if(!action.startsWith('plugin-'))return oldAction(action,target);
    const id=target.dataset.id;
    if(action==='plugin-new')editor();
    else if(action==='plugin-import')openModal('导入插件包','',`<form data-form="plugin-import"><div class="field"><label for="plugin-package-file">选择 ZIP、tar.gz 或 tgz（最多8MB）</label><input id="plugin-package-file" type="file" name="file" accept=".zip,.tar.gz,.tgz" required></div><p>会自动查找 plugin.json，可识别外层文件夹；上传阶段不运行任何代码。不支持 AstrBot 专用插件、RAR 或7z。</p><button class="btn primary" type="submit">识别并预览</button></form>`);
    else if(action==='plugin-chat'){
      if(state.sending||state.debouncePending)throw new Error('请先完成或取消当前回复与消息收集，再创建插件。');
      closeModal();if(!state.scope.startsWith('web:'))state.scope='web:default';state.chatMode='chat';
      state.draft='帮我创建一个插件，用来：\n\n请定义清楚输入参数，提供使用说明，先保存为草稿。';
      await navigate('chat');const input=$('#chat-text');if(input){input.value=state.draft;input.focus();}toast('补充你想完成的目标后发送即可');
    }
    else if(action==='plugin-edit')editor(await api(endpoint(id)));
    else if(action==='plugin-enable'){await api(`${endpoint(id)}/enabled`,{method:'PUT',body:{enabled:target.dataset.enabled==='true'}});await renderPage();}
    else if(action==='plugin-rollback'){await confirmAction('回退插件？','恢复上一份不同内容的修订，恢复后保持停用。工作目录中已产生的文件不会回退。',async()=>{await api(endpoint(id)+'/rollback',{method:'POST'});toast('已恢复旧修订，检查后可启用');await renderPage();});}
    else if(action==='plugin-delete'){await confirmAction('删除插件？','停止引用此插件的任务并移除插件。工作目录内已产生的文件会保留。',async()=>{await api(endpoint(id),{method:'DELETE'});await renderPage();});}
    else if(action==='plugin-run')runEditor(await api(endpoint(id)));
    else return oldAction(action,target);
    return true;
  };
  ui.handleSubmit=async(kind,form)=>{
    if(kind==='plugin-import'){
      const body=new FormData(form),file=body.get('file');if(!file?.size)throw new Error('请选择插件包');
      if(file.size>8*1024*1024)throw new Error('插件压缩包最多8MB');
      showCandidates(await api('/api/plugins/import',{method:'POST',body}));return true;
    }
    if(kind==='plugin-install'){
      const values=new FormData(form),candidate_id=values.get('candidate_id');if(!preview||!candidate_id)throw new Error('请选择通过校验的插件');
      await api('/api/plugins/import/install',{method:'POST',body:{scan_id:preview.scan_id,candidate_id,replace:values.get('replace')==='on'}});
      preview=null;closeModal();toast('插件已导入为草稿，启用后可运行');await renderPage();return true;
    }
    if(kind==='plugin-editor'){
      const data=Object.fromEntries(new FormData(form));if(data.input_schema?.trim()){try{data.input_schema=JSON.parse(data.input_schema);}catch{throw new Error('参数定义需为有效 JSON');}}else delete data.input_schema;
      await api('/api/plugins',{method:'POST',body:data});closeModal();toast('草稿已保存，启用后才能运行');await renderPage();return true;
    }
    if(kind==='plugin-run'){
      const values=new FormData(form);let input;
      try{
        if(values.get('input_override')?.trim()||values.get('input')?.trim())input=JSON.parse(values.get('input_override')?.trim()||values.get('input'));
        else{
          input=Object.create(null);const schema=JSON.parse(form.dataset.schema||'{}');
          Object.entries(schema.properties||{}).forEach(([key,rule],index)=>{
            const raw=values.get('arg_'+index);if(rule.type==='boolean'){input[key]=raw==='on';return;}
            if(raw===null||raw===''){if((schema.required||[]).includes(key))throw new Error('请填写 '+(rule.title||key));return;}
            if(Array.isArray(rule.enum))input[key]=rule.enum[Number(raw)];
            else if(rule.type==='integer'||rule.type==='number'){input[key]=Number(raw);if(!Number.isFinite(input[key]))throw new Error('数字无效');}
            else if(rule.type==='array'||rule.type==='object')input[key]=JSON.parse(raw);else input[key]=raw;
          });
        }
      }catch(error){throw new Error('请检查插件参数：'+error.message);}
      if(!input||typeof input!=='object'||Array.isArray(input))throw new Error('输入需为 JSON 对象');
      const job=await api(`${endpoint(form.dataset.id)}/run`,{method:'POST',body:{input,scope_id:state.scope.startsWith('web:')?state.scope:'web:default'}});closeModal();state.activeJob=job.id;await navigate('jobs');return true;
    }
    return oldSubmit(kind,form);
  };
  return {render};
})();
