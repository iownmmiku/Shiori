# 扩展开发说明

扩展用来给 Shiori 增加新本领：给聊天模型加工具、在控制台里放自己的设置页、声明一组设置让控制台自动生成表单。Shiori 自带的“生图”就是一个扩展（`shiori/extensions/imagegen/`），可以照着它写。

## 扩展和“插件”的区别

| | 扩展 | 插件（[本地插件](PLUGINS.md)） |
|---|---|---|
| 在哪里运行 | 直接在 Shiori 进程里，权限和 Shiori 相同 | 电脑能力的沙箱或本机任务里，按授权运行 |
| 谁能触发 | QQ 聊天里的聊天模型（通过工具）、控制台 | 只有开了电脑能力的本地管理员 |
| 能做什么 | 给聊天模型加工具、回复后再发图片或文字、注册接口、带设置页 | 把一段 `run(input)` 保存成可复用的功能 |

扩展能读写 Shiori 的数据、联网和执行任意代码，所以只启用 Shiori 自带的、自己写的或来自可信来源的扩展。

## 放在哪里

- Shiori 自带的扩展在程序目录的 `shiori/extensions/<id>/`，默认启用（清单里 `"default_enabled": false` 的除外）。
- 其他扩展放在数据目录的 `extensions/<id>/`，重启 Shiori 后出现在控制台的“扩展”页。它们默认停用；第一次启用要在页面上确认可信。扩展文件夹里的任何文件改动后（`.git` 这类隐藏文件夹和 `__pycache__` 除外），下次启动会自动停用，需要重新确认。
- 扩展自己的数据放在 `ext.data_dir`（数据目录的 `extension_data/<id>/`）。普通应用备份不包含扩展数据。

## 文件结构

```text
hello/
├─ extension.json        # 清单
├─ main.py               # 入口，定义 setup(ext)
├─ helper.py             # 可选，main.py 里用 from .helper import ... 导入
└─ pages/
   └─ main/
      ├─ index.html      # 设置页
      ├─ app.js
      └─ app.css
```

## 清单 extension.json

```json
{
  "id": "hello",
  "name": "挥手",
  "version": "0.1.0",
  "author": "你的名字",
  "description": "一句话说明扩展做什么。",
  "entry": "main.py",
  "pages": [{"id": "main", "title": "挥手设置", "entry": "pages/main/index.html"}],
  "config": {
    "greeting": {"type": "string", "title": "问候语", "description": "说明文字", "default": "你好"},
    "times": {"type": "int", "title": "次数", "default": 1, "min": 1, "max": 5},
    "mood": {"type": "select", "title": "心情", "options": [["calm", "平静"], ["happy", "开心"]], "default": "calm"},
    "token": {"type": "secret", "title": "令牌"}
  }
}
```

- `id`：2—40 个小写字母、数字或下划线，以字母开头，必须和文件夹名相同。
- `entry`：扩展文件夹里的 `.py` 文件，默认 `main.py`。
- `pages`：最多 8 个页面，`entry` 指向扩展文件夹里的 `.html`。页面只能读取它所在文件夹及子文件夹里的文件。
- `config`：设置项，控制台按它生成“设置”表单。`type` 可选 `bool`、`int`、`float`、`string`、`text`、`select`、`secret`。`secret` 加密保存，页面不回显，不能带默认值。

清单有错误时，扩展页会显示原因，扩展不加载，其他扩展不受影响。

## 入口 setup(ext)

```python
from shiori.extension_host import ToolResult


def setup(ext):
    def schema(turn):
        # 返回这一轮要提供的工具定义；返回 None 就不提供。
        if not turn.scope_id.startswith(('qq:', 'qqofficial:')):
            return None
        return {'type': 'function', 'function': {'name': 'wave', 'description': '对方让你挥手时调用。',
                'parameters': {'type': 'object', 'properties': {'to': {'type': 'string'}},
                               'required': ['to'], 'additionalProperties': False}}}

    def handler(call):
        async def after(send):
            await send.text('（挥手）' + call.args['to'])
        return ToolResult({'ok': True, 'status': 'queued', 'message': '挥手会在文字之后发出。'},
                          after_reply=after, cancel=lambda: None)

    ext.tool('wave', schema, handler, hint='你可以用 wave 挥手。', queued=True)
    ext.api('GET', 'state', lambda request: {'greeting': ext.config()['greeting']})
```

`setup` 必须是普通函数（不是 `async def`），出错时扩展不加载，错误写进运行日志。`ext` 提供：

| 成员 | 说明 |
|---|---|
| `ext.id`、`ext.name`、`ext.root` | 扩展 ID、名称、扩展文件夹 |
| `ext.data_dir` | 扩展自己的数据文件夹 |
| `ext.config()` | 当前设置（按清单补上默认值，密钥已解密） |
| `ext.store`、`ext.settings` | Shiori 的数据和设置对象，例如 `ext.store.add_event(...)` 写运行日志 |
| `ext.personas()` | 人格列表 `[{id, name}]` |
| `ext.state` | 扩展自己用的字典 |
| `ext.tool(name, schema, handler, hint='', queued=False)` | 给聊天模型加工具 |
| `ext.api(method, path, handler)` | 注册接口 |
| `ext.on_unload(fn)` | 扩展停用或 Shiori 退出时调用 |
| `ext.turn_context(fn)` | 给每轮聊天补充说明和图片，见下文 |

