"use strict";

// Resource budgets are distinct from daily billing limits and summary triggers.
window.MikubotResources = (() => {
  const groups = {
    memory: [
      ["term_cache_mb","记忆词项缓存（MiB）",64,0,256,"0关闭缓存；缩小后重新分词会更多。"],
      ["read_concurrency","记忆检索并发",2,1,8,"调整后在检索空闲时生效。"],
      ["read_queue_limit","记忆检索在途上限",32,1,200,"包含正在运行和等待的检索。"],
      ["semantic_timeout_seconds","在线语义检索总等待（秒）",8,1,120,"每条回复共用，包含查询向量与排队；超时改用词法检索。"]
    ],
    scheduler: [
      ["global_model_concurrency","全部模型合计并发",2,1,16,"不同模型共用此上限。"],
      ["global_model_queue_limit","全部模型合计等待上限",32,1,200,"队列满时明确提示稍后再试。"],
      ["model_concurrency","每个模型并发",1,1,8,"按模型ID分别限制。"],
      ["model_queue_limit","每个模型等待上限",32,1,200,"队列不是额外的运行并发。"],
      ["background_queue_limit","后台学习等待上限",16,1,100,"前台聊天优先。"],
      ["scope_queue_limit","每会话排队上限",8,1,32,"减少群消息突发时的堆积。"],
      ["request_concurrency","管理请求并发",8,1,64,"控制一般API请求。"],
      ["request_queue_limit","管理请求等待上限",16,1,200,"不改变登录鉴权。"],
      ["request_max_mb","管理请求正文上限（MiB）",32,1,64,"较低时可能拒绝大语料ZIP；登录仍有独立8KiB上限。"],
      ["parsing_concurrency","文件解析并发",1,1,8,"限制语料、文档等解析峰值。"],
      ["parsing_queue_limit","文件解析等待上限",4,1,100,"排队也会保留请求，请勿设得过大。"]
    ],
    context: [
      ["max_input_chars","聊天输入文字硬上限",80000,2000,200000,"先省略整条旧上下文；人设与当前请求超出则拒绝。图片另计。"],
      ["recent_turns","近期对话轮数",24,1,100,"普通模式按用户轮次选取并对齐；继续上次话题时可补充有来源的早前片段，不增加摘要调用。"],
      ["max_history_messages","发送的历史消息上限",400,1,400,"限制送给对话模型的原文；自动摘要使用独立的有界候选窗口，不再被这个上限阻止触发。"],
      ["max_images","每条请求图片上限",3,0,3,"0停止接收图片，不会静默忽略图片。"]
    ]
  };
  const preset = {
    memory:{term_cache_mb:16,read_concurrency:1,read_queue_limit:8,semantic_timeout_seconds:8},
    scheduler:{global_model_concurrency:1,global_model_queue_limit:8,model_concurrency:1,model_queue_limit:8,background_queue_limit:4,scope_queue_limit:2,request_concurrency:4,request_queue_limit:8,request_max_mb:12,parsing_concurrency:1,parsing_queue_limit:2},
    context:{max_input_chars:16000,max_history_messages:16,max_images:1}
  };
  function render() {
    const labels={memory:"记忆检索",scheduler:"并发、队列与上传",context:"聊天上下文硬限制"};
    return `<section class="panel settings-section" id="resource-settings-section"><h2>资源与响应速度</h2><p class="help-caption">适合小内存服务器和高活跃群。这里控制占用与等待，不代替每日费用预算，也不是总内存保证。</p><form data-form="resource-settings">${Object.entries(groups).map(([section,fields])=>`<h3>${labels[section]}</h3><div class="form-grid">${fields.map(([key,label,value,min,max,help])=>field(`${section}__${key}`,label,state.settings?.[section]?.[key] ?? value,"number",help,`min="${min}" max="${max}" step="1"`)).join("")}</div>`).join("")}<label class="field"><span>缺失记忆向量的构建方式</span><select name="memory__background_indexing"><option value="true" ${state.settings?.memory?.background_indexing!==false?"selected":""}>后台构建（推荐）</option><option value="false" ${state.settings?.memory?.background_indexing===false?"selected":""}>本轮等待构建</option></select><small>后台仍会调用嵌入模型并计费；冷记忆先用词法召回，构建完成后可参与语义召回。索引队列和失败次数可在统计页查看。</small></label><p class="help-caption" id="resource-preset-note">“低内存起点”只填写此表，点击保存后才生效；不会关闭功能或更改供应商密钥。</p><div class="section-actions"><button class="btn ghost" type="button" data-resource-preset>填写低内存起点</button><button class="btn primary" type="submit">保存资源设置</button></div></form></section>`;
  }
  async function handleSubmit(form) {
    const data=getForm(form), payload={};
    for(const [section,fields] of Object.entries(groups)) payload[section]=Object.fromEntries(fields.map(([key])=>[key,Number(data[`${section}__${key}`])]));
    const indexing=form.querySelector('[name="memory__background_indexing"]');
    if(indexing)payload.memory.background_indexing=indexing.value!=='false';
    await api('/api/settings',{method:'PUT',body:payload});
    toast('资源设置已保存；检索并发在下次空闲时应用');
    await bootstrap();
  }
  document.addEventListener('click',event=>{
    const button=event.target.closest?.('[data-resource-preset]');if(!button)return;
    const form=button.closest('form');if(!form)return;
    for(const [section,values] of Object.entries(preset))for(const [key,value] of Object.entries(values)){
      const input=form.querySelector(`[name="${section}__${key}"]`);if(input)input.value=String(value);
    }
    const note=form.querySelector('#resource-preset-note');if(note)note.textContent='低内存起点已填入，尚未保存。确认后点击“保存资源设置”。';
  });
  return {render,handleSubmit};
})();
