# 每日科技新闻自动收集与邮件推送

每天早上 **08:00** 自动从多个中英文源抓取 **AI / 科学前沿 / 医疗健康 / 科技行业** 最新新闻，
外加 **Hacker News 中文摘要（Zeli）** 和 **GitHub 开源项目 7 日增速榜**，
整理成美观的 HTML 邮件发送到你的邮箱。纯 Python 标准库实现，**无需安装任何第三方依赖**。

> 📁 **项目位置**：`C:\Users\P4rfa1t\Documents\每日科技新闻\`
> 日报本地存档在项目内的 `history\` 子文件夹

---

## 一、文件说明

| 文件 | 作用 |
| --- | --- |
| `news_collector.py` | 主程序：抓取 + 生成邮件 + 发送 |
| `test_news_collector.py` | 单元测试（纯标准库 unittest）：`python -m unittest test_news_collector` |
| `config.json` | 配置文件：邮箱信息和新闻源（**需要你填写**） |
| `run_news.bat` | 供定时任务调用的运行脚本（ASCII + CRLF） |
| `run_hidden.vbs` | 静默启动器：让定时任务在后台运行、不弹出 cmd 窗口 |
| `setup_task.ps1` | 一键注册 Windows 每日定时任务 |
| `.env.example` | 密钥模板（复制成 `.env` 填值；`.env` 本身不入库） |
| `.github/workflows/daily-news.yml` | GitHub Actions：云端每日自动运行 |
| `translation_cache.json` | 标题翻译缓存（自动生成，可删除以强制重新翻译） |
| `send_state.json` | 发送状态（记录最近成功发送日期，用于补跑去重，自动生成） |
| `github_stars.json` | 每日 star 快照，仅在**自建** GitHub 榜（无噪不可用时的兜底）启用后才用到（自动生成） |
| `github_descriptions.json` | GitHub 项目原简介缓存，用于「原文 + 译文」展示（自动生成） |
| `安装定时任务-管理员运行.bat` | 双击即可（会请求管理员权限）重新注册定时任务 |
| `news_collector.log` | 运行日志（自动生成，**单文件上限 2MB，自动轮转保留 3 份**） |
| `history\` | 日报本地存档：每天一个 `.html` + `.txt`，另有带搜索的 `index.html`（自动生成） |
| `!发送失败提醒.txt` | 仅在发信失败时生成（项目目录 + 桌面），下次成功自动删除 |

---

## 二、第一步：填写 `config.json`

用记事本打开 `config.json`，修改 `email` 部分：

```json
"email": {
  "smtp_host": "smtp-mail.outlook.com",
  "smtp_port": 587,
  "use_tls": true,
  "use_ssl": false,
  "sender": "你的邮箱@outlook.com",
  "password": "你的密码或应用专用密码",
  "sender_name": "每日科技新闻",
  "recipients": ["接收邮件的邮箱@example.com"]
}
```

### 常见邮箱 SMTP 参数

| 邮箱 | smtp_host | port | use_ssl | use_tls | 密码填写 |
| --- | --- | --- | --- | --- | --- |
| Outlook / Hotmail | `smtp-mail.outlook.com` | 587 | false | true | Microsoft 账户密码或**应用密码** |
| QQ 邮箱 | `smtp.qq.com` | 465 | true | false | 在QQ邮箱设置里开启SMTP后得到的**授权码** |
| 163 邮箱 | `smtp.163.com` | 465 | true | false | 开启SMTP后得到的**授权码** |
| Gmail | `smtp.gmail.com` | 587 | false | true | **应用专用密码**（需先开启两步验证） |

> ⚠️ 注意：
> - `use_ssl` 为 true 时端口一般填 465；`use_tls` 为 true 时端口一般填 587，二者只取其一。
> - **Outlook** 若开启了双重验证，或提示“需要使用应用密码”，请到
>   <https://account.microsoft.com/security> → 高级安全选项 → 创建**应用密码**，
>   然后把应用密码填入 `password`。
> - 发送方（sender）和接收方（recipients）可以填同一个邮箱。

### 其他可调项（`settings`）

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `hours_lookback` | 只保留最近多少小时内的新闻 | 24 |
| `max_per_category` | 每个分类最多显示几条（全局默认） | 8 |
| `max_per_category_by` | 按分类覆盖条数上限，如 `{"网络安全": 12}`；用于源多的分类 | {} |
| `subject_prefix` | 邮件标题前缀 | `【每日科技新闻】` |
| `request_timeout` | 单个源抓取超时（秒） | 20 |
| `translate` | 是否翻译标题 | true |
| `translate_target` | 翻译目标：`auto`（英→中、中→英）/ `zh`（**只译成中文**）/ `en`（只译成英文） | zh |
| `translate_delay` | 每次翻译请求后的间隔（秒），防止被限流 | 0.25 |
| `translate_workers` | 翻译并发线程数（仅 MyMemory 后端） | 4 |
| `translate_summary` | 是否翻译新闻简介（不只是标题） | true |
| `translate_max_requests` | 单次运行最多发多少段翻译请求（仅 MyMemory 后端） | 80 |
| `fetch_og_image` | RSS 无图时，是否抓文章页 `og:image` 补图 | true |
| `og_image_delay` | 每次补图请求间隔（秒） | 0.2 |
| `og_image_limit` | 单次运行最多发起多少次补图请求 | 40 |
| `og_image_workers` | 补图并发线程数（1 = 串行） | 6 |
| `fetch_workers` | 并发抓取新闻源的线程数（1 = 串行） | 6 |
| `send_after_hour` | 最早可发送的小时（0–23）；登录补跑时若未到该点则跳过 | 8 |
| `send_retries` | 发送失败后的重试次数 | 3 |
| `send_retry_delay` | 每次重试之间的间隔（秒） | 15 |
| `github_trending` | 是否抓取 GitHub 热门项目 | true |
| `github_count` | 显示几个项目 | 20 |
| `github_since_days` | 统计窗口（天），即"近 N 日增速" | 7 |
| `github_lookback_days` | GitHub 候选池：只看最近 N 天内新建的项目 | 60 |
| `github_min_stars` | 项目最少 star 数（过滤噪声） | 100 |
| `github_search_pages` | 每条 GitHub 查询翻几页（单页最多 100 条，API 硬上限 1000） | 3 |
| `github_pool_max` | 候选池上限，防止 star 快照文件无限增长 | 600 |

> **为什么分类要单独配上限？** 全局 `max_per_category=8` 适合"每个分类 3–5 个源"的场景。
> 但像「网络安全」有 6 个源、「硬件极客」有 5 个源时，8 条会让大部分源的内容直接被丢弃
> （一屏里只能看到 2–3 个源的条目）。用 `max_per_category_by` 给源多的分类单独放宽即可：
>
> ```json
> "max_per_category_by": { "网络安全": 12, "硬件极客": 12 }
> ```
>
> 放宽后记得同步调大 `og_image_limit`——否则补图请求数不够，新分类的条目会缺图。

你也可以在 `sources` 数组里自由增删新闻源，每项格式为：

```json
{ "category": "分类名", "name": "显示名称", "url": "RSS 地址" }
```

---

## 三、第二步：测试运行

在项目目录打开 PowerShell：

```powershell
# 1) 只抓取、打印结果，不发送邮件（验证抓取是否正常）
python news_collector.py --dry-run

