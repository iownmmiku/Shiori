# PR #8：高级模型生成参数审查

## 本地整合与补修（2026-10-24）

GitHub已由用户合并为`24975a57e2267eda3704c86ee36a94c5473068f9`。本地在保留未提交0.1.6改动的前提下，三方整合了本报告审查的`02d19ed`内容，处理`provider.py`的两处冲突，保留时间刷新、统一调度和耗时记录。未在脏工作区直接执行pull或覆盖整文件；整合和后续修复仍为本地未提交修改。

复审的P2已补修：隐式summary请求也统一应用配置上限，128额度实际发送128；P3已补修：前端拒绝`1e309`与`-1e309`，不再被JSON序列化成null。作者测试增加这两类回归，当前共43项。下文为各阶段历史审查结果，不代表尚未修复。发布仍暂停。

## 本轮复审：`02d19ed`（2026-10-24）

复审提交：`02d19ed9e8bb9e096432b3d57bc89de62af871f1`，基线仍为已发布的0.1.5（`d2ddf2c`）。以下“首轮审查”保留历史背景，不代表旧的5项问题仍未修复。

**结论：原5项问题已修复，新增回归通过；仍有1项P2及1项P3需要处理。建议先修正P2，再整合到本地0.1.6。当前未合并、推送或发布。**

### 原问题复验

| 首轮问题 | 本轮结果 |
| --- | --- |
| `n`、`max_completion_tokens`等绕过输出预算 | 保存时拒绝保留字段，请求构造再次剥离，已修复 |
| 聊天设置不生效、旧蒸馏额度被默认8192截断 | 聊天默认使用配置额度，旧配置以0表示不增加上限；显式任务额度正确取上限，已修复。隐式任务额度另有下述遗漏 |
| 非有限数字、超大参数、预留后才序列化 | 后端限制有限数字、数量及体积，完整请求在预算预留前序列化，已修复。前端数字转换另见下述P3 |
| 内联事件被CSP拦截 | 使用共享事件委托，滑块与数字框双向同步，已修复 |
| 模型目录重复温度字段 | 设置页与目录复用高级参数组件，每个字段只提交一次，已修复 |

### 剩余问题

