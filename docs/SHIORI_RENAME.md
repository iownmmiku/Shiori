# Shiori 本地更名

本地更名在 **0.2.0** 随 GitHub 仓库改名（`iownmmiku/mikubot` → `iownmmiku/Shiori`）一并发布；更新日志见 `RELEASE_NOTES.md`。服务器没有更新。

## 当前入口

- 产品显示名：**Shiori**；Python 核心包：`shiori`。
- 源码：`python -m shiori`；Windows 也可运行项目根目录的 `start.ps1`。
- 便携程序：`shiori.exe`；安装包及源码包使用 `shiori-0.1.6-…` 文件名。
- 窗口、托盘、登录页、导航、应用图标、Windows 产品资源与部署脚本使用新名称；S 标记保留原青绿色配色。

## 数据与旧入口

全新安装默认数据目录为系统用户数据位置的 `shiori/data`。该目录为空而原 `mikubot/data` 或 `qunyu/data` 有数据时，直接沿用旧目录，不复制、合并或删除。显式指定的 `--data-dir` 仍优先；`SHIORI_DATA_DIR` 是新环境变量，兼容旧 `MIKUBOT_DATA_DIR`、`QUNYU_DATA_DIR`。

数据库文件名 `qunyu.db`、历史身份签名与数据锁保留兼容，以继续使用原密码、记忆、管理员和任务记录。已删除的默认人格不会因改名重新出现。默认初音未来人格和机器人自定义称呼不随产品改名而改变。

旧 `mikubot`、`qunyu` 导入及启动入口转发到同一 `shiori` 模块，兼容层计划在 **0.3.0** 移除。更新器和安装器识别旧发布文件名、旧安装/更新标记与旧服务名；已管理的 QQ 反向连接在重新配对时复用并改名，避免创建重复连接。浏览器页面和会话偏好沿用旧值。

Docker 卷名不会自动迁移：已有部署应确认原卷完整名称，并以 `SHIORI_DATA_VOLUME` 指定后再启动新 Compose。全新部署默认使用 `shiori-data`。新旧进程或容器不得同时写同一份数据。

## 保留的历史名称

GitHub 仓库已改名为 `iownmmiku/Shiori`，旧链接由 GitHub 自动跳转；0.1.6 及更早发行资产保留 `mikubot-…` 原名，更新器同时接受 `shiori-…` 与历史 `mikubot-…` 文件名。原始发行记录、审计记录、第三方许可证和旧构建备份保留历史名称。不要把旧 EXE 简单重命名后当作新构建分发。

## 本地验证

完整自动回归 **2558 项通过、3 项跳过**。实际便携 EXE 已验证启动、认证与 DPAPI、重启、记忆、工具、插件相关任务和退出；冻结程序的新旧模块导入共享同一类，Windows 产品资源为 **Shiori 0.1.6**。源码与部署配置检查不等于 Linux、Docker、真实 QQ 或服务器重新验收。