### 工具

- 工具名不能和内置工具重名，不能以 `mcp_` 开头，也不能和其他扩展的工具重名。
- `schema(turn)` 每轮调用一次。`turn` 有 `scope_id`、`user_id`、`persona_id`、`images`（对方这一轮发的图片，`data:image/...` 地址）、`group`。会话关闭“允许使用工具”、模型不支持工具，或多人合并成一轮的群回复时，扩展工具一律不提供。
- `handler(call)` 可以是普通函数或 `async`。`call` 有 `scope_id`、`user_id`、`user_key`（如 `qq:12345`）、`persona_id`、`args`（已按 schema 校验）、`admin`、`images`。返回 dict 就直接交给模型；抛出 `ValueError` 时，消息会作为工具错误告诉模型。
- 返回 `ToolResult(output, after_reply=..., cancel=...)` 时，`after_reply(send)` 在文字回复送达后运行，`send.image(bytes, description='')` 发一张图片（`description` 用一句话写图里是什么，发出后会告诉聊天模型“这条回复之后发了这张图”，之后对方说起这张图她才接得上；0.2.4-beta.9 起支持），答应的图没画出来时调用 `send.image_failed(description, reason)`（不发消息，只告诉之后的回合“这张没画出来”和原因，免得她假装发了或反复重试；0.2.4-beta.10 起有，用前先检查有没有这个方法），`send.text(str)` 发一条短文字（最多 1000 字），都返回是否确认送达。回复没有发出（被打断、平台拒收、模型没写出文字）时改为调用 `cancel()`，用来退回预留的次数等；10 分钟内没有发出的也会取消。后来的消息不会取消已经开始的 `after_reply`。
- `queued=True` 表示工具本身只是预留、真正的效果在回复之后：如果一轮只调用了这类工具，回复失败时可以像没用工具一样重试，不会把工具记录当成回复发出去。
- `hint` 在提供这个工具的那一轮加进系统提示词。

### 本轮补充

`ext.turn_context(fn)` 注册的 `fn(turn)` 在每轮聊天组装提示词时调用（普通函数，要快；`turn` 同上）。返回 None 表示不补充，或者返回 `{'text': '…', 'images': [...]}`：

- `text` 最多 1500 字，加进这一轮的说明（跟在每轮不变的系统提示词后面，不影响它被模型服务缓存）。
- `images` 最多 3 张 `data:image/png|jpeg|webp;base64,…` 地址，接在对方这一轮发的图片后面。只有这一轮本来就带图片（说明聊天模型能看图）时才会发出，否则只用 `text`。
- 返回格式不对或者抛出异常时，这一轮不加任何东西，错误写进运行日志。

自带的生图扩展用它在对方发图时附上人格的外貌和参考图，让角色认得出图里的自己。较旧的 Shiori 没有这个方法，扩展可以先用 `hasattr(ext, 'turn_context')` 判断。

### 接口

`ext.api('GET', 'state', handler)` 挂在 `/api/extensions/<id>/api/state`，方法可选 `GET`、`POST`、`PUT`、`DELETE`，路径由字母、数字、`_`、`-` 组成，最多 5 段。接口沿用控制台的登录和跨站检查，扩展停用后返回 404。

`handler(request)` 可以是普通函数或 `async`，`request` 有 `method`、`path`、`query`（字典）、`body`（解析后的 JSON，没有时为 None）。返回能转成 JSON 的值。抛出 `ValueError` 返回 400 和消息，`KeyError` 返回 404，`PermissionError` 返回 403，其他异常返回 500 并写运行日志。

## 设置页

页面地址是 `/extensions/<id>/pages/<page>/`，控制台在“扩展”页里用同源 iframe 嵌入，需要已登录。框的高度固定为窗口高度减去页头，页面内容较长时在框内滚动。页面受和控制台一样的内容安全策略约束：不能写内联脚本，脚本和样式要放在单独文件里，只能请求 Shiori 自己的地址。

页面引入控制台样式和扩展 SDK 就能和控制台风格一致：

```html
<link rel="stylesheet" href="/static/styles.css">
<link rel="stylesheet" href="/static/experience.css">
<link rel="stylesheet" href="/static/theme.css">
<link rel="stylesheet" href="/static/extension-page.css">
<script src="/static/extension-sdk.js" defer></script>
<script src="app.js" defer></script>
<body class="extension-page">…</body>
```

`window.ShioriExtension` 提供：

| 成员 | 说明 |
|---|---|
| `api(route, {method, body})` | 调用本扩展注册的接口，出错时抛出带说明的 Error |
| `escape(text)` | HTML 转义，拼接页面时所有外部内容都要转义 |
| `ui.field / textarea / select / toggle / button / values(form)` | 和控制台一致的表单片段 |
| `dialog(title, description, bodyHtml, onSubmit, label)` | 对话框；`onSubmit(values)` 抛错时显示错误、不关闭 |
| `confirm(title, text, perform, label)` | 确认框 |
| `toast(message, error)` | 底部提示 |

完整例子见 `shiori/extensions/imagegen/pages/main/`。