# 2) 正式运行一次，验证能否成功发送邮件
python news_collector.py
```

成功时你会在邮箱收到一封新闻邮件，同时 `news_collector.log` 里会记录过程。
若发送失败，日志会给出具体原因（如授权码错误、SMTP 被禁用等）。

### 运行单元测试

改动代码后可以先跑测试（不联网，秒级完成）：

```powershell
python -m unittest test_news_collector -v
```

覆盖解析、清洗、转义、并发抓取、去重、分类上限、GitHub 分页、无噪榜单、名次徽章、翻译方向、火山引擎签名、噪音清洗、存档清理等纯逻辑，共 190 个用例。

---

## 四、第三步：注册每天 08:00 定时任务

**以管理员身份**打开 PowerShell，进入项目目录后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
```

看到 `✅ 定时任务已创建：DailyTechNews` 即成功。

- 立即测试一次定时任务：
  ```powershell
  Start-ScheduledTask -TaskName "DailyTechNews"
  ```
- 查看任务状态：
  ```powershell
  Get-ScheduledTask -TaskName "DailyTechNews" | Get-ScheduledTaskInfo
  ```
- 删除任务：
  ```powershell
  Unregister-ScheduledTask -TaskName "DailyTechNews" -Confirm:$false
  ```

> 说明：任务设置为「错过时间后补跑」——如果 08:00 电脑关机，开机后会自动补发。
> 若希望电脑在睡眠/关机时也能准点运行，需要电脑处于开机且已登录状态。

