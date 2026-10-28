"use strict";
window.ShioriUsageLedger=(()=>{
  let offset=0,records=[],local=false;
  async function open(){
    const [data,status]=await Promise.all([api('/api/usage/unconfirmed?offset='+offset+'&limit=30'),api('/api/auth/status')]);
    records=data.items || [];local=!!status.local_setup_allowed;
    const list=records.map(item=>`<article class="provider-card"><strong>${e(item.model)}</strong><p>${e(fmt(item.created_at,true))} · ${e(item.purpose)}</p><p>待核实 ${number(item.input_tokens+item.output_tokens)} Token · 预留估算 ¥${Number(item.cost||0).toFixed(4)}</p>${local?button('核实这条记录','usage-reconcile','small','',`data-id="${e(item.id)}"`):'<p>请在运行电脑本机核实。</p>'}</article>`).join('');
    openModal('待核实消费',`共${data.total}条。超时不代表服务商未计费；请先核对账单或请求记录。`,list+`<div class="section-actions">${offset?button('上一页','usage-prev','small'):''}${offset+records.length<data.total?button('下一页','usage-next','small'):''}</div><p>核实会替换这条记录的有效用量，并保留原估算与审计记录；请求尝试次数不变。</p>`);
  }
  const action=window.ShioriExperience.handleAction,submit=window.ShioriExperience.handleSubmit;
  window.ShioriExperience.handleAction=async(name,target)=>{
    if(name==='usage-open'){offset=0;await open();return true;}
    if(name==='usage-next'||name==='usage-prev'){offset=Math.max(0,offset+(name==='usage-next'?30:-30));await open();return true;}
    if(name==='usage-reconcile'){
      if(!local)throw new Error('请在运行电脑本机核实');
      const item=records.find(record=>record.id===target.dataset.id);if(!item)throw new Error('请刷新用量记录');
      const id=crypto.randomUUID();
      openModal('核实模型消费','填写服务商确认的实际数值；确认未计费时选择对应选项。',`<input type="hidden" name="id" value="${e(item.id)}"><input type="hidden" name="reconciliation_id" value="${e(id)}">${select('action','核对结果',[['actual','已核实实际用量'],['not_billed','服务商确认未计费']],'actual')}${field('input_tokens','实际输入Token',0,'number','','min="0" step="1" required')}${field('output_tokens','实际输出Token',0,'number','','min="0" step="1" required')}${field('cost','实际费用（人民币）',0,'number','','min="0" step="0.000001" required')}${textarea('note','核对依据','','例如账单日期或请求记录；不要填写密码和密钥。','required maxlength="1000"')}`,'usage-reconciliation');return true;
    }
    return action(name,target);
  };
  window.ShioriExperience.handleSubmit=async(kind,form)=>{
    if(kind!=='usage-reconciliation')return submit(kind,form);
    const data=getForm(form);
    await api('/api/usage/'+encodeURIComponent(data.id)+'/reconcile',{method:'POST',body:{reconciliation_id:data.reconciliation_id,action:data.action,input_tokens:Number(data.input_tokens),output_tokens:Number(data.output_tokens),cost:Number(data.cost),note:data.note}});
    closeModal();toast('消费已核实，原估算保留供审计。');await renderPage();return true;
  };
  return {open};
})();
