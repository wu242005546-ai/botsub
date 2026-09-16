# SUB_BOT V2

每日自动扫描 GitHub 上的免费 Clash / v2ray 订阅源，验证有效性后输出并邮件通知。

- 每天 **02:00 UTC（北京时间 10:00）** 由 GitHub Actions 定时运行一次。
- 运行结束把当日结果通过 **QQ 邮箱**发到指定收件箱。
- 状态与历史保留在 `output/state.db`（SQLite），跨 run 去重。

## 目录结构

```
config.py                单一配置入口（默认 < 环境变量 < CLI）
main.py                  流水线入口，退出码 0/1/2
bot/
  domain.py              记录模型（SourceIdentity / 各 Result / Node）
  fetcher.py             统一 Fetcher + 内容缓存（同 URL 一次获取）
  analyze.py             分类 + 确定性打分 + 指纹
  discovery.py           Provider 协议：GitHub(Search/GraphQL/Debug/History)
  gh.py                  GitHub REST/GraphQL 客户端 + 配额预算
  telegram.py            Telegram 公开频道扫描（t.me/s/ 预览页，无需 Bot Token）
  tree_scan.py           Trees API 递归扫描 + truncated 目录 fallback
  verify.py              验证（独立验证缓存 + TTL）
  store.py               SQLite state.db（生命周期状态机）
  output.py              current/（全量）+ latest/（报告/manifest/diff）
  notify.py              QQ SMTP 邮件
  parse/                 base64 链 / clash yaml / node 列表 纯解析
tests/self_test.py       离线自检（不联网）
.github/workflows/daily-crawler.yml  每日定时 + 手动触发
```

## 功能说明

| 能力 | 说明 |
|---|---|
| 发现 | 关键词搜索 GitHub 仓库 + 历史召回 + 调试白名单；REST 搜索失败自动 GraphQL 兜底 |
| Telegram | 扫描 `TELEGRAM_CHANNELS` 配置的公开频道预览页；频道内的外部订阅链接并入常规发现池按正常流程验活，频道内直接贴的裸节点链接只做语法校验、单独输出（见下方说明），不需要 Bot Token |
| 扫描 | Trees API `recursive=1` 一次拿全仓库文件；`truncated=true` 时强制目录遍历 fallback（绝不静默少扫） |
| 提取 | 仓库里高分文件解析出订阅/节点；内嵌的裸订阅链接补进候选池 |
| 统一获取 | 同一次 run 内同一 canonical URL 只会真实请求一次（Fetcher memo + 内容缓存） |
| 验证 | 拉取内容、解 base64 链、解析节点、按"唯一主机数 ≥ MIN_VALID_NODES"判定存活；独立验证缓存 TTL 控制"要不要重新验证" |
| 状态机 | 新源 NEW → ACTIVE；失败分级 FAILED(1次)/DEAD(3次)；复活 REVIVED（DEAD→ACTIVE）；久未再现 STALE |
| 指纹 | canonicalize（去 fragment、还原 base64、统一大小写）→ SHA256；**密码/UUID 只进 hash，绝不落库、不入 git** |
| 输出 | `current/` 全量快照 + `latest/report.json`（报告）+ `manifest.json`（哈希）+ `diff.json`（本次变化）；原子写（tmp→rename） |
| 邮件 | 邮件正文带 run 摘要 + 新增/失败清单 + 全部链接；失败不影响 CI 判定 |
| 退出码 | 0=成功；1=完成但有验证失败（仍产出）；2=致命（无有效产出）→ 驱动 CI 红/绿 |

## 使用说明

### 1. 本机环境修改（无全局侵入，均可选）

> 需要 Python 3.10+。本机当前未安装/未加入 PATH，请先安装。

