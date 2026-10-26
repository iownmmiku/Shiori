# 网页搜索（0.1.6）

入口为 **机器人配置 → 网页搜索**。这是一套独立于聊天模型的搜索配置：聊天、人格蒸馏中的通用网页检索、人格兴趣探索和页面测试共用。角色目录的 Bangumi、维基百科等专用公开接口继续独立使用；读取资料正文仍通过公开网页读取器校验地址和页面内容。

## 快速使用

1. 打开网页搜索，选择服务商。
2. 公共搜索无需密钥；Tavily、DeepSeek Search、Brave Search 填写对应搜索 API 密钥。
3. 保存配置，再在下方输入短查询测试。测试使用已保存的服务商，并计入当天搜索次数。
4. 在聊天中说“帮我搜索……”或“查一下最新……”，让支持工具调用的聊天模型使用 `web_search`。会话和聊天模型也需要允许工具。

没有填写搜索密钥时可继续使用默认公共搜索；公共搜索会尝试 Bing 网页、Bing RSS 和 DuckDuckGo，可能遇到验证、限流或页面变化。API 的测试结果会展示实际返回的链接与摘要，空结果会如实显示。

## 服务商

| 选项 | 默认接口 | 密钥与结果 |
| --- | --- | --- |
| 公共搜索 | 内置公共渠道 | 无密钥；可用性受搜索站点限制 |
| Tavily | `https://api.tavily.com/search` | 使用 Tavily API key；读取真实 `results`，固定 basic，关闭自动升级深度和生成答案 |
| DeepSeek Search | `https://api.deepseek.com/v1/tools/search` | 使用 DeepSeek API key；调用独立 Search API，查询限 128 个字符，读取来源摘要和链接 |
| Brave Search | `https://api.search.brave.com/res/v1/web/search` | 使用 Brave Search 订阅密钥，读取网页搜索结果 |

DeepSeek 使用独立检索接口，不通过普通聊天提示词模拟联网。三种协议分别依据 [Tavily Search 文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)、[DeepSeek Search 文档](https://api-docs.deepseek.com/api/search/)和 [Brave Web Search 文档](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)实现。第三方模型中转站未必提供搜索接口；普通 `/chat/completions` 地址不能直接替代上述接口。

## 上限、密钥与失败

- 默认开启公共搜索，每日最多 100 次，单次最多取 5 条结果、等待 12 秒。高级设置可修改超时、条数和每日上限；每日上限 **0 表示停止调用**。
- 每日次数按系统设置的时区跨日，持久保存，重启不清零。聊天、蒸馏、兴趣探索与测试共享；发起后失败的查询也保留次数，以限制重试开销。
- 搜索服务的费用由对应服务商计收；搜索次数上限独立于聊天 Token 预算。搜索摘要进入模型上下文后，模型调用仍按原有 Token 预算管理。
- 默认不回退。只有明确开启“API 失败后使用公共搜索”，才会在已选 API 失败时尝试公共渠道；不会切换到其他收费搜索 API。开启后同一查询会发送给公共搜索站点。
- 密钥按服务商分开保存，Windows 使用既有 DPAPI 保护，界面只显示是否保存。留空保持该服务商已有密钥，勾选清除才删除。切换服务商不会复用另一家的密钥。
- 数据备份排除搜索密钥；恢复会清除本机原有搜索密钥，恢复到付费搜索配置时先关闭搜索，重新填写并启用后使用。

自定义接口必须为公开 HTTPS 地址，不含账号、密码、查询参数或片段。访问使用 DNS 地址校验和固定连接目标，拒绝私网、重定向与超大响应。搜索结果是外部资料，不能授权电脑任务，也不能覆盖用户和系统指令；来源网址还会在读取正文时重新校验。

## 本地 API

以下入口沿用已有 WebUI 登录、同源和远程 HTTPS 规则：

- `GET /api/search/status`：返回已选服务商、开关和当天次数。
- `POST /api/search/test`，`{"query":"搜索内容"}`：用已保存配置发起真实检索。
- `POST /api/tools/search`：原入口保留，行为与测试入口使用同一服务。
- `PUT /api/settings`：`search` 保存全局选项，`search_providers` 保存按 ID 区分的连接与密钥；公开响应排除密钥。

本轮本地测试使用协议替身，不调用收费接口，也不据此宣称各服务商在用户网络上均已实测可用。
