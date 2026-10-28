"""Small, explicit configuration patches; applying one never connects an account."""
import copy

PRESETS = {
    'private_always': {'name':'私聊必回复','description':'跳过 Planner，适合明确授权的私聊。',
        'patch':{'always_reply':True,'plan_enabled':False,'proactive_reply':False,'activity':'quiet','use_persona_knowledge':True}},
    'group_companion': {'name':'群聊陪伴','description':'由 Planner 判断对象与参与价值，保留名字、@和回复触发。',
        'patch':{'always_reply':False,'plan_enabled':True,'proactive_reply':True,'activity':'balanced','trigger_at':True,'trigger_reply':True,'trigger_name':True,'cooldown_seconds':20,'followup_seconds':90,'use_persona_knowledge':True}},
    'quiet_assistant': {'name':'安静助手','description':'只响应直接触发，关闭主动插话与该会话的人格知识引用。',
        'patch':{'always_reply':False,'plan_enabled':False,'proactive_reply':False,'activity':'quiet','trigger_at':True,'trigger_reply':True,'trigger_name':True,'use_persona_knowledge':False}},
}

def list_presets():
    return [{'id':key,**copy.deepcopy(value)} for key,value in PRESETS.items()]

def preset_patch(key, scope_id):
    if key not in PRESETS: raise ValueError('未知配置预设')
    if key=='private_always' and ':group:' in scope_id: raise ValueError('私聊必回复预设仅适用于私聊或本地对话')
    return copy.deepcopy(PRESETS[key]['patch'])
