# Shiori 源码部署指南

本文描述0.1.6正式版源码部署，安装包、便携包与源码在[GitHub Releases](https://github.com/iownmmiku/mikubot/releases/tag/v0.1.6)按实际公开资产下载。端口告警与 `--strict-port` 继续可用；完整隔离、HTTPS、Linux/Docker步骤与验收范围见[部署与安全说明](DEPLOYMENT_AND_SAFETY.md)。

原0.1.6 beta内部版本也为0.1.6。升级时重新检查并选择正式发行记录；若显示“当前版本”，仍可切换同版本修复包。源码有本地修改时不会自动覆盖，应先保留改动，再在独立目录部署正式源码并沿用原数据目录。旧beta资产仅供手动历史下载，不作自动切换目标。

## Windows：一条命令准备并启动

安装 **Python 3.11–3.13（推荐3.12）**，保留pip和venv。在源码根目录打开PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-source.ps1
```

首次需要网络，脚本创建`.venv`、安装`requirements-desktop.txt`，然后打开独立桌面窗口。无需Git、Node或前端构建。WebView2缺失或加载失败自动回退默认浏览器。

```powershell
# 只准备环境；解释器也可用 -Python 指定完整路径
powershell -ExecutionPolicy Bypass -File .\scripts\setup-source.ps1 -SetupOnly
# 日常启动、显式浏览器模式与安全停止；始终使用同一数据目录
powershell -ExecutionPolicy Bypass -File .\start.ps1 -DataDir 'D:\shiori-data'
powershell -ExecutionPolicy Bypass -File .\start.ps1 -DataDir 'D:\shiori-data' -Browser
powershell -ExecutionPolicy Bypass -File .\start.ps1 -DataDir 'D:\shiori-data' -Stop
```

默认数据目录为`%LOCALAPPDATA%\shiori\data`，独立于源码和安装目录。脚本不覆盖数据、不替换不兼容虚拟环境；依赖损坏可用`-Reinstall -SetupOnly`。`-NoBrowser -NoTray`为纯后台；`-Port`指定端口，`-BindHost`指定监听网卡。关闭网页不会停止后台程序，有托盘时关闭窗口也可继续运行。

## 手动部署与 Python 包入口

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe -m shiori --data-dir 'D:\shiori-data'
```

无头服务改装`requirements-server.txt`并附加`--no-browser --no-tray`，不要求GUI或.NET。也可`pip install -e .`安装核心、`pip install -e ".[desktop]"`加入Windows桌面依赖。正式包名为`shiori`，旧`mikubot`、`qunyu`导入与启动入口兼容，数据库`qunyu.db`不改名。

## Linux 与 Docker

```bash
bash scripts/setup-source.sh --setup-only
.venv/bin/python -m shiori --data-dir "$HOME/shiori-data" --init-password
bash start.sh --data-dir "$HOME/shiori-data"
```

支持Python3.11–3.13；系统需提供对应的 `venv` 和 `pip`（Debian/Ubuntu按所选解释器安装对应的 `python3-venv` / `python3-pip` 包）。先在终端交互设密，再启动服务。默认监听回环，远程需要HTTPS。Docker构建、首次密码、持久卷、代理与容器内QQ地址见[Linux/Docker部署步骤](DEPLOYMENT_AND_SAFETY.md#docker-服务部署)。已发布0.1.3的Windows/Linux、Python3.11–3.13和Docker检查见[GitHub Actions](https://github.com/iownmmiku/mikubot/actions/runs/39423537788)，不代替真实QQ或长期挂机验收。

若使用可编辑安装，在已创建的虚拟环境中运行：

```bash
.venv/bin/python -m pip install --upgrade pip 'setuptools>=75,<83' wheel
.venv/bin/python -m pip install -e .
```

正常的隔离构建会按 `pyproject.toml` 自动准备构建后端。旧pip、禁用构建隔离（`--no-build-isolation`）或不完整的虚拟环境可能出现 `BackendUnavailable` / `setuptools.build_meta` 无法导入；确认使用同一个 `.venv/bin/python`，先执行上面的构建工具安装再重试。离线环境需要事先准备匹配的wheel包；不要直接修改系统Python。`setup-source.sh` 安装的是依赖文件后从源码目录运行，不需要 `pip install -e .`。

## 固定监听端口与 systemd

桌面默认在请求端口不可用时尝试后续29个端口。当前源码在POSIX使用 `SO_REUSEADDR` 减少旧TCP连接残留对重启的影响；它不能抢占仍在监听的进程。发生顺延会在 `runtime.log` 写入WARNING，并在WebUI顶部持续显示请求端口和实际端口。`server.json` 的 `port` 是实际端口，`requested_port` 是本次请求端口；保存的配置不会被顺延覆盖。

NapCat/SnowLuma反向WS、代理和Docker端口映射不会自动跟随。固定地址部署建议添加 `--strict-port`：端口不可用就明确退出，不启动到其他端口。当前源码的Docker默认命令已带此参数。

```bash
bash start.sh --data-dir "$HOME/shiori-data" --port 8765 --strict-port
# 查看实际占用；先确认进程身份，再决定停止哪个服务
ss -ltnp 'sport = :8765'
```

配置systemd时，以下占位路径和 `your-user` 必须替换为实际值；使用已完成密码初始化、与日常启动相同的数据目录：

```ini
[Unit]
Description=shiori
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/shiori
ExecStart=/path/to/shiori/.venv/bin/python -m shiori --data-dir /path/to/shiori-data --no-browser --no-tray --host 127.0.0.1 --port 8765 --strict-port
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

示例只监听回环，适用于本机HTTPS代理；跨容器反连需按实际网络选择可达监听地址并限制访问。旧服务先用 `systemctl cat shiori` 核对生效的unit及drop-in，把原 `ExecStart` 中的 `-m qunyu` 改为 `-m shiori`，**保留原解释器路径、数据目录和所有其他参数**。需要固定端口时再加 `--strict-port`；它只在含本次修改的源码中可用。修改完成后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart shiori
sudo systemctl status shiori --no-pager
sudo journalctl -u shiori -n 50 --no-pager
```

应用WARNING与异常也记录在实际数据目录的 `runtime.log`。旧 `qunyu` 入口计划0.3.0移除，数据库名 `qunyu.db` 保留；迁移入口不需要重建数据库。更新源码不会自动改写宿主机systemd服务文件。

## 本地 API 调用与鉴权

`X-Token` 不是受支持的鉴权头。`/healthz` 仅用于无凭据探活；业务API默认需要登录会话。宿主机脚本可读取**正在运行实例所用数据目录**中的 `admin.token`，通过 `Authorization: Bearer <令牌>` 调用；这只对真实回环连接且Host为localhost/回环IP生效。令牌应作为凭据保护，不放URL、不上传日志，也不要转发到公网。

以下脚本不打印令牌，从 `server.json` 读取实际端口，仅向127.0.0.1发送请求，禁止使用环境代理和跟随HTTP重定向；先把数据路径改为实际值：

```python
import json
from pathlib import Path
import urllib.request

data = Path('/path/to/shiori-data')
port = json.loads((data / 'server.json').read_text('utf-8'))['port']
if type(port) is not int or not 1 <= port <= 65535:
    raise ValueError('server.json 中的端口无效')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
request = urllib.request.Request(
    f'http://127.0.0.1:{port}/api/bootstrap',
    headers={'Authorization': 'Bearer ' + (data / 'admin.token').read_text('utf-8').strip()},
)
with client.open(request, timeout=10) as response:
    result = json.load(response)
print(result['name'], result['version'])
```

在另一台电脑或Docker容器里请求宿主机不属于宿主回环，Bearer不会授予管理权限。远程脚本应通过已配置HTTPS域名 `POST /api/auth/login`，正文 `{"password":"管理密码"}`，使用Cookie jar保留返回的 `shiori_session` Cookie，再用同一会话调用业务API；密码从交互输入或安全凭据源读取，不写进脚本或命令历史。写请求的 `Origin` 如有提供必须匹配实际访问域名。不要为远程脚本开放本机初始化接口。OneBot反向WS另用连接器的access token，不使用WebUI的 `admin.token`。

## 使用 HTTPS 反向代理

远程访问默认要求HTTPS。在“登录与远程管理”填写自己的HTTPS域名，生成Caddy配置，完成有效证书与代理部署。只允许可信TCP代理提供的HTTPS转发头，保留公网Host；容器桥接需明确配置固定代理IP。详细配置见[HTTPS步骤](DEPLOYMENT_AND_SAFETY.md#使用-https-反向代理)。监听地址/端口修改后需重启。

可信局域网需要原始HTTP时，明确勾选“允许明文HTTP”或传入`--allow-http-lan`，该选择会保存；HTTP不加密密码和聊天，不直接公开后端端口。仅本机免密不能同时启用远程入口。

## 密码、数据与恢复

首次在桌面设密；无头使用`--init-password`。忘记时用本机托盘“重置 WebUI 密码”，或：

```powershell
.\.venv\Scripts\python.exe -m shiori --data-dir 'D:\shiori-data' --reset-password
```

聊天和记忆保留，旧会话失效；无头随后再次`--init-password`。也可完全退出后仅删除`security/webui-password.json`，重开后重新设密。无HTTP重置入口。Windows密码校验值由DPAPI保护，其他系统使用scrypt与文件权限；跨系统需重新设密、填密钥。

升级前退出旧实例并完整备份数据。应用备份脱敏，不代替原始数据副本；原附件、蒸馏材料、检查点、Skill和工作目录另备。同一数据目录仍只允许单实例写入。

## 隔离、恢复与验收

新安装电脑任务默认选择Docker隔离，须先准备本机Linux容器引擎和按固定摘要锁定的官方Python镜像（完整pull命令见“检查隔离环境”及隔离说明）；不可用就停止，不降级到宿主。旧本机模式保留；本机Shell/Python具有当前系统账户权限。文件工具和MCP与代码容器的边界见[隔离说明](DEPLOYMENT_AND_SAFETY.md#代码隔离与任务恢复)。

蒸馏任务支持按检查点继续；电脑命令不自动重放。真实本地网络回归、可选真实QQ/模型验收、Docker隔离脚本和持续集成矩阵见[验收入口](DEPLOYMENT_AND_SAFETY.md#验收入口)。替身通过不代表真实QQ账号、模型质量、Linux/Docker或公网证书验收通过。

默认不调用真实模型的回放命令：

```powershell
.\.venv\Scripts\python.exe -m shiori --replay .\evals\routing-synthetic.json --replay-output .\replay-report.json
```

真实模式显式开启后可能计费，见[回放说明](archive/REPLAY-v0.6.md)。源码发行白名单包括部署文件、核心与兼容包；维护者构建使用`scripts/package_source.py`和`scripts/build.ps1`。

源码可在设置中选择GitHub发行版并下载源码包，但不会自动切换分支或覆盖工作区。更换代码请在新目录准备依赖，保持同一个`--data-dir`；详见[更新指南](UPDATES.md)。
