# 群屿前端 0.4 接口

新增 `tools`（扩展工具）导航页，使用原有本地管理会话，不引入新依赖。

## MCP 服务

`GET /api/settings` 的 `mcp_servers` 是完整列表，每项包含 `id/name/url/enabled/allowed_tools/token_set`。私有令牌不回显。

`PUT /api/settings {mcp_servers:[...]}` 提交完整列表。保存前重新读取设置，尽量保留其他服务的最新值。仅填写新令牌时携带 `token`，显式清除时携带 `clear_token:true`。删除时从列表移除；最多 8 个服务。修改地址会在前端清空该服务允许列表，要求重新发现。

`GET /api/mcp/status` 返回 `{servers:[{id,name,enabled,configured,tools_count,last_error,last_checked_at}]}`。加载页面只读取本地状态，不主动连接外部服务。

`POST /api/mcp/discover {server_id}` 是明确标注“发现工具（联网）”的按钮操作，返回 `{tools:[{name,description,inputSchema,schema_hash}],message}`。UI 展示工具说明和参数格式，勾选名称并保存 `allowed_tools:string[]`，最多 50 个。保存允许列表只允许提出请求，不能跳过逐次审批。服务 schema/hash 的真实性及变更检查由后端执行。

支持 Streamable HTTP 的 JSON 响应；HTTPS 或本机 HTTP。界面明确说明不支持 stdio 进程或原始 SSE 传输。

## 会话与对话

会话编辑新增 `allow_mcp:boolean`，默认 false。它同时受既有 `allow_tools` 工具总开关约束，开启后仍不会立即执行群内请求。

聊天接口返回 `pending_actions` 非空数组时，展示未执行、待审批提示和扩展工具入口。该提示不宣称任务已经完成。审批完成结果只在本地显示，不自动发往 QQ，继续使用结果需重新提问。

## 执行记录

`GET /api/tool-runs?scope_id=&status=` 返回数组（也兼容 `{items:[...]}`），字段：`id/scope_id/user_id/server_id/server_name/tool_name/arguments/status/created_at/updated_at/result/error/source_ids/expires_at`。

状态接受 `pending/running/succeeded/failed/unknown/rejected/cancelled`。待批准显示目标服务、工具名、参数、用户、来源、过期时间。确认弹窗重新读取记录，展示完整参数 JSON；列表参数预览及结果展示限长，所有外部文本均 escape。未知结果提示先在目标服务核对，避免重复操作。

`POST /api/tool-runs/{id}/approve {}` 只能通过“批准并执行一次”确认表单发出。兼容返回记录本体或 `{run:record}`。不会在 pending 阶段显示成功，执行后按返回 status 显示并重新读取实际详情。

`POST /api/tool-runs/{id}/reject {}` 拒绝前二次确认。

来源通过 `GET /api/messages?scope_id=&limit=200` 读取，仅展示最近 200 条内可读取的原文，已删除或超范围时如实显示缺失。

## 验证

`node --check shiori/static/app.js` 通过。浏览器交互及真实 HTTP/审批验证由主任务执行，不由此静态界面单独推断。