### 错过时间会补发（重要）

任务同时带有两个触发器：**每天 08:00** 和 **每次登录时**。脚本内部会检查
`send_state.json`，实现「当天只发一次」：

- 08:00 电脑开着 → 准点发送；
- 08:00 电脑关着，稍后开机登录 → **自动补发当天邮件**（只要已过 08:00）；
- 同一天内多次触发（登录、手动运行等）→ 自动跳过，不会重复发送。

> 手动强制发送（忽略“今天已发过/未到时间”）：`python news_collector.py --force`

---

## 五、内置新闻源

| 分类 | 源 |
| --- | --- |
| AI 人工智能 | 量子位、The Decoder、MIT Technology Review |
| 科学前沿 | ScienceDaily、Nature、Phys.org |
| 医疗健康 | ScienceDaily Health、STAT News、Fierce Healthcare |
| 科技行业 | 爱范儿、钛媒体、InfoQ 中文、TechCrunch、The Verge |
| 网络安全 | KitPloit、PortSwigger Research、Krebs on Security、SANS ISC、Dark Reading、嘶吼 |
| 硬件极客 | Hackaday、CNX Software、Dangerous Prototypes、Raspberry Pi News、Adafruit Blog |

> 之前用过的 `Hacker News (hnrss.org)` 源因**经常超时**且与 Zeli 的中文 HN 摘要**重复**，已移除。
> `先知社区 (xz.aliyun.com)` 源因**持续握手超时**（每次白等 20 秒，还占掉一个并发槽位），已移除。

部分源可能因网络或对方限制偶发失败，程序会自动跳过并继续，不影响整封邮件。

---

## 六、标题翻译功能

每封邮件里，每条新闻标题的**下一行**都会显示对应的译文（灰色斜体，以 `→` 开头），
新闻简介也会翻译并显示在原文下方。

默认 `translate_target: "zh"`，即**只把英文译成中文**，中文内容保持原样不译。
这是因为读者是中文用户，把中文再翻成英文既没有信息量，又白白消耗翻译额度。

翻译默认使用**火山引擎机器翻译**（质量好、支持批量），未配置 AK/SK 时自动退回
免费的 **MyMemory**（按 IP 限流，容易撞 429）。译文缓存到 `translation_cache.json`，
同一段文本只翻译一次。

### 配置火山引擎翻译（推荐）

