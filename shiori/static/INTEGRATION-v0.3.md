# 群屿前端 0.3 接口

保留之前所有 API；新增页面 `diagnostics`，群语言页新增独立的表达视图。

## 模型池

`PUT /api/settings {providers:[...]}` 使用完整配置列表。每条含 `id/name/base_url/model/vision/tools/temperature/input_price/output_price/enabled`；仅用户填了新密钥时带 `api_key`，清除时带 `clear_api_key:true`。保存时不回传 `api_key_set`。

`PUT /api/settings {routing:{chat,memory,gate,summary,embedding,fallback:[id...]}}`。备用顺序最多三个、去重。删除配置时，同次提交清空引用该配置的路由与备用项。

`GET /api/providers/status` 读取 `{routes:{任务:{provider_id,name,model}},providers:[{id,name,enabled,failures,circuit_open,last_error,last_attempt_at,last_success_at}]}`。UI区分“启用”与“已验证”，不会把未测试配置标记在线。

卡片测试 `POST /api/model/test {provider_id}` 使用已保存配置。编辑窗口明确提醒先保存再测试。

Embedding 路由指定时采用该配置的模型名、地址、密钥与输入单价；未指定时使用默认服务与 `memory.embedding_model/embedding_price`。

## 独立表达学习

`GET /api/expressions?scope_id=&status=` 返回 jargon 同结构、`entry_type:'expression'`；`term` 是2—30字表达短语，`meaning` 是适用情境说明。

`POST /api/expressions/analyze {scope_id}` 返回 `{candidates,message}`。手动新增使用 `POST /api/jargon` 并带 `entry_type:'expression'`。审批、停用、删除继续使用既有 jargon API；默认新表达 pending/use_allowed false。

会话新增 `expression_learn/expression_use/plan_enabled/context_compress` 四个独立布尔字段。黑话词义与表达列表分开显示。

## 上下文摘要

`PUT /api/settings {context:{enabled,trigger_messages,max_chars,keep_recent}}`。

`GET /api/context/status?scope_id=&user_id=` 读取当前摘要。兼容 `summary` 字符串或带 `content/source_ids/created_at/updated_at` 的对象；可带顶层 `source_ids/last_compacted_at`。

`POST /api/context/compact {scope_id,user_id}` 返回 `{summary,message,compressed,...}`。UI展示真实返回说明、摘要与原始消息来源，明确计入模型用量。

## 诊断

`GET /api/diagnostics` 返回 `{checks:[{id,status,title,detail}],social?...}`；状态只接受 `ok/warning/error`。概览摘要与完整自检页均读取该接口；不主动测试外部服务。