1. **P2：未显式传额度的非聊天任务绕过模型上限。** [provider.py:465–468](https://github.com/iownmmiku/mikubot/blob/02d19ed9e8bb9e096432b3d57bc89de62af871f1/mikubot/provider.py#L465)中，`requested is None`先赋默认值1000，后面的`elif configured`不再执行。实际[聊天总结调用](https://github.com/iownmmiku/mikubot/blob/02d19ed9e8bb9e096432b3d57bc89de62af871f1/mikubot/chat.py#L831)没有传`max_tokens`。真实Provider、Store及MockTransport复现：配置128，`purpose='summary'`仍发送1000，并预留1008（8输入+1000输出），与界面“其他任务最多使用该上限”的说明不一致。应先解析默认值，再对所有用途统一应用配置上限，并补充`summary / None / 128 → 128`回归。这不是原来的自定义字段预算绕过；本地账本仍按实际请求1000预留。
2. **P3：前端把非有限自定义数字静默改为null。** [ui-core.js:91–96](https://github.com/iownmmiku/mikubot/blob/02d19ed9e8bb9e096432b3d57bc89de62af871f1/mikubot/static/ui-core.js#L91)只检查JSON对象，`{"seed":1e309}`解析为`Infinity`，经过[API序列化](https://github.com/iownmmiku/mikubot/blob/02d19ed9e8bb9e096432b3d57bc89de62af871f1/mikubot/static/app.js#L64)变成`{"seed":null}`。后端收不到原始非法数字，因此无法给出有限值错误。Node执行实际共享解析函数已复现；应在前端解析后拒绝非有限数字并补充回归。此问题可能造成参数失效或供应商拒绝，不构成预算绕过。

### 本地验证与整合边界

- 在隔离的精确提交快照中执行作者新增的后端及Node前端测试：**41 passed**。
- 执行既有供应商、模型目录/选择、流式账本、人格蒸馏及模型分段回归：**214 passed**。与上组不重叠，共**255 passed**。初次沙箱运行无法调用Windows DPAPI，相关加密/登录用例出现环境性失败；在正常Windows用户环境、全新隔离数据目录中原样重跑后全部通过，未修改密码或加密实现。
- 两项额外离线探针分别确认上述P2、P3。未使用真实QQ、用户密钥或收费模型；未执行全量测试，也未验收真实浏览器交互。
- GitHub工作流[39999191970](https://github.com/iownmmiku/mikubot/actions/runs/39999191970)为`action_required`，仍需维护者授权，不能记作CI通过或测试失败。作者评论中的全量测试结果属于作者报告，不作为本次独立验收结果。
- 隔离三方试合并发现`provider.py`有**2处文本冲突**：导入区和`_complete`方法附近。正式整合须同时保留本地`refresh_dispatch_time`、请求调度/耗时记录，以及PR的新参数及预算规则；不能直接用0.1.5文件覆盖工作区。其余本地有改动的PR生产文件可文本合并，但这不等于已完成整合测试。
- 本地0.1.6的“机器人配置 → 模型”仍调用`settingsModelForm()`和`ShioriPages.renderModels()`，共享高级参数组件可继续复用。贡献者署名已保留，未授予仓库权限。

## 首轮审查：`f91b625`（历史记录）

审查日期：2026-10-24（Asia/Hong_Kong）。

- PR：[feat: support advanced model generation parameters](https://github.com/iownmmiku/mikubot/pull/8)
- 作者：[HuangGuaKnn](https://github.com/HuangGuaKnn)
- 审查版本：`f91b6255311fb775e6bb6faec18f02340380e4ce`
- 基于：`d2ddf2c27c34785d9942294375e2fbbab61ba9b8`，即已发布的0.1.5；本地另有未提交的0.1.6改动。
- 结论：功能方向适合采纳，但需要修正后合并。本次仅审查并在README补充贡献者，未合并远端PR、推送或发布0.1.6。

## 适合采纳的部分

1. 模型增加Top-p、最大输出Token、思考强度和自定义请求参数，默认模型及目录模型共用配置能力。
2. 模型目录新增字段白名单，配置持久化及请求传参；`_same_model`将新字段纳入配置变更守卫。
3. 温度与Top-p范围校验、自定义JSON对象校验、请求核心字段保护的基本设计。
4. 滑块配合数字输入的交互方向，但应共享同一个表单组件，接入现有事件委托。

本地“机器人配置 → 模型”仍复用`ShioriPages.renderModels()`，PR中的目录编辑弹窗可以适配，无需恢复独立的旧模型导航。

## 合并前需要修正

### 1. 输出预算可被自定义参数绕过（P1）

[settings.py:487](https://github.com/iownmmiku/mikubot/blob/f91b6255311fb775e6bb6faec18f02340380e4ce/mikubot/settings.py#L487)的核心字段名单遗漏`n`和`max_completion_tokens`。离线实际调用中，`max_tokens=16`与`custom_params={"n":3,"max_completion_tokens":1000000}`可以同时保存，请求只预留16个输出Token，却携带额外的候选数和输出额度参数。支持这些参数的服务可能超出本地预算假设；其他服务可能拒绝冲突请求。

建议把影响输出数量、额度和工具协议的参数统一保留，不能由通用自定义参数覆盖；若需要支持别名，先统一转换并进行预算核算。

### 2. 模型输出上限与聊天默认值、蒸馏重试冲突（P1）

[provider.py:474](https://github.com/iownmmiku/mikubot/blob/f91b6255311fb775e6bb6faec18f02340380e4ce/mikubot/provider.py#L474)仅计算`min(caller_max, configured_max)`。普通聊天调用默认1000，即使界面设置8192或16000，实际请求仍为1000；蒸馏原有3000→12000的输出额度扩展又被新默认8192压低，改变旧配置行为。

建议区分聊天默认额度与任务显式限额，定义清楚优先级；旧配置迁移不要悄悄增加硬限制。预算预留、实际请求和界面说明须使用同一套规则。

### 3. 自定义数字缺少有限值与体积检查（P2）

[settings.py:489](https://github.com/iownmmiku/mikubot/blob/f91b6255311fb775e6bb6faec18f02340380e4ce/mikubot/settings.py#L489)接受非有限浮点数；合法JSON数字`1e309`也会被Python解析为`inf`。离线重现配置校验通过，但设置/模型目录返回值无法由Starlette序列化，或者在httpx发送前抛出`ValueError`。后一种情况还可能被错误结算为未确认消费。

建议检查`math.isfinite`，限制单值字符串和参数整体大小，并在预留、标记请求已派发之前完成请求序列化校验。

### 4. 滑块被现有CSP拦截（P2）

[settings-page.js:9](https://github.com/iownmmiku/mikubot/blob/f91b6255311fb775e6bb6faec18f02340380e4ce/mikubot/static/settings-page.js#L9)及[workspace-pages.js:130](https://github.com/iownmmiku/mikubot/blob/f91b6255311fb775e6bb6faec18f02340380e4ce/mikubot/static/workspace-pages.js#L130)新增内联`oninput`，而现有`script-src 'self'`不允许执行内联事件。滑块没有表单字段名，保存依赖旁边的数字输入，因此拖动不能更新实际提交值。

建议接入统一输入事件委托，双向同步滑块和数字框，保留现有CSP。两种控件的最大Token范围也应统一。

### 5. 目录弹窗重复温度字段（P2）

[workspace-pages.js:130](https://github.com/iownmmiku/mikubot/blob/f91b6255311fb775e6bb6faec18f02340380e4ce/mikubot/static/workspace-pages.js#L130)保留旧`name="temperature"`后，又新增同名输入。真实`getForm()`使用`Object.fromEntries(new FormData(form))`，后值覆盖前值。离线VM中修改上方温度为1.3，保存结果仍为下方旧值0.7。

建议删除重复字段，抽出共用高级参数组件，并统一JSON对象检查、中文标签及错误提示。

## 验证与合并方式

后台探针以内存加载审查版本函数和`httpx.MockTransport`验证真实参数校验、预算预留及请求构造；前端用Node VM执行PR中的实际表单函数与现有`getForm()`。重复温度覆盖、内联处理器、参数映射和JSON文本转义均已检查。未发现自定义JSON文本在现有`textarea()`中的HTML注入问题；这些离线检查不等同于真实浏览器或供应商验收。

PR修改6个生产文件，没有新增测试。GitHub工作流`39991350946`的状态为`action_required`，需要维护者授权，不能记作CI通过或代码测试失败。合并前至少补齐预算字段保护、非有限值、输出限额实际生效、旧配置迁移、蒸馏重试、重复字段和滑块交互的回归。

建议修正后保留作者原始提交来源，再整合到本地新模型入口；不要用PR基于0.1.5的文件直接覆盖0.1.6工作区。README已注明作者与PR，贡献者署名不授予仓库管理权限。

发布前完整回归另有一个本地待办续写兼容失败，发生在引入PR之前，见[测试报告](../TEST_REPORT.md)。它与本PR的可合并性分别处理。0.1.6继续暂停发布。