在 [火山引擎控制台 → 访问控制 → 访问密钥](https://console.volcengine.com/iam/keymanage/)
创建 **Access Key**（需要 AK + SK 两个值，不是单个 API Key），并确认已开通「机器翻译」服务。

```json
"translate": {
  "provider": "volcengine",
  "volc_access_key": "AKLT...",
  "volc_secret_key": "...",
  "volc_region": "cn-north-1"
}
```

| 字段 | 含义 |
| --- | --- |
| `provider` | `volcengine` / `mymemory` / `auto`（默认 auto：有 AK/SK 用火山，否则 MyMemory） |
| `volc_access_key` / `volc_secret_key` | 火山引擎访问密钥（AK + SK） |
| `volc_region` | 区域，默认 `cn-north-1` |

> 🔐 **建议用环境变量代替写进配置文件**（更安全，密钥不落盘）：
>
> ```powershell
> setx VOLC_ACCESS_KEY "你的AK"
> setx VOLC_SECRET_KEY "你的SK"
> ```
>
> 程序优先读环境变量，读不到才用配置里的值。

**AK/SK 权限很大**（等于账号级凭证）。建议在 IAM 里建子账号，只授予「机器翻译」权限，
用子账号的 AK/SK。

### 为什么用火山引擎

| 对比 | 火山引擎 | MyMemory |
| --- | --- | --- |
| 质量 | 好（专有名词、技术术语准确） | 一般 |
| 批量 | 支持，一次可传 20 条 | 只能一条一次 |
| 实测请求数 | 104 段 → **6 次请求** | 104 段 → 104 次请求 |
| 实测耗时 | **7 秒** | 80+ 秒，且常撞 429 |
| 额度 | 按字符计费（有免费额度） | 免费但按 IP 限流 |

### 翻译不出来的内容不会显示

机器翻译遇到专有名词、版本号、缩写时常常原样返回，例如 `frida v17.24.0` → `frida v17.24.0`。
这类"译文"只是重复一遍原文，属于噪音。程序会识别并**不显示译文行**：

- 译文与原文完全相同 → 不显示
- 去掉空白与标点后相同（`frida v17` vs `frida v17.`）→ 不显示
- 译文为空或接口报错 → 不显示

**工具名/库名不翻译**：像 `IMSIsting`、`TornadoC2`、`OshoosiClaw` 这类单个标识符，
机器翻译会当成普通单词硬翻（`IMSIsting` → 「即兴表演」），所以程序识别后直接跳过。

### 额度保护（MyMemory 后端）

1. **单次上限** `translate_max_requests`：默认 80 段，超出的下次再补（火山后端不受此限）；
2. **熔断**：撞到 429 或鉴权失败立刻停止本次剩余翻译，不会几十条请求轮着撞；
3. **缓存**：已翻译的内容永久复用，第二天不会重复消耗额度。

> 说明：MyMemory 是免费的机器翻译，质量一般。如果你更看重翻译质量，可以改用
> DeepL / 百度 / 大模型 API（需自行申请 Key），告诉我即可帮你接入。

---

## 七、新闻配图

每条新闻会尽量在左侧显示一张缩略图，来源分两层：

1. **优先**从 RSS 自身的 `media:content` / `media:thumbnail` / `enclosure` / 正文 `<img>` 提取；
2. RSS 没图时，自动抓取该文章网页的 `og:image` 兜底（如 ScienceDaily、TechCrunch、量子位等）。

关闭补图：把 `fetch_og_image` 设为 `false`。

**补图是并发抓取的**（`og_image_workers`，默认 6 线程）。补图是纯网络 IO、各条目互不干扰，
并发后这一阶段从约 28 秒降到 7 秒左右，是整个流程里最大的一块耗时节省。
把 `og_image_workers` 设为 `1` 可退回串行。

`og_image_limit` 限制的是**发起的请求数**（不是成功条数），程序会在提交任务前截断，
所以实际请求数不会超过这个值。放宽分类条数上限时，记得同步调大它。

> ⚠️ 提示：出于安全策略，**Outlook / 部分邮件客户端默认会拦截外部图片**。首次打开邮件时
> 若图片显示为空白框，点一下邮件顶部的「**下载图片**」或「显示图片」即可正常显示。
> 这是邮件客户端的通用行为，不是脚本问题。图片本身是直接引用新闻网站的地址，不占用存储。

---

## 八、GitHub 开源项目增长榜

邮件末尾会有一个 GitHub 增长榜区块，列出 star 增长最快的开源项目，包含：项目名（可点击）、
简介、**star 增量**、主语言，以及**自动生成的项目预览图**。

这个区块有**两种数据来源**：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1（默认） | **无噪趋势榜**（见 8.1） | 每天 30 条、含名次徽章、描述已是中文 |
| 2（兜底） | **自建快照算法**（见 8.2） | 无噪不可用时自动接管 |

### 8.2 自建算法（兜底，也可用作独立榜单）

自己用 GitHub Search API 找候选项目，再靠每日快照算出真实增量：

排名依据是**真实增量**，而不是总 star 数：

- 程序**每天都把一批候选项目的 star 数记录到** `github_stars.json`；
- 再次运行时，用「当前 star − 若干天前的 star」得到**真实新增 star**，按增量排序；
- 最近 7 天内新建的项目，总 star 本身就是窗口内的增量，所以第一天上榜即准确；
- 随着快照积累（一般几天后），榜单还会覆盖「老项目突然爆火」的情况。

### 候选池怎么保证不漏项目

「增速榜」最容易出的问题是：**只用 `sort=stars` 排序时，低星但正在起飞的新项目永远排在
前 100 条之外**，而那恰恰是增速榜最该抓的对象。因此程序对每条时间窗查询跑**两种排序**，
并**翻页**：

| 查询 | 排序 | 作用 |
| --- | --- | --- |
| 近 `lookback_days` 天新建 | `stars` | 保证榜单质量下限（大体量项目） |
| 近 `lookback_days` 天新建 | `updated` | 覆盖近期活跃、包括低星新项目 |
| 近 `since_days` 天新建 | `stars` | 窗口内新建的高星项目 |
| 近 `since_days` 天新建 | `updated` | 窗口内新建的爆发项目 |

实测（60 天窗口、min_stars=100）：

```
旧版（stars，第 1 页）      : 100 个候选
新版 stars 翻 3 页          : 300 个候选
新版 stars + updated 翻 3 页 : 555 个候选   ← 其中 255 个只靠 updated 才能发现
```

「只靠 updated 才发现」的样例：`anthropics/oss-scanner`、`ESPARGOS/esp-sdr` ——
star 数不高但正在快速增长，正是增速榜的目标。

> GitHub Search API 单页最多 100 条、总结果上限 1000 条，所以 `github_search_pages`
> 最大有效值是 10（默认 3）。翻页途中某页失败不会丢弃已取得的结果。

每个项目的说明行会标明增量，例如：

```
GitHub · ⭐ 3,592 · 新建 5 天 · C
GitHub · ⭐ 8,120 · 近 7 日 +1,240 · Python
```

**配置**（`config.json` 的 `settings`，仅兜底模式生效）：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `github_trending` | 是否启用该区块 | true |
| `github_count` | 榜单条数 | 20 |
| `github_since_days` | 统计窗口（天），即「近 N 日增速」 | 7 |
| `github_lookback_days` | 候选池：只看最近 N 天内新建的项目 | 60 |
| `github_min_stars` | 候选项目最少 star 数 | 100 |
| `github_search_pages` | 每条查询翻几页 | 3 |
| `github_pool_max` | 候选池上限（控制 `github_stars.json` 大小） | 600 |

**GitHub Token（强烈建议配置）**：填了 Token 后 API 额度从 10 次/分钟提升到 5000 次/小时，
翻页才跑得动（无 Token 时 4 条查询 × 3 页 = 12 次请求，很容易撞上限）：

```json
"github": { "token": "ghp_xxxxxxxx" }
```

也可以改用环境变量 `GITHUB_TOKEN`（更安全，Token 不落盘）。

> 📈 第一天运行时 `github_stars.json` 里只有当天快照，此时榜单等价于「近 7 天新建、star 最多的项目」；
> 连续运行几天后，就会逐步变成真正的跨天增量榜——这是正常现象。

### 8.1 GitHub 趋势榜（默认用「无噪」数据源）

邮件里的 GitHub 板块默认展示 **[无噪](https://www.wuzao.com/projects/trends/daily/)** 的
GitHub 趋势榜：**每天 30 个项目**，带名次徽章（前三名金色、4–10 名蓝色、其余灰色），
每条含中文描述、主语言、star 总数和该周期的增长量。

```
[1] morluto/rea                    1 日 +22,945 · ⭐ 293.0k · TypeScript
    Reverse engineer anything with agents, from app behavior down to native binaries
    用智能体逆向分析任何东西，从应用行为到原生二进制文件
```

### 项目简介：原文 + 译文

无噪榜单有时不给简介，而且榜单里的项目**每天都在变**。所以程序对**每一个**上榜项目
都去 GitHub API 取一次英文原简介，而不是只在"缺简介"时才补——否则每天新出现的项目
永远拿不到原文。

每条项目的简介按「原文在上、译文在下」显示：

| 情况 | 显示 |
| --- | --- |
| GitHub 有英文简介 | 英文原文 + 中文译文 |
| GitHub 简介本身是中文 | 只显示中文（无需翻译） |
| GitHub 没有简介 | 退回无噪给出的描述 |
| 机器翻译无效（等于原文） | 只显示原文，不显示译文行 |

**简介缓存**：抓到的原简介存到 `github_descriptions.json`，同一天重复运行不会重复请求。
如果某仓库当时没有简介，7 天后会自动重试（避免"后来补上了却永远拿不到"）。

**翻译来源优先复用无噪的中文**：无噪自带的中文描述质量比机器翻译好，且不消耗额度，
程序会优先用它作为译文；只有无噪没给描述时，才走机器翻译。

数据来源与周期（`config.json` 的 `wuzao`）：

```json
"wuzao": {
  "enabled": true,
  "period": "daily",
  "limit": 30,
  "timeout": 30,
  "retries": 2,
  "fetch_descriptions": true
}
```

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `enabled` | 是否启用无噪榜单 | true |
| `period` | 统计周期：`daily`（1 日）/ `weekly`（7 日）/ `monthly`（30 日） | daily |
| `limit` | 收录条数 | 30 |
| `timeout` | 抓取超时（秒） | 30 |
| `retries` | 失败重试次数 | 2 |
| `fetch_descriptions` | 是否去 GitHub 取每个项目的英文原简介 | true |

对应页面：`daily` → `/projects/trends/daily/`，`weekly` → `/weekly/`，`monthly` → `/monthly/`。

**为什么用外部榜单？** 该站已经基于自己的星标快照算好了真实增量，我们不需要再花几天时间
积累快照；而且它的描述**已经是中文**，省掉了机器翻译环节。实测每次运行省下 12 次 GitHub API
请求，整体耗时从约 94 秒降到约 48 秒。

**自动兜底**：如果无噪站点临时不可访问或页面结构变化导致解析为空，程序会**自动回退到
8.2 节的自建算法**，那一节的所有配置仍然有效。日志里会看到：

```
[INFO]  无噪榜单不可用，回退到自建 GitHub 增速榜。
```

> 该站无公开 API / RSS，本功能解析其服务端渲染的 HTML（页面不依赖 JS，抓取稳定）。
> 若对方改版导致解析为空，日志会明确提示「页面结构可能已变化」，此时可把
> `wuzao.enabled` 设为 `false` 回到纯自建模式。

---

## 九、Hacker News 中文摘要（Zeli）

除了 RSS 新闻源，邮件还包含一节 **「Hacker News 中文摘要（Zeli）」**，
内容来自 <https://zeli.app/zh> —— 它把 Hacker News 首页热帖翻译成中文并生成摘要。

- 程序读取其 RSS（`https://zeli.app/zh/rss.xml`），取**最新一天**的摘要；
- 默认收录**热度最高的 15 条**，每条含：中文标题（点击进原文）、中文摘要、HN 点赞 `▲`、评论 `💬`、原站域名；
- 按 HN 热度降序排列；
- 若该站点临时无法访问，会自动跳过这一节，**不影响其它内容**。

**配置**（`config.json`）：

```json
"zeli": {
  "enabled": true,
  "rss": "https://zeli.app/zh/rss.xml",
  "limit": 15,
  "timeout": 60,
  "retries": 3
}
```

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `enabled` | 是否启用该板块 | true |
| `limit` | 收录条数（0 表示全部） | 15 |
| `timeout` | 抓取超时（秒）；该站首次连接较慢，建议 ≥60 | 60 |
| `retries` | 失败重试次数 | 3 |

> 说明：`zeli.app` 使用 Cloudflare，脚本已**强制走 IPv4**（本机 IPv6 访问该站握手会超时）。

---

## 十、本地存档

每次生成日报时，会**自动在项目目录下的 `history\` 子文件夹里备份一份**，方便随时回看、离线浏览：

```
每日科技新闻\
└── history\
    ├── index.html        ← 索引页，列出所有历史日报（可在浏览器打开）
    ├── 2026-10-17.html    ← 当天日报（带图带译文，浏览器可直接查看）
    └── 2026-10-17.txt     ← 当天日报纯文本版
```

- 每天一个文件，按日期命名；同一天多次运行会覆盖为最新版本；
- 每次运行会刷新 `index.html` 索引；
- 索引页支持 **关键词搜索日期**，并显示**每天条数**与「共 N 天 · 累计 M 条」统计；
- 存档**在发送邮件之前**写入，所以即使发信失败也有本地副本。

**配置**（`config.json`）：

```json
"archive": {
  "enabled": true,
  "dir": "history",
  "keep_days": 0
}
```

`dir` 支持**相对路径**（相对于项目目录，推荐）或绝对路径；`enabled: false` 可关闭存档。

`keep_days` 控制保留天数：`0`（默认）永久保留；设成 `90` 就只留最近 90 天，
每次存档后自动删掉更早的 `.html` / `.txt`（`index.html` 不受影响）。

---

## 十一、常见问题

**Q：邮件发送失败 `SMTPAuthenticationError`？**
- 检查 `password` 是否填的**授权码/应用密码**，而不是登录密码。
- 确认已在邮箱服务商处**开启 SMTP 服务**。

**Q：收不到邮件？**
- 检查垃圾邮件文件夹；把发件人加入白名单。
- 查看 `news_collector.log` 确认是否发送成功。

**Q：想换时间？**
- 重新运行 `setup_task.ps1` 前，修改脚本里的 `$RunAt = '08:00'`。

**Q：某天没收到？**
- 双击 `run_news.bat` 手动跑一次，并查看日志定位问题。

---

## 十二、发送重试与失败告警

发送环节有两层保护：

**1）自动重试**：`send_email` 失败后会按 `send_retries`（默认 3 次）重试，
每次间隔 `send_retry_delay`（默认 15 秒）。网络抖动、SMTP 临时故障多半能自动恢复。

**2）失败告警文件**：若重试后仍失败，会在**两个地方**写下 `!发送失败提醒.txt`：

- 项目目录 `C:\Users\P4rfa1t\Documents\每日科技新闻\`
- **桌面**（最显眼）

文件里包含失败时间、原因、当天存档的完整路径，以及排查步骤。
**下次成功发送后，这些提醒文件会自动删除。**

**配置**（`config.json`）：

```json
"alert": {
  "enabled": true,
  "desktop_copy": true
}
```

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `enabled` | 是否生成失败提醒文件 | true |
| `desktop_copy` | 是否同时写到桌面（更容易看到） | true |

> 因为发信失败时**不会**更新 `send_state.json`，所以当天稍后只要再触发一次（登录或手动）
> 就会自动重试补发。也可以手动补发：`python news_collector.py --force`。

---

## 十三、密钥管理：`.env` 与环境变量

`config.json` 里的密钥字段现在写成**占位符**，真实值从环境变量读取：

```json
"email":  { "password": "${SMTP_PASSWORD}" },
"github": { "token":    "${GITHUB_TOKEN}" },
"translate": {
  "volc_access_key": "${VOLC_ACCESS_KEY:-}",
  "volc_secret_key": "${VOLC_SECRET_KEY:-}"
}
```

| 写法 | 含义 |
| --- | --- |
| `${NAME}` | 取环境变量 `NAME`；未设置则保留占位符原文（随后报"配置不完整"） |
| `${NAME:-默认值}` | 取环境变量；未设置则用默认值（冒号后为空即空串） |

占位符会**递归展开**，dict / list 里的字符串都能用，所以任何配置项都可以参数化。

**本机运行**：复制 `.env.example` 为 `.env` 并填值。程序启动时自动读取同目录 `.env`，
且**只填空位、不覆盖已有环境变量**——所以 CI 注入的值永远优先。

**云端运行**：GitHub Actions 不读 `.env`，用的是仓库 Secrets，见下一节。

---

## 十四、GitHub Actions 自动运行

仓库已经配好 `.github/workflows/daily-news.yml`，推上去即可每天自动跑。

### 触发方式

| 触发器 | 说明 |
| --- | --- |
| `schedule` | 每天 **UTC 00:00**（= 北京时间 08:00）自动运行 |
| `workflow_dispatch` | 在 Actions 页面点 **Run workflow** 手动跑；勾 `force` 可忽略"今天已发送"限制 |

> ⚠️ Actions 的 cron 用 **UTC**，不是北京时间。换算：`北京时间 - 8 小时 = UTC`。
> 想改成早上 7 点，就写 `cron: "23 23 * * *"`（UTC 前一天 23:00）。

> ⚠️ GitHub 的定时任务**不保证准时**：共享服务器繁忙时可能延迟 5～30 分钟。

### 密钥填在哪里

**仓库首页 → Settings → Secrets and variables → Actions → New repository secret**

点右上角 **New repository secret** 逐个添加：

| Secret 名称 | 值 | 必填 |
| --- | --- | --- |
| `SMTP_PASSWORD` | QQ 邮箱的 **SMTP 授权码**（16 位，不是登录密码） | ✅ 必填 |
| `VOLC_ACCESS_KEY` | 火山引擎 AK（想用火山翻译才填） | 可选 |
| `VOLC_SECRET_KEY` | 火山引擎 SK | 可选 |
| `VOLC_REGION` | 区域，默认 `cn-north-1` | 可选 |

> **`GITHUB_TOKEN` 不用自己填。** Actions 会自动注入一个内置的 `secrets.GITHUB_TOKEN`，
> 程序里 `os.environ.get("GITHUB_TOKEN")` 会直接命中它。GitHub 也**禁止**创建
> 以 `GITHUB_` 开头的自定义 Secret（会报 `Secret names must not start with GITHUB_`）。

填完后可以点 **Run workflow** 手动跑一次验证；密钥缺失会直接在发信那步报错。

### 这个 workflow 做了什么

1. 检出代码，装 Python 3.12
2. 从 Actions 缓存还原 `translation_cache.json` / `send_state.json` 等状态
3. 跑 204 个单元测试（**测试不过就不会发信**）
4. `python news_collector.py` 执行收集与发送
5. 把日志、日报存档打包成 artifact（保留 14 天）
6. 存回缓存，保证明天的"当天去重"和翻译缓存不丢
7. 把当日存档 commit 回仓库，`history/` 里能直接翻历史日报

### 常见调整

| 需求 | 改哪里 |
| --- | --- |
| 改发送时间 | `daily-news.yml` 的 `cron`（记得 UTC 换算） |
| 不想让 Actions 回写存档 | 删掉最后一步 `提交当日存档回仓库` |
| Actions 里跑单元测试太慢 | 删掉 `跑单元测试` 那一步 |
| 失败时想收到通知 | Settings → Notifications，或在 workflow 结尾加邮件通知 |