- 安装 [Python 3.10+](https://www.python.org/downloads/)，勾选 "Add python.exe to PATH"。
- 本目录安装依赖（推荐用 venv，不改系统环境）：
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```
- 自检（不联网）：
  ```powershell
  python tests\self_test.py
  ```
- 本地试跑（可选，需要 GitHub token；不打邮件）：
  ```powershell
  $env:PAT_TOKEN="ghp_xxx"
  python main.py --no-email
  ```
  - 用 `--debug-repos owner/repo` 指定白名单快速验证单个仓库：
    ```powershell
    python main.py --debug-repos yourname/yourrepo --no-email
    ```

### 2. GitHub Actions 部署

1. 把本目录推送到一个 GitHub 仓库。
2. Settings → Secrets and variables → Actions，添加：
   | Secret | 必填 | 说明 |
   |---|---|---|
   | `PAT_TOKEN` | 推荐 | GitHub Personal Access Token（提搜索配额；不填则用内置 GITHUB_TOKEN） |
   | `QQ_EMAIL` | 是 | 发件 QQ 邮箱（如 `1234@qq.com`） |
   | `QQ_EMAIL_AUTH_CODE` | 是 | QQ 邮箱 SMTP 授权码（非登录密码，QQ 邮箱设置里生成） |
   | `TO_EMAIL` | 是 | 收件邮箱 |

   如果要用 Telegram 公开频道扫描,再去 Settings → Secrets and variables → Actions → **Variables** 标签页(不是 Secrets,频道名不是密钥)添加:
   | Variable | 必填 | 说明 |
   |---|---|---|
   | `TELEGRAM_CHANNELS` | 否 | 公开频道用户名,逗号分隔,`@`可省略,如 `freenode_share,clashnode`；留空则完全不扫描,零开销。**不需要 Bot Token/API ID**——用的是频道自带的公开预览页 `t.me/s/<channel>`，只能扫公开频道 |
3. Actions 页会自动出现 **Daily Sub Crawl** 工作流（`workflow_dispatch` 手动触发，或等每日 cron）。
4. 工作流结束后会自动 commit `output/state.db` + `output/current` + `output/latest`（内容缓存不提交），并上传 artifact。

### 3. 可选环境变量（用默认即开箱）

`SEARCH_KEYWORDS`、`REPOS_PER_KEYWORD`、`MAX_REPOS_TOTAL`、`MIN_VALID_NODES`、`MAX_VERIFY_CANDIDATES`、
`VERIFY_CACHE_TTL_HOURS`、`CONTENT_CACHE_TTL_HOURS`、`NOTIFY_ALWAYS`、`SEARCH_PUSHED_DAYS` 等，见 `config.py`。

### 4. 输出样例

```
output/
  state.db                  # SQLite：跨 run 状态（含凭据指纹，不含明文）
  current/
    sub_clash_current.txt   # 通过验证的 Clash 订阅
    sub_v2ray_current.txt   # 通过验证的 v2ray 订阅
    sub_nodes_current.txt   # 通过验证的裸节点源(目前流水线里此分支恒为空,保留字段)
    sub_telegram_nodes_current.txt  # Telegram频道直接贴出的裸节点链接(仅语法校验,未二次验活)
  latest/
    report.json             # 本次 run 汇总
    manifest.json           # 输出文件 sha256
    diff.json               # 本次新增/变更/死亡/复活
    discovery_snapshot.json # 本次发现的现场（可解释）
  run.log
```

## 安全

- 节点 `password`/`uuid` 只在 fingerprint 里参与 SHA256，**不在 state.db**。
- 内容缓存目录 `output/_content_cache` 已 gitignore。
- `GITHUB_TOKEN` 只从环境变量读取，不写进任何文件。
- 每次 run 只对同一 canonical source 获取一次，按域名限流，避免把订阅源打爆。

## V1 → V2 迁移红线（本地做一次即可）

1. 用 V1 跑一天产出 golden baseline。
2. 在 V2 上 `git stash` 掉 state.db 后跑同样一天。
3. 对比 `diff.json` / `current/*.txt`：凡 V2 有而 V1 没有、或 V2 少了的链路，必须能解释清楚（truncated fallback / kind 判定 / 验证 TTL），解释不了就拒绝合入。