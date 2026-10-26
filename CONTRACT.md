# mikubot 0.1.6 集成约定

本文描述0.1.6源码集成约定，版本发布与下载以GitHub实际公开资产为准；旧版不保证具备本版新增接口。

本文供当前源码开发与接入使用，运行时校验以 `mikubot/api.py`、`experience_api.py` 及对应服务为准。历史内部版本说明归档于 `docs/archive/`，不覆盖当前约定。产品名与核心包均为 `mikubot`；`qunyu` 仅为旧导入和启动兼容层，计划在 **0.3.0** 移除，新集成应直接使用 `mikubot`。

## 运行与认证

0.1.6配置与消息处理见[使用说明](docs/CONFIGURATION_WORKFLOWS.md)：`settings.debounce`含`enabled/seconds/max_wait_seconds`，默认`false/3/30`；机器人配置与会话可用`debounce_enabled/debounce_seconds/debounce_max_wait_seconds`覆盖，空值继承。`GET /api/time`经过管理台认证，返回程序UTC时钟、配置时区和感知开关。`POST /api/chat`可接收1—8项`messages`原始输入，各项仅包含`text/images/attachments/sent_at`；合并来源、接收时刻、身份与授权由服务端生成，不接受客户端冒充。JSON与SSE共用同一校验和附件租约。客户端收集不等于跨客户端服务端等待队列。插件规范包导入、导出与回退接口见[插件说明](docs/PLUGINS.md)。

Python 3.11–3.13（推荐3.12），FastAPI、SQLite；正式入口为 `python -m mikubot`，应用工厂为 `mikubot.api:create_app`。前端为 `mikubot/static` 中的 HTML/CSS/JS，无需用户构建。Windows 默认数据在 `%LOCALAPPDATA%\mikubot\data`，可用 `--data-dir` 指定位置；新默认目录全空时可复制旧版同级 `data`，不删除旧目录，不覆盖已有新数据。

**每个数据目录只允许一个写入进程。**正式启动器在加载设置前获取同一数据目录的系统进程锁：Windows命名互斥体，POSIX文件锁，桌面、浏览器与`--no-browser`均经过此入口。第二次启动复用已有实例或明确拒绝。应用工厂用于嵌入和测试，不应直接开启多个ASGI worker。同时以原子独占文件创建 `.instance-owner.json`，包含宿主、PID命名空间和进程身份；本地已死亡进程可回收，外来命名空间或损坏记录保守拒绝，只有确认所有写入者退出后才能手动移除。禁止Docker、本地程序或多台主机同时写同一目录，不承诺网络文件系统或多节点写入。安装现有数据目录时获取同一锁。

管理服务默认监听 `127.0.0.1`，显式远程模式必须先配置密码。Windows 默认 WebView2 原生窗口；`--browser` 使用系统浏览器，`--no-browser` 后台运行。桌面与浏览器共用认证与管理 API；反向 WebSocket 另行验证 OneBot 密钥。JSON 错误通过 `detail` 返回。不会根据请求正文中自称管理员的字段授予权限。

启动器通过本机 URL 的 `#bootstrap=` 片段提供初始化证明，前端消费并清理；旧 `#token=` 仅作为相同证明兼容，不直接登录。`POST /api/auth/bootstrap {token}` 仅真正回环连接可换取短期 HttpOnly 初始化 Cookie；已有密码不能靠启动令牌绕过。内部 Bearer 凭据只用于本机控制，不属于远程 WebUI 登录。

本机脚本使用数据目录的 `admin.token` 作为 `Authorization: Bearer <令牌>`，不支持 `X-Token`；真实TCP对端和Host都须为回环。远程管理需密码登录后的Cookie。部署与调用示例见[源码部署指南](docs/SOURCE_DEPLOY.md)。正式启动器设置实际 `listen_host/listen_port/requested_port`；bootstrap的 `listener` 以及auth状态/访问设置返回 `actual_host/actual_port/requested_port/port_changed/port_warning`，不从代理公开端口推断后端监听。发生顺延写WARNING并显示全局提示，配置端口保持原值；`--strict-port` 禁止顺延，Docker默认命令启用。直接嵌入ASGI应用的宿主需提供这些实际监听状态，或自行负责端口管理。

