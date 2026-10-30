# 当前前端结构

前端仍为原生 HTML/CSS/JS，无 Node 运行依赖或用户构建步骤。`shiori/static/index.html` 用 `defer` 按明确顺序加载公共脚本；统计、插件、QQ 监控和角色日程由 `asset-loader.js` 的固定白名单按需加载。新增页面必须明确注册资源，不允许从 API、插件或用户输入构造脚本地址，也不动态执行字符串。

使用指南以 `docs/USER_GUIDE.md` 为唯一编辑源。修改后运行 `python scripts/sync_guide.py` 生成 `shiori/static/guide.md`，相对文档链接会转成主仓库的 HTTPS 链接；代码示例保持原样。`python scripts/sync_guide.py --check`、回归测试和 Windows 构建入口会检查同步状态。源码文档变更不会自动更新已发布的安装包，需在后续构建时打包。

| 模块 | 责任 |
| --- | --- |
| `app.js` | 启动、共享状态、导航、通用事件分发与 API |
| `ui-core.js` | 转义后的表单、弹窗、提示、公共界面组件 |
| `chat-page.js`、`memory-page.js`、`culture-page.js`、`tasks-page.js` | 基础聊天、记忆、群语言、任务页面 |
| `personas-page.js`、`settings-page.js`、`diagnostics-page.js` | 人格基础视图、设置与诊断 |
| `knowledge-page.js`、`tools-page.js` | 知识库、扩展工具基础视图 |
| `experience-chat.js`、`experience-jobs.js` | 会话侧栏、附件、流式消息与后台任务展示 |
| `chat-projects.js`、`chat-workspace.css` | 本地项目与会话组织、独立 Chat 外壳、移动端项目抽屉；在 `experience-chat.js` 前加载控制器 |
| `qq-monitor.js`、`qq-monitor.css` | 只读 QQ 列表、历史游标、交付状态和可暂停轮询，离开或隐藏页面停止刷新 |
| `experience-config.js`、`experience-persona.js`、`experience-help.js` | 系统配置、人格生成/蒸馏、概览与指南 |
| `experience.js` | 上述工厂共享的请求状态、SSE 生命周期和交互协调 |
| `workspace-pages.js`、`bot-config.js` | 供应商/模型目录、日志、集中的机器人配置 |
| `stickers.js`、`statistics.js`、`plugins.js`、`updates.js` | 独立功能控制器；经既有分发器衔接 |
| `character-schedule.js`、`character-schedule.css` | 独立角色日程工作台、角色指纹绑定草稿、生成进度、留言及日记来源管理 |
| `asset-loader.js` | 独立页面的资源加载、并发去重与失败重试；保留样式覆盖顺序 |
| `backup-ui.js` | 后台备份进度、取消与下载；关闭弹窗、切页和退出停止轮询 |

页面工厂显式接收状态访问器与依赖，基础页面函数保持原有调用约定。控制器签名为 `handleAction(action,target)`、`handleSubmit(kind,form)`，未处理的事件必须转交既有处理器。避免在新控制器覆盖掉此前功能。

`chat` 仅处理 `web:` 会话，`qqChats` 仅监视 QQ 会话。`/chat` 返回同一认证页面，导航分别记住本地与 QQ 的选中会话；离开 Chat 前 `parkChatDraft()` 停放完整草稿，回来通过 `activateChatScope()` 恢复。项目元数据不能作为完整 scope 使用，新会话必须读取继承后的完整配置。消息和任务请求写回 DOM 前核对页面、scope 和渲染 revision；QQ 轮询额外核对自身 generation，不复用过期的 history cursor。

项目写操作按资源与登录代去重，写后刷新不复用写前读取，完成后仅更新仍属于原操作的界面或弹窗。QQ 消息窗口最多 500 条，加载旧记录后进入历史模式，仍更新列表与回执；“回到最新”重新读取尾部窗口。服务端游标使用时间索引范围限制扫描，不将完整 scope 拉入 Python 排序。

登录页统一调用 `ShioriAppLifecycle.leaveAuthenticated()`：立即失效 `authGeneration` 和页面 revision，停止轮询、流读取并清理内存草稿。API 在响应头和 JSON 解析结束后校验登录代，旧 401 不得使新的登录失效；bootstrap 仅通过认证入口或仍有效的登录继续。普通页面导航不调用这个清理流程。

动态文本先转义；密钥和聊天正文不拼到 HTML 事件属性，不增加内联脚本。统计轮询仅在认证且处于统计页时继续；退出、切页后停止。

会话表单记录打开时的初始值，仅提交修改项。单字段恢复继承使用后端白名单 `inherit_fields`，不以整个 `profile_overrides` 数组覆盖其他字段。黑话证据按原消息 ID 分页读取，弹窗关闭、切换条目或切换会话后忽略旧请求。异步页面加载结束后同样校验当前页面 revision。

变更后运行前端回归（由 `tests/test_experience_ui.py` 统一调用 Node）、所有 JS 语法检查，以及真实浏览器验证。`tests/frontend_plugins_statistics.cjs` 同时检查外部 script 标签必须正确闭合，防止纯函数测试遗漏 HTML 加载问题。
