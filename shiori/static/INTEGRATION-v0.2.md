# 群屿前端 0.2 接口补充

保留 CONTRACT.md 中所有旧 API。前端只读取真实结果，不种入演示数据。

## 记忆审核

- `GET /api/memory/candidates?scope_id=...`：返回待确认记忆数组；支持既有 `content/source_ids/confidence/review_reason`。
- `GET /api/memories/{id}/conflicts`：返回可能冲突的记忆数组。
- `POST /api/memories/{id}/approve`：JSON `{replace_conflicts: boolean}`。确认后状态为 `active`。
- `POST /api/memories/{id}/reject`：不传 JSON；状态为 `rejected`。
- 常规 `/api/memories` status 筛选新增 `pending/rejected`。
- 来源读取既有 `/api/messages?scope_id=...&limit=200`，不找到时明确告知来源不可用，绝不虚构原文。

## 知识与检索

- `GET /api/memory/search?scope_id=...&user_id=...&q=...`：返回 `{items:[memory],mode,elapsed_ms?,message?}`。
- 每条 `memory.retrieval` 可带 `{score,reasons:[string],excerpt,chunk_index}`。分数不解读为概率。
- 实际片段编号读取顶层 `memory.chunk_index` 或 `retrieval.citation.chunk_index`；展示 `semantic.error/reason/cache_hits/embedded_chunks` 及算法信息。
- `GET /api/profile?scope_id=...&user_id=...`：返回 `{facts:[memory],preferences:[memory],recent_events:[memory]}`。
- `POST /api/memory/analyze`：JSON `{scope_id,user_id}`，返回 `{candidates:[memory]}`；按钮明确标注需要文本模型并计入用量。结果展示当前候选总数，不称为新创建数量。
- 复用 `POST /api/files`，multipart `file/scope_id`，接受 TXT/MD/CSV/JSON/PDF/DOCX，客户端限制 8 MB。UI告知文本上限 30 万字、PDF/DOCX依赖解析器、不承诺OCR。

## 群文化

- `POST /api/jargon/analyze`：JSON `{scope_id}`，返回 `{candidates:[],message}`。分析后打开待确认列表。
- 词条实际返回 `evidence:[{message_id,user_id,quote}]`，前端展示证据摘要与原始来源；兼容早期对象形式 `evidence:{message_ids:[],observations:number|array,confidence?:number}`，缺失时不伪造。
- 复用 `PUT /api/scopes/{id}`，分别保存 `dictionary_learn/dictionary_understand/dictionary_use`。

## 设置

- 复用 `PUT /api/settings`，JSON `{memory:{semantic_enabled:false,embedding_model:'',embedding_price:0,top_k:8,auto_review:true}}`。
- `auto_review=true` UI文案为「自动记忆先审后用」。Embedding使用相同服务地址和密钥、独立模型名；开启但缺少模型名时前端报错。
- 会话设置新增 `semantic_gate/cooldown_seconds/merge_window_ms/followup_seconds/bot_aliases/daily_tokens`，数值作为JSON数字、别名作为字符串数组传递。
- 沿用此前 system/shutdown 与三种 QQ adapter 接口；不自动修改系统启动设置。
