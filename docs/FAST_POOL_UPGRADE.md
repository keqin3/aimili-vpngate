# V2.1.6 快速节点池升级指南

## 本次修改
- 工作池默认住宅 200、机房 50。未知/移动类型不计入住宅目标；公开代理来源的住宅声明须验证出口后才能计数。
- HTTP 请求只读缓存，不抓节点、不做握手；50 条前端分页，支持 gzip，可选服务端分页。
- 统一后台任务，每轮分类/探测上限 20，连续失败 2 次才淘汰。系统级 OpenVPN/TUN 故障不视为 IP 失效。
- 住宅/机房均达到目标后不再抓来源、不按天整池轮换。待确认最多 60，候选缓存最多 2000。
- “更新节点”保持当前批次，仅补缺额。“换下一批（IP 不重复）”换成历史未用 IP；活动、收藏、固定节点保护。无新候选或验证出口曾使用时保留原批次。
- 换批历史持久化，重启和临时黑名单过期不会重用旧 IP。勿删除 pool_history.json。

## Linux 源码安装升级（root）

```bash
curl -fsSL https://raw.githubusercontent.com/keqin3/aimili-vpngate/main/upgrade.sh -o /tmp/aimili-upgrade.sh
bash /tmp/aimili-upgrade.sh
```

脚本备份 /opt/aimilivpn，保留数据目录与登录配置；一次性备份并迁移 /etc/default/aimilivpn 中的旧池参数为 200/50。第二次执行尊重已迁移后自定义设置。
升级完成后浏览器强制刷新（Ctrl+F5）。

```bash
cat /opt/aimilivpn/VERSION
ml status
ml logs
```

预期版本 2.1.6。查看面板“住宅/机房目标、缺额、待确认、已测可用”。首次启动或大批换批后台逐步填满；页面无需等待它结束。

## Docker 用户：必须从本仓库重新构建

现有 ghcr.io/baoweise-bot 镜像不是本次 keqin3 main 修改，直接 pull 不会得到本次代码。请先备份数据卷，再在当前克隆目录执行：

```bash
git pull --ff-only origin main
docker compose build --pull
docker compose up -d --no-deps aimilivpn
docker compose logs --tail=100 aimilivpn
```

保留原 Compose 项目名/目录和已有数据卷；不要运行 down -v。Compose 默认值已调整为 200/50。此处没有发布新远程镜像。

## 可调参数

| 环境变量 | 默认 | 作用 |
| --- | ---: | --- |
| RESIDENTIAL_RETAIN_LIMIT | 200 | 住宅保留目标 |
| HOSTING_RETAIN_LIMIT | 50 | 机房保留目标 |
| POOL_PENDING_LIMIT | 60 | 待确认窗口上限 |
| POOL_RESERVE_LIMIT | 2000 | 独立候选缓存上限 |
| POOL_PROBE_BATCH | 20 | 每轮分类与探测批大小 |
| POOL_CHECK_SECONDS | 300 | 自动检测/补齐调度间隔 |
| POOL_REFILL_RETRY_SECONDS | 900 | 缺额时上游重抓最小间隔 |
| EXTERNAL_SOURCE_MAX_PROFILES | 40 | 每来源每轮外部配置下载数，游标自动推进 |
| AUTO_PRUNE_FAILURE_THRESHOLD | 2 | 连续失败淘汰阈值 |

改源码服务环境文件后重启服务。旧 HOSTING_ROTATION_SECONDS / PUBLIC_PROXY_REFRESH_SECONDS 的独立定时抓取不再由 main 启动，由统一池任务取代。用户自行开启的“自动整理网页全部节点”仍保留，它只整理已有节点。

## 保护例外与不足情况

- 活动、收藏、固定节点保护，可能导致相应类别超过目标。
- 候选不足时明确显示实际缺额，不保证 200 个住宅出口均已可连；分类数量与实测可用数量分别显示。
- 住宅识别依赖 IP 情报与实际出口验证，未知不冒充住宅。
- 大批换批可从缓存立即得到候选，但住宅识别/握手验证依然需要后台时间。
- 历史候选耗尽时提示，不自动清空去重记录。

## 回滚

升级脚本显示时间戳备份路径。先停止服务，备份当前数据目录（含换批历史），再根据备份恢复代码及 /etc/default/aimilivpn 的 .before-pool-* 备份；避免覆盖较新的登录配置和历史数据。Git 部署也可切回升级前 commit f7543bd，再重启。切勿直接删除数据卷。

## 验证范围

99 项单元/HTTP 集成测试在 Windows Python 3.11 上通过（临时测试目录使用工作区继承 ACL，生产逻辑未打补丁）；Python 编译、页面 JavaScript 语法检查通过。包含实际本地 HTTP 分页、压缩和 config_text 不泄漏检查。
本机 Bash 在受限 Windows 环境无法启动，未完成 Bash -n / Docker / Linux TUN/OpenVPN 实机端到端验收。升级脚本内 Python 参数迁移已测试保留无关配置及幂等性。
未连接生产服务器，因此不宣称生产网站的具体提速倍率。