- `GET /api/auth/status`：公开登录状态、设密状态；实际数据和密码文件路径只在经过初始化证明或已认证的本机请求返回。
- `POST /api/auth/setup {mode,password}`：`password` 或 `local_only`，首次或删文件后只能由本机初始化证明创建；`local_only` 同步关闭远程入口。
- `POST /api/auth/login {password}`：验证后设置 HttpOnly、SameSite 会话 Cookie；HTTPS 请求附 Secure。没有默认密码。
- `POST /api/auth/logout`：注销当前会话；`POST /api/auth/password {current_password,new_password}`：改密并使旧会话失效，仅本机免密首次设密使用本机授权流程。
- `GET/PUT /api/auth/access`：已认证读取或保存 `remote_enabled/host/port`；保存与实际监听分别返回，网络重绑定需要重启。关闭远程访问立即停止接受远程管理请求。

专用 `security/webui-password.json` 保存加盐 scrypt 校验值，Windows 外层 DPAPI 加密，非 Windows 使用私有权限；不保存明文，不纳入应用备份。完全退出后删除此文件并从本机重新打开可设新密码；旧会话不能复用，数据库与业务数据保留。损坏文件保持拒绝访问，不静默变为免密。

## 身份与电脑授权

`access.administrators` 存储 `platform`、`account_id`、`instance_id` 和备注。平台为 `onebot` 或 `qqofficial`；认证的本地 `mikubot` 管理员内置。填写实例 ID 时只匹配该机器人实例，留空的手填历史条目可匹配该平台同账号；短时码绑定保存实际实例。账号名、群身份和引用内容不构成凭据。

`AccessManager` 在本机或平台认证入口签发内部身份凭据，任务每一步、审批及继续时复核。私聊/群允许列表只控制聊天资格。普通用户可以使用允许的记忆、计算、待办等聊天工具，不获得本机电脑能力或配置管理权限。

## 新工作流接口

以下入口都需要本地管理台认证；QQ 任务另由连接器认证入口转交，禁止由 HTTP 请求冒充 QQ 身份。

- `/api/admins`：查看、添加管理员；`DELETE /api/admins/{id}` 移除，内置本地账户保留。
- `/api/admins/pairing`：创建短时码；快速配对模式在60秒内由真实目标私聊入口完成绑定；保留手动候选核对及`POST .../confirm`，`DELETE`取消。绑定平台、实例与真实发送者，不因允许聊天或转发代码提权。
- `POST /api/qq/local/discover`、`connect`、`disconnect`：本机探测、鉴权配置和撤销。仅回环 WebUI 地址；不存储管理口令，配置写入前复核版本，不清空其他连接。保存和实际上线分别返回。
- `/api/jobs`、`/api/jobs/{id}`：任务列表与详情；`POST .../cancel` 停止；`POST .../approve` 要求当前单次 `approval_id`；`GET .../stream` 提供 SSE 进度。
- `POST /api/computer/jobs`：只接受本地会话，创建电脑任务；`GET /api/jobs/{id}/files/{index}` 下载成功任务的文件，检查目录与哈希。
- `POST /api/persona/distill/character/search`：搜索并保存待确认角色来源；`POST .../corpus` 上传解析材料；`POST .../start` 必须带已确认标记，创建后台蒸馏。
- `POST /api/persona/distill/{job_id}/trial`：无操作工具的草稿试聊；`POST .../apply` 另存人格；`GET .../artifact` 只允许角色模式下载 Skill。
- `POST /api/chat/attachments`：multipart `file`、`scope_id`、`purpose`，用途为 `context`、`memory` 或 `distillation`；不默认为长期记忆。普通参考附件最多 8MB。
- `POST /api/chat` 默认返回完整JSON；本地网页会话可传 `stream:true` 接收SSE。`data` 内事件类型为 `start`、`delta`、`reset`、`result`、`error`，注释心跳不含正文。`delta` 仅供临时预览，`result.data` 才是已校验的最终结果；`reset` 清除草稿。断连取消请求并保留未知消费，客户端不能因缺少最终事件自动重发工具操作。代理应禁用缓冲；QQ会话在管理界面仍为只读。
- `POST /api/models`：使用当前表单配置读取模型列表；普通 `GET` 读取已保存配置。模型测试与列表读取按实际服务验证，不凭模型名推断工具或图片能力。
- `GET/POST /api/model-providers`、`PUT/DELETE .../{id}`：供应商连接目录；`POST .../discover` 或 `.../{id}/discover` 使用当前表单读模型目录，不保存未提交密钥。`legacy` 来源只读，原模型 ID 和密钥保留。
- `POST /api/model-catalog/models {provider_id,models}`：多选批量添加；`PATCH/DELETE .../models/{id}` 修改或删除模型；`PUT .../routing` 按用途选择模型。会话 `model_id:''` 继承全局聊天模型；在用模型删除需先解除路由和会话引用。
- `GET /api/logs?limit=200&level=...&q=...&after=...`：已认证增量日志，返回 `lines/cursor/reset`；`GET .../export` 下载脱敏文本。页面只在日志页轮询，不记录模型密钥或密码明文。

