# 本地改进：部署、隔离与验收

本文描述0.1.4。正式桌面目标为Windows；Linux核心的Python3.11–3.13回归、Docker镜像构建和隔离探针此前0.1.1已在[GitHub Actions](https://github.com/iownmmiku/mikubot/actions/runs/39226282587)通过。本机Windows家庭版仍未运行Docker，真实QQ、长期服务器部署与公网证书需按目标环境验收。

## Python 与包入口

支持 Python **3.11、3.12、3.13**，推荐3.12。核心包已迁为 `shiori`，旧 `qunyu` 导入与启动入口保留兼容，使用同一份实现；数据库文件名 `qunyu.db` 保留。Windows默认数据在 `%LOCALAPPDATA%\shiori\data`；Linux在 `${XDG_DATA_HOME:-$HOME/.local/share}/shiori/data`，可用 `--data-dir` 或 `SHIORI_DATA_DIR` 指定。

- `requirements-server.txt`：无头核心，不含WebView2、pythonnet、托盘。
- `requirements-desktop.txt`：核心与Windows桌面依赖。
- `requirements.txt`：保留完整兼容入口。
- `pip install -e .` 安装核心，`pip install -e ".[desktop]"` 加Windows桌面依赖。

Windows运行 `powershell -ExecutionPolicy Bypass -File .\scripts\setup-source.ps1`；只准备用 `-SetupOnly`，日常运行 `start.ps1`。桌面初始化失败自动切换默认浏览器，服务先完整停止并释放锁，再启动浏览器后端。无需手动输入回退参数。回退时尝试发送托盘通知，不显示要求作选择的信息框；通知不可用也不阻塞运行，原因保存在日志。

## Linux 无头启动

先准备支持版本的Python、venv与pip，再运行：

```bash
bash scripts/setup-source.sh --setup-only
.venv/bin/python -m shiori --data-dir "$HOME/shiori-data" --init-password
bash start.sh --data-dir "$HOME/shiori-data"
```

`--init-password` 在终端交互设密且不启动服务，无默认密码，也不把密码放命令参数里。默认只监听回环。远程使用先配置HTTPS，再以 `--host 0.0.0.0 --port 8765` 启动并限制后端只能由代理访问。停止用 `bash start.sh --data-dir "$HOME/shiori-data" --stop`。脚本可指定 `--python /path/to/python3.13`。

可编辑安装的构建工具准备、systemd标准入口、端口固定和本地API凭据示例见[源码部署指南](SOURCE_DEPLOY.md)。已有systemd应把 `-m qunyu` 改为 `-m shiori`，保留原数据目录与参数。当前源码新增 `--strict-port`，推荐固定反向WS、代理和容器部署使用；端口不可用直接退出。桌面仍可自动顺延，发生时日志写WARNING、WebUI顶部持续提示，`server.json` 同时记录请求与实际端口。POSIX的 `SO_REUSEADDR` 不能抢占活跃监听；外部地址不会自动改写。以上端口改进从0.1.4提供。

Windows在加盐scrypt密码校验值外加DPAPI，其他系统使用scrypt校验值与私有文件权限。非Windows的模型连接密钥目前为Base64编码，**不是加密**，须保护宿主账户和数据目录。跨系统不能解密Windows DPAPI记录，需重设密码、重新填写密钥。

## Docker 服务部署

准备本机Docker Linux引擎，在源码根目录运行：

```bash
docker compose build
docker compose run --rm --no-deps shiori --init-password --allow-http-lan
docker compose up -d
docker compose logs --tail 100
```

上面的HTTP选择仅用于Compose的**宿主回环映射**，浏览器打开 `http://127.0.0.1:8765/`。不要将该映射改成公网绑定。镜像为非root账户、只读系统目录、临时 `/tmp`、无Docker socket。数据在命名卷 `shiori-data` 中；`docker compose down` 保留数据，`down -v` 会删除卷，不用于常规升级。

容器服务不通过挂载宿主Docker socket来启用内置沙箱。服务容器中的“本机模式”是在服务容器内执行，仍继承该服务的网络和挂载数据权限。跨容器QQ连接请手动配置反向WebSocket和一致Token；自动管理连接器仅针对同一宿主回环。

## 使用 HTTPS 反向代理

在网络设置填写自己的HTTPS域名，点击“生成HTTPS部署配置”。软件只生成配置，不自动买域名、安装代理、申请证书或开放防火墙。以本机Caddy为例：

```caddyfile
bot.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

配置域名解析和有效证书后，运行 `caddy run --config Caddyfile`。对应Shiori现有 `settings.json` 段如下，**不要覆盖整个配置文件**：

```json
"webui": {
  "remote_enabled": true,
  "host": "0.0.0.0",
  "port": 8765,
  "allowed_hosts": ["bot.example.com"],
  "public_url": "https://bot.example.com",
  "require_https": true,
  "trusted_proxy_ips": []
}
```

代理保留 `Host: bot.example.com`，设置 `X-Forwarded-Proto: https`。转发头只有在真实TCP对端是本机回环或明确配置的独立代理IP，且公网Host匹配时才可信；不接受整个网段或通配符。代理信任不会授予本机初始化、免密或重置权限，不以 `X-Forwarded-For` 判断本机身份。不要将公网Host改为localhost；限制外部访问 `/api/auth/bootstrap`、`/api/auth/setup`、`/api/session`。监听地址/端口变更后重启。

Host侧代理经Docker桥转发时，后端TCP对端可能不是127.0.0.1。经宿主回环登录后，在“代理高级设置”填入**实际且固定的代理IP**，可用 `docker inspect` 查看网络Gateway辅助核对；地址变化后同步更新。重新开启“要求HTTPS”，后端仍只映射到宿主回环，仅公开代理HTTPS入口。不要通过允许公网HTTP修补代理配置。

远程HTTP默认拒绝，包含登录请求。只有可信局域网明确选择允许HTTP或传入 `--allow-http-lan` 后才能使用，此选择会保存；HTTP不加密密码、Cookie和聊天。这里提供部署约定，没有把域名、证书或公网连通性自动标成通过。

## 密码恢复与数据迁移

在**运行电脑的托盘菜单**选择“重置 WebUI 密码”，或执行 `python -m shiori --data-dir 实际数据目录 --reset-password`。随后从本机重新设密；无头部署再运行 `--init-password`。该入口使旧会话失效，保留聊天记忆，没有对应的HTTP重置接口。

仍支持完全退出后仅删除 `security/webui-password.json`，重开后重新设密。不要删除整个数据目录。升级前退出旧实例并完整备份数据。应用导出备份脱敏，不包含有效密钥、密码或蒸馏阶段检查点；原材料、附件、Skill、修改副本和工作目录需另备。一个数据目录仍只允许一个进程写入，不支持多节点共享SQLite，也不要删除锁文件来绕过。

## 代码隔离与任务恢复

新安装默认 `computer.runtime: docker`，电脑能力默认关闭；旧本机配置不强制迁移。先准备本机Docker Linux引擎，手动运行 `docker pull docker.io/library/python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`，再点击“检查隔离环境”。不自动下载镜像、不连接远程Docker、不降级到宿主执行。

Docker 沙箱隔离主机系统，不保护工作目录内的文件；/workspace 是可写映射，脚本可在其中增删文件。内置Shell/Python容器仅挂载专用工作目录和本次只读脚本，断网、只读系统、非root、移除capabilities、禁止新权限，限制512MB内存、1CPU和64进程。拒绝工作目录中的链接/联接、磁盘根目录和管理数据目录。Shell是POSIX，不能直接运行宿主Windows GUI；容器隔离仍依赖宿主内核和引擎安全。

本机模式明确使用当前系统账户权限，工作目录不是Shell/Python隔离边界。文件工具由宿主执行且有路径约束；MCP服务不受该容器隔离。电脑能力开启且选择Docker时，默认禁止MCP调用（包括已在等待审批的动作）；确有需要须在高级设置开启“允许沙箱外MCP工具”或配置`computer.allow_external_tools: true`，仍受管理员、会话与允许列表检查。本机模式保留原有调用流程。自动执行仅对真实管理员生效。逐步审批支持原会话“同意/拒绝”，同一发起者、唯一待审批动作、5分钟有效；有歧义时仍要求编号和审批码。

蒸馏完成阶段保存检查点，重启后可手动“从检查点继续”，复用成功分析和来源快照并保留累计预算；模型、材料或参数变化拒绝复用。同一原任务已有进行中的续作时，再次点击会返回该续作。已发出但未收到回执的模型请求可能再次计费。电脑命令不重放，先核对已有文件与外部效果，再建立后续任务。

## 验收入口

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/check_docker_isolation.py --require
.\.venv\Scripts\python.exe scripts/accept_live.py --url http://127.0.0.1:8765 --seconds 60 --output .qa-live-report.json
```

Docker脚本真实检查宿主文件不可见、根目录只读、网络不可用和工作目录可写。无引擎时显示unavailable，带 `--require` 返回失败，不执行宿主替代命令。

真实QQ验收脚本不改配置、不代发消息，由用户在观察窗口发送测试消息。报告只留诊断代码与计数，登录密码交互输入，Cookie不落盘。加 `--test-model` 才允许一次真实模型调用，可能计费。未观察到平台发送回执标记inconclusive；平台回执不等于用户已阅读。

真实本地TCP回归覆盖429、坏格式、流中断、超时、错误Token和WebSocket断开重连，仍非真实QQ账号或付费模型验收。`.github/workflows/ci.yml` 已在上述代码提交上完成Windows/Linux、Python3.11–3.13和Docker验收；对应结果不扩大为真实QQ或全部部署环境均通过。


## 0.1.3：镜像摘要与共享数据目录

沙箱引用固定 `docker.io/library/python:3.12-slim@sha256:...`，2026-10-20从Docker Hub官方library/python的OCI索引核验并固定。实际使用 `shiori/sandbox.py` 的 `IMAGE`，环境检查要求本地RepoDigests匹配；同名标签不是通过依据。更新摘要需维护者重新核验官方索引、在隔离环境跑 `check_docker_isolation.py --require`，再同步CI和文档，不在用户启动时自动跟随最新标签。这是内容固定，不是独立镜像签名验证。

数据目录增加 `.instance-owner.json`，配合OS锁防止桌面、后台、跨宿主或容器命名空间同时写入。正常退出移除自己的标记；同宿主/命名空间的PID可证明已退出时自动回收。外来命名空间或损坏记录无法证明已退出，拒绝启动。必须先停止所有实例、核对占用，再手动删除此标记；不能通过删锁运行第二个写入者。不支持共享SQLite集群或网络文件系统的多写入者。
