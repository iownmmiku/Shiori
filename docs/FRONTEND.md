# 当前前端结构

前端仍为原生 HTML/CSS/JS，无 Node 运行依赖或用户构建步骤。`shiori/static/index.html` 用 `defer` 按明确顺序加载脚本；新增模块应同时加入该清单，不能依赖动态执行字符串。

| 模块 | 责任 |
| --- | --- |
| `app.js` | 启动、共享状态、导航、通用事件分发与 API |
| `ui-core.js` | 转义后的表单、弹窗、提示、公共界面组件 |
| `chat-page.js`、`memory-page.js`、`culture-page.js`、`tasks-page.js` | 基础聊天、记忆、群语言、任务页面 |
| `personas-page.js`、`settings-page.js`、`diagnostics-page.js` | 人格基础视图、设置与诊断 |
| `knowledge-page.js`、`tools-page.js` | 知识库、扩展工具基础视图 |
| `experience-chat.js`、`experience-jobs.js` | 会话侧栏、附件、流式消息与后台任务展示 |
| `experience-config.js`、`experience-persona.js`、`experience-help.js` | 系统配置、人格生成/蒸馏、概览与指南 |
| `experience.js` | 上述工厂共享的请求状态、SSE 生命周期和交互协调 |
| `workspace-pages.js`、`bot-config.js` | 供应商/模型目录、日志、集中的机器人配置 |
| `stickers.js`、`statistics.js`、`plugins.js`、`updates.js` | 独立功能控制器；经既有分发器衔接 |

页面工厂显式接收状态访问器与依赖，基础页面函数保持原有调用约定。控制器签名为 `handleAction(action,target)`、`handleSubmit(kind,form)`，未处理的事件必须转交既有处理器。避免在新控制器覆盖掉此前功能。

动态文本先转义；密钥和聊天正文不拼到 HTML 事件属性，不增加内联脚本。统计轮询仅在认证且处于统计页时继续；退出、切页后停止。

变更后运行前端回归（由 `tests/test_experience_ui.py` 统一调用 Node）、所有 JS 语法检查，以及真实浏览器验证。`tests/frontend_plugins_statistics.cjs` 同时检查外部 script 标签必须正确闭合，防止纯函数测试遗漏 HTML 加载问题。