## 扩展本地接口（0.1.3）

- `/api/plugins`：已认证本地管理员查看清单与保存草稿；`/{id}`读取或删除；`PUT /{id}/enabled`显式启停；`POST /{id}/run`仅运行已启用版本。聊天仅在认证的本地管理员、web会话、电脑能力与会话工具均允许时提供生成/运行工具；QQ不能经此入口创建代码。
- `/api/statistics?days=1|3|7`：有界SQL聚合与真实进程采样；未采集指标为null，不伪造首字延迟或价格。
- `/api/updates/switch {release_id}`：直接替换程序并重启。需启动器可退出、无活动任务/模型请求、固定仓库资产校验通过；远程需由宿主设置 updates.allow_remote。独立助手备份数据及旧程序，健康检查失败后安全停止并回退。源码本地改动与未经声明的降级拒绝覆盖，容器通过镜像更新。
- `/api/updates/apply {release_id}`：旧安装器更新兼容接口，保留给旧版安装流程；新界面优先使用 switch。
- `time.timezone`：IANA时区，默认Asia/Shanghai；日预算、Planner、表情配额与统计用同一自然日。


## 扩展本地接口（0.1.4）

- `GET /api/chat/attachments?scope_id=...&offset=0&limit=50`：按会话分页列出资料；`GET/DELETE /api/chat/attachments/{id}`预览或主动撤销。`POST .../release`释放临时引用，`POST .../cleanup`触发安全回收；正在使用的引用在请求/任务结束前不会被自动删除。主动删除属于撤权，后续模型派发、最终结果仍需复核。
- `GET /api/usage/unconfirmed`：分页查看待核实消费；`POST /api/usage/{id}/reconcile`由真正本机的已认证管理员填写实际用量/费用或未计费结论及依据。幂等更新有效账本，保留原估算和审计，尝试次数不清除；不提供供应商自动逐笔核销。
- `POST /api/chat {stream:true,...}`的增量事件仍遵循上方SSE约定；请求取消必须释放附件租约与模型名额，已派发但无法确认的费用保留为待核实。只在最终`result`保存完整回复，禁止把工具参数或隐藏思考作为预览正文。
- `/api/statistics`额外返回可读取的cgroup v1/v2资源边界、采样CPU使用率与后台向量索引状态；无法可靠关联的外部QQ连接器资源明确标记未采集。
- 记忆词项/向量索引是可重建派生数据。候选必须经过当前作用域、来源版本和依赖权限校验；后台队列有界、去重、低优先级，关闭时取消并等待，不跨重启重放。查询向量与可选同步构建共用`memory.semantic_timeout_seconds`，不代表整轮聊天总时限。
- `output.reply_max_chars`整轮字符上限为0—20000整数，`output.message_max_chars`单条为0—4000整数，均默认0不限制；`output.remove_parentheses`默认false。它们独立于分段开关，只处理AI正文，受保护代码/链接/JSON不截残，系统说明和工具确认凭据保留。调整后消息保存`metadata.original_reply`与`metadata.output_adjustment`并提供折叠原文；详情见[输出约定](docs/MODEL_OUTPUT.md)。

## 网页搜索与兴趣探索（0.1.6）

搜索接口：`GET /api/search/status`、`POST /api/search/test`（`query`），原 `POST /api/tools/search` 保留。三者使用与聊天、人格蒸馏通用搜索及兴趣探索共享的 `SearchService`；设置为 `search` 和按服务商 ID 区分的 `search_providers`。每次检索按当前配置检查开关和持久日配额；测试同样计数。搜索密钥不进入公开设置、日志和备份。详见[搜索约定与使用说明](docs/WEB_SEARCH.md)。

`GET /api/knowledge/status` 的人格计划提供 `disabled_reason`；可选 `last_result` 记录最近一次完成时间、原兴趣、实际查询、各结果数量和最多12条过滤明细。诊断地址移除查询参数与片段，详情总计最多128KiB并受人格知识整文件5MiB上限约束；容量紧张时先清理详情，保留完成消息。旧备份可以没有这些字段。并列主题通过可选 `focus_cursors`、`last_focus` 保留轮换进度，知识条目仍关联原兴趣；跳过排除主题不消耗探索次数。全局与人格自动探索开关不因迁移改变，手动探索也不绕过来源、暂停与额度检查。详见[人格兴趣探索](docs/INTEREST_EXPLORATION.md)。

## 任务执行边界

常规聊天默认 `output.mode: model`，生成提示词要求独立消息用空行分隔，单换行属于条内排版；完整代码与JSON保护。`reply_parts`记录在助手消息元数据并在聊天API返回，原正文及来源仍按一条逻辑回复保存。发送端只读这些生成边界，保留条数/传输上限和每条权限复核。旧`target_chars/max_chars`仅在显式`mode: length`时用于兼容；关闭输出分段仍保留传输上限。

任务保存在本地 `jobs.json`。状态为 `queued/running/awaiting_approval/completed/failed/cancelled/interrupted`；待批准动作绑定任务和单次审批 ID；QQ 可在原会话回复“同意/确认/拒绝”，仅匹配同一真实发起者、唯一等待动作，5分钟失效；有歧义使用 `/确认 <任务ID> <审批短码>`，旧短码不能批准下一步。关网页不停止任务，关闭程序则中断。重启、恢复历史和重复审批不得重放电脑或插件操作；人格蒸馏可手动从已完成阶段检查点续作，材料/模型变化则拒绝复用，不确定消费需人工核对。

电脑文件工具只能访问所选工作目录；路径穿越、链接和目录联接被拒绝。电脑能力开启且 `computer.approval_mode:'automatic'` 时，可信管理员明确发起的本机任务自动执行，文件写入、Shell、Python 不再逐步审批，保留每步权限检查、停止与结果记录。同一条件下，管理员在允许会话中的普通聊天 MCP 调用也会直接执行，仍校验服务与工具允许列表；电脑能力关闭或模式为 `per_action` 时逐次审批。新安装默认Docker运行时，固定官方镜像摘要并核对本地RepoDigests，缺环境停止且不降级。本机模式的Shell/Python以当前账户权限执行，工作目录不能隔离其外部影响。Docker 沙箱隔离主机系统，不保护工作目录内的文件；/workspace 是可写映射，脚本可在其中增删文件。内置文件工具和MCP不在此代码容器内；Docker模式默认禁用沙箱外MCP，需明确开启。

人格/网页/附件是分析资料，不作为新的工具授权。角色研究只使用确认的来源快照，变更则重新确认。语料按目标说话者提炼，资料没有提供的信息保持未知；语料模式仅输出人设。

## 记忆、来源与备份

同会话与用户 ID 构成身份边界，不按昵称合并。共享空间只共享已确认、明确可共享的记忆。任何派生回复、关系或工具上下文必须检查来源、版本、撤权和暂停状态。

自动提取先验证完整候选批次；畸形非空结果不推进游标，合法空数组及低置信度结果可完成。线程写入在同一事务内复核来源与权限，失败整批回滚；保存后用户编辑不被自动采用覆盖。原有记忆以ID/版本/active状态检测在途纠正，未被发送的过期事实或关闭的摘要不作为新提取的可见性依赖。召回偏好与关系使用当前来源权限，关系按邻接节点分批读取，保留两跳和最终结果限制。

聊天参考按完整记录装入预算，超出时通过 `omitted_references` 标明各类省略数量；回复计划和已完成命令回执独立保留为必要数据，不能随可选参考悄悄丢弃。必要数据超出整轮输入上限时停止模型派发。时间检索与近期历史在预算处理后按消息ID去重，不合并同文不同来源；摘要失效时按数据库时间及插入顺序恢复规范原消息。`/api/context/status`复用聊天实际的有效摘要选择，群摘要不限制为当前成员生成，但继续遵守同群与来源规则。详见[复查记录](docs/MEMORY_CONTEXT_AUDIT_2026-10-26.md)。

`context.recent_turns`默认24，范围1—100；近期窗口按用户轮次对齐，仍受发送历史消息数与字符上限限制。`max_history_messages`只限制对话模型的原文，不限制自动摘要的独立有界候选批次。时间召回保持规范消息与指纹不变，展示改写在校验后进行，并作为历史参考而非新用户轮次。`context_compressed=true`仅用于实际发送有效摘要的轮次；摘要无法容纳时恢复原文并报告false。提取游标采用created_at与插入顺序，首次无一天截断，分页前排除遗忘来源。详见[整改与验收边界](docs/HISTORY_PIPELINE_AUDIT.md)。

完整应用备份含数据库、脱敏设置、人格知识、只读工具与后台任务历史。恢复后暂停、断开通道、清除密钥，历史审批无效、操作不重放。原始语料、附件、Skill ZIP、电脑工作目录及任意用户文件不应被视为已随应用备份打包，需另行保留。插件源码 `plugins/` 与工作目录产物、表情包 `stickers/library.sqlite3` 也需独立备份；表情包页面提供独立库导出。

测试不连接用户真实 QQ，也不默认请求收费模型。真实模型回放必须显式开启，合成路由案例只验证回归路径，不证明智能质量。正式交付验证记录在 `TEST_REPORT.md`。
