---
name: meeting-recording-export
description: 把企业空间会议助手（录音卡 / 手机录音）里当前账号上传的录音、转写和 AI 总结拉到电脑本地，整理成会议归档并上传到企业知识库。当用户要求「导出/下载/拉取会议录音」「把手机上的会议同步到电脑」「把会议内容放进知识库」时使用。
---

# 会议录音导出到知识库

本 Skill 只调用企业空间现有接口和桌面端现有知识库扩展，不改软件代码。

**关键改进**：不再要求用户去浏览器 F12 手工复制 Cookie。登录态由 `scripts/den_auth.py` 统一获取、校验、加密落盘（600 权限），有效期约 180 天，过期前自动可用。

---

## 0. 准备凭证（三条路径，按顺序尝试）

先统一确认接口地址：

```bash
# 公有云默认；私有化部署换成 http(s)://<企业空间域名>/api/den
export DEN_API_BASE="${DEN_API_BASE:-https://cloud.spaceagents.cn/api/den}"
```

然后**永远先跑一次体检**，不要一上来就问用户要密码：

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py status
```

- `{"ok": true, ...}` → 登录态可用，直接进第 1 步。
- `{"ok": false, ...}` → 按下面三条路径依次尝试，**找到一条能用的就停**。

### 路径 A：复用本机浏览器里已有的登录态（首选，零交互）

客户电脑上如果他平时用 Chrome / Edge / Brave 登录过企业空间，登录态就在浏览器 cookie 库里，直接复用：

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py import-browser
# 指定浏览器：--browser chrome|edge|brave
```

- macOS 首次会弹系统钥匙串授权（"security 想要访问 Chrome Safe Storage"），**必须提前告诉用户去点允许**，否则命令会卡住。脚本超时 120 秒。
- Windows 走 DPAPI 自动解密，Linux 走默认密钥，一般无需交互。
- 成功后输出形如：`已复用 chrome 的登录态。账号：xxx@yy.com / 空间：凹凸时空 / 角色：admin。有效期：2027-05-09（剩余 180.0 天）`。

### 路径 B：让用户在内置浏览器登录，脚本自动抓取（推荐给客户）

用户不需要复制任何东西，只要在右侧浏览器面板里自己输一次账号密码：

1. 用 `openwork_browser_open_url` 打开 `${DEN_API_BASE 去掉 /api/den}`（如 `https://cloud.spaceagents.cn`），拿到 `browser_url` 和 `target_id`。
2. 告诉用户：**"请在右侧浏览器面板里用自己的账号登录，登录进到工作台后跟我说一声。"**
3. 用户说好了之后，先确认已登录（同源请求，cookie 自动带上）：

```javascript
// browser_eval
(async () => {
  const r = await fetch('/api/den/v1/me', { credentials: 'include', headers: { accept: 'application/json' } });
  return { status: r.status };
})()
```

`200` 才继续；`401` 说明还没登录成功，让用户再确认一下。

4. 抓取并保存登录态：

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py import-cdp --port <browser_url 的端口，如 9223>
```

脚本通过 CDP 取 cookie、校验 `/v1/meetings` 后写入 `~/.spaceagents/den-session.json`。
**全程不接触用户密码，不产生任何需要用户复制的文本。**

### 路径 C：用户在对话里给账号密码（最后手段）

客户电脑既没有浏览器登录态、也不方便用内置浏览器时，才用这条。**必须先取得用户明确同意，并说明两点风险**：

> 1. 密码会出现在本次对话记录里，建议事后改一次密码，或改用路径 B。
> 2. 我只用它调一次登录接口换登录态，不会保存密码、不会回显、不写进任何文件。

引导话术示例：

> "我可以用你的账号密码直接换一个有效期约 180 天的登录态，之后就不用再登了。但密码会留在这次对话记录里，如果你不方便，我们改用内置浏览器登录（我全程看不到密码）。你选哪种？"

用户同意后，**用标准输入传密码，绝不写进命令行参数**：

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py login \
  --email "user@company.com" --password-stdin
# 密码从 stdin 读入：可以 printf '%s' "$PW" | ... ，或由交互式输入
```

成功会输出账号、空间、角色和有效期。**不要在回复里重复密码**，也不要把它写进任何脚本或笔记。

### 兼容：手工 cookie（旧流程，不主动推荐）

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py import-cookie --cookie "name=value; name2=value2"
```

### 登录态存放与失效

| 项 | 说明 |
| --- | --- |
| 文件 | `~/.spaceagents/den-session.json`（权限 600，可用 `DEN_SESSION_FILE` 覆盖） |
| 内容 | 接口地址、来源方式、cookie、服务端 `session.expiresAt` |
| 有效期 | 企业空间 session 约 180 天；`status` 会显示剩余天数，<7 天会提示重登 |
| 失效 | 任意命令返回 401 时，重新走 A/B/C 任一条即可，不需要用户重新学流程 |
| 退出 | `den_auth.py logout` 删除本机登录态 |

> 用**上传录音的那个账号**取登录态。会议接口按 `scope=mine` 过滤，用别人的登录态会拉到别人的会议。

---

## 1. 拉取我的会议列表

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py get "/v1/meetings?scope=mine&limit=50"
```

返回：

```json
{
  "meetings": [
    {
      "id": "meetingRecord_xxx",
      "title": "产品周会",
      "audio_url": "https://...",
      "audio_format": "m4a",
      "audio_size_bytes": 12345678,
      "audio_duration_ms": 1800000,
      "status": "done",
      "source": "recording_card",
      "shared_with_org": false,
      "created_at": "2026-11-01T09:00:00.000Z"
    }
  ],
  "nextCursor": null
}
```

- `scope=mine` 只返回当前登录账号上传的会议，对应「谁的账号上传的」。
- 可选 query：`source=recording_card|phone|upload`、`status=done|transcribed|...`、`cursor`。
- `source`：`recording_card` 录音卡，`phone` 手机录音机，`upload` 网页上传。

## 2. 选定会议

- 用户给了标题 / 日期 / 来源就按条件匹配；匹配多条就列「日期 · 标题 · 来源 · 状态 · 时长」让用户选，不要自己挑。
- 「最近一次」取 `created_at` 最新的一条。
- 用户要多条时循环处理，每条各自归档。

## 3. 拉取详情（转写 + 总结）

```bash
python3 .opencode/skills/meeting-recording-export/scripts/den_auth.py get "/v1/meetings/<meetingId>"
```

返回 `meeting`、`transcript`（`full_text`、`speaker_segments`）、`summary`（`summary`、`action_items`、`keywords`）。

`transcript` 或 `summary` 为 `null`，或 `full_text` 为空字符串时，表示后端还在处理（或该录音本身是空文件）：记录 `meeting.status` 并告知用户，**已完成的部分照常导出，不要循环轮询，也不要编造摘要**。

## 4. 下载原版录音

直接用详情里的 `meeting.audio_url`：

```bash
curl -fIL "$meeting.audio_url"    # 先确认可访问
mkdir -p "meeting-exports/<YYYY-MM-DD>-<slug>"
curl -fL --retry 2 -o "meeting-exports/<YYYY-MM-DD>-<slug>/recording.<ext>" "$meeting.audio_url"
ls -lh "meeting-exports/<YYYY-MM-DD>-<slug>/recording.<ext>"
```

- 扩展名按 `meeting.audio_format`；日期取 `created_at`；`<slug>` 取标题（去特殊字符，最长 40 字符）。
- 校验文件大小与 `audio_size_bytes` 一致。偏小就重下一次；仍失败如实报告，不要静默继续。
- **`audio_url` 返回 403**：说明该录音是手机直传到私有 OSS 的对象，接口只返回不带签名的地址，也没有其它下载接口。明确告诉用户「该录音需要服务端签发下载链接，当前版本没有这个接口」，跳过音频、继续导出转写和总结，不要伪造成功。

## 5. 生成本地归档

```bash
# 全部（已存在则跳过；--json 给自动任务用）
python3 .opencode/skills/meeting-recording-export/scripts/export_meetings.py
python3 .../export_meetings.py --limit 3
python3 .../export_meetings.py --ids mtr_xxx,mtr_yyy
python3 .../export_meetings.py --since 2026-10-01
python3 .../export_meetings.py --force        # 重做（音频已存在不会重下）
python3 .../export_meetings.py --no-audio
```

产出：

```
meeting-exports/<日期>-<来源>-<HHMM>/
  recording.<ext>   原版录音（第 4 步成功时才有）
  transcript.md     带 [mm:ss] 时间轴的转写；空转写写明后端状态
  summary.md        元信息 + AI 摘要 + 待办 + 关键词
  meeting-notes.md  合并稿（面向知识库）
  meta.json         内容主题（第 5.5 步写入，决定知识库文档名）
kb-upload/          可直接上传知识库的成品：<同名>.md（纪要）+ <同名>.wav（录音，硬链接不占双份）
```

空转写 / 无 AI 总结必须在文件里显式标注，不要留白让人误会，也不许编造。

## 5.5 命名：先读懂内容，再起名字（**强制步骤**）

知识库文档**没有改名接口**，名字只能在上传那一刻定死。所以**导出后、上传前必须先命名**。

### 规则

```
MTG-<YYYY-MM-DD> <内容主题> · <源文件名>.md
```

- `MTG-` 前缀：让所有会议纪要在知识库列表里聚成一类，按日期天然排序（沿用该库 `<编号> <名称>.md` 的风格）。
- `<YYYY-MM-DD>`：**本地日期**，与用户看到的会议时间一致。
- `<内容主题>`：**必须是读出来的内容，不是设备名**。8–20 字，说清"跟谁、谈什么"，例如
  `职称评审材料填报培训`、`录音卡定制与合作模式沟通`、`与XX客户谈二期方案与报价`。
  **禁止** `手机录音1047`、`录音1`、`会议纪要` 这种无语义命名。
- ` · `（空格+中点+空格）分隔主题与来源。
- `<源文件名>`：平台侧原始标题（如 `手机录音 10-14 10:47`、`call20261019-102656.opus`），
  便于回溯到原始录音。文件名里的 `:` 会被替换成 `-`（Windows 友好）。
- 空录音 / 无转写：主题写 `空录音（无转写）`，照样上传（用户明确要求保留记录）。

### 操作

```bash
# 1. 读转写，提炼主题（自己读懂，不要猜）
python3 .../set_topic.py --list                       # 看有哪些目录、现有主题
python3 .../set_topic.py --folder 2026-10-14-phone-1047 --topic "职称评审材料填报培训"
python3 .../set_topic.py --from-json topics.json      # 批量

# 2. 重新生成 kb-upload/ 里的规范文件名（音频不会重下）
python3 .../export_meetings.py --force
```

提炼主题的做法：读 `meeting-exports/<目录>/transcript.md`（长会议先看开头、中段、结尾各 1000 字，
或直接读 `summary.md` 的 AI 摘要），抓住"谁 + 谈的什么业务"，用**用户自己的话**写，
不要写"关于……的会议"这类空话。

## 6. 上传到企业知识库

### 6.0 先校准知识库 ID（最容易踩的坑）

`SPACEKB_KB_ID` 里存的值可能已经失效，表现为 `get_knowledge_base` 返回「企业知识库不存在」、
`list_documents` 返回 `[]`。**不要据此认为知识库是空的。** 先校准：

```bash
python3 .opencode/skills/meeting-recording-export/scripts/upload_kb.py --list-kb
```

- 只有 1 个库 → 直接用它的 id。
- 多个库 → 列给用户选，不要自己挑。
- 校准后把正确 id 写回 `SPACEKB_KB_ID`（设置 → 环境变量），**需重启引擎才生效**；
  未重启前用 `--kb <id>` 直接指定即可。

### 6.1 上传权限自检（读写分离）

**「导出 Skill」拿到的 Key 往往只有读权限。** 自检：

```bash
# 读 200 正常；写返回 422（缺 file 字段）= 有写权限
curl -s -o /dev/null -w 'read %{http_code}\n' "$SPACEKB_BASE_URL/api/knowledge-bases/<kbId>/documents" -H "Authorization: Bearer $SPACEKB_API_KEY"
curl -s -o /dev/null -w 'write %{http_code}\n' -X POST "$SPACEKB_BASE_URL/api/knowledge-bases/<kbId>/documents" -H "Authorization: Bearer $SPACEKB_API_KEY"
```

- 写返回 **401 `无效的 API Key`** → Key 没有写权限。如实告诉用户「去 SpaceKB 给这个 Key 开上传权限，
  或换一个可写 Key」，**不要重试、不要伪造成功**。
- 写返回 **422** → 权限没问题，只是缺 `file` 字段，可以正常上传。

### 6.2 必须指定权限域（否则 400）

上传接口强制要求 `target_domain`，不传直接
`400 {"detail":"必须指定上传目标权限域"}`。可用域名：

```bash
python3 .../upload_kb.py --list-kb     # 同时列出知识库与权限域
```

选域原则：

| 会议内容 | 建议域 |
| --- | --- |
| 经营决策、培训、内部管理、跨部门沟通 | **总经办**（默认） |
| 明确属于某部门业务（招聘、采购、客户售后等） | 对应部门域 |
| 拿不准 / 涉及敏感人事或薪酬 | 先问用户，不要自己定 |

会议纪要用**总经办**作为默认；用户指定别的域就按用户的来。

### 6.3 上传（录音 + 纪要配对，脚本优先）

**默认把原始录音和纪要一起入库**，两者用同一个主名，便于配对与回溯：

```
MTG-2026-10-14 职称评审材料填报培训 · 手机录音 10-14 10-47.wav   ← 原始录音，先传
MTG-2026-10-14 职称评审材料填报培训 · 手机录音 10-14 10-47.md    ← 纪要正文，后传
```

理由：音频块只解析成 1 个块（不做文本检索，但可下载试听、可复核原话），文本检索靠纪要。
两个都传，既有可检索的文本、又能追到原声。只有用户明确只要文本时才加 `--no-audio`。

```bash
python3 .opencode/skills/meeting-recording-export/scripts/upload_kb.py \
  --domain 总经办 --dry-run      # 先看计划（音频会排在前面）
python3 .../upload_kb.py --domain 总经办
python3 .../upload_kb.py --domain 总经办 --no-audio   # 例外：只传纪要
```

音频由 `export_meetings.py` 自动放进 `kb-upload/`（同主名、硬链接，不占双份空间）。
大文件（>50 MB）上传后状态是 `processing`，需要等一会儿才变 `completed`，**不要重复上传**。

脚本会：校准 KB → 按文件名查重（已存在跳过，不制造重复）→ 逐个上传 → 汇报成功/失败/跳过。
该知识库历史上有大量同名重复文档（同一文件传了 3 份），**查重这步不能省**。

也可以用内置扩展 `spacekb-knowledge-base.batch_upload_documents`，但它不带权限域参数，
且 `status` 只检查 Key 是否存在（**不代表连得上**）；用扩展前先 `get_knowledge_base` 确认。

### 6.4 上传后核对格式

```bash
curl -s "$SPACEKB_BASE_URL/api/knowledge-bases/<kbId>/documents/<docId>/chunks" \
  -H "Authorization: Bearer $SPACEKB_API_KEY"
```

块策略是 `heading / 512 字符 / overlap 50`，所以纪要必须用 `##` 分节
（一、摘要 / 二、待办 / 三、关键词 / 四、完整转写），否则会被切成无标题碎片，召回质量差。
核对三点：`status=completed`、`chunk_count > 0`、第一块以 `# 会议纪要 · <主题>` 开头。

---

## 7. 收尾报告

一张简表说清：

- 导出了哪几场会议、本地目录在哪
- **每场会议给你起的名字**（`MTG-日期 主题 · 源文件`），主题是从哪句话看出来的——用户最可能改这一项
- 录音是否拉取成功（失败要说明原因）
- 上传到哪个权限域、成功/跳过/失败各几条
- 哪些会议**没有转写 / 没有 AI 总结**（这类我照样归档并上传，但会标明）

## 8. 全流程固化（每次都要走完这 6 步）

```
① 登录态   den_auth.py status        失败→按第 0 步三路径重取
② 导出     export_meetings.py        只导新增（已存在自动跳过）
③ 命名     set_topic.py + --force    读转写→提炼主题→写 meta.json→重生成文件名
④ 上传     upload_kb.py --domain X   查重→上传→按权限域归档
⑤ 校验     chunks 抽查               status=completed、chunk_count>0、首块标题正确
⑥ 汇报     一张表                    名字/目录/域/成败/缺转写
```

**顺序不能颠倒**：必须③在④之前，因为知识库没有改名接口，名字上传即定型；
②③可以合并成一次脚本循环，⑤⑥不可省——不校验就等于没上传成功。

### 8.1 自动化（定时任务）

前提两条，否则任务每天报同一个错：登录态有效、知识库 Key **有写权限**。

用 `spaceagents-automations.create_task` 建任务：

| 项 | 建议值 |
| --- | --- |
| 名称 | 会议纪要同步 |
| 频率 | 每天一次（如 09:00）或每周一 09:00 |
| 提示词 | 见下 |

```
执行会议纪要同步，严格按 .opencode/skills/meeting-recording-export/SKILL.md 的第 8 节走：
1. den_auth.py status；失效则按第 0 步三路径重取登录态。
2. export_meetings.py --limit 10 --json（已导出过的自动跳过）。
3. 对本次新增的会议：读 transcript.md 提炼内容主题（8-20字，说清跟谁谈什么），
   用 set_topic.py 写入，再 export_meetings.py --force 生成规范文件名。
4. upload_kb.py --domain 总经办 --dry-run 看计划，再去掉 --dry-run 上传；查重跳过已存在的。
5. 抽查 1 篇 chunks：status=completed、chunk_count>0、首块以「# 会议纪要 · 」开头。
6. 汇报：新增几场、每场的名字、上传成功/失败、哪些缺转写。
只报新增内容；任何一步失败原样报错，不要谎报成功。
```

要点：

- **幂等**靠 `meeting-exports/` 已存在即跳过，不要删历史目录。
- 不要写死日期，用 `--limit` / `--since`。
- 401 大概率是登录态失效或 Key 没写权限，直接提示用户，不要反复重试。
- 定时任务里**不要自动删除**知识库任何文档（删除只在用户明确要求时做）。

## 脚本清单

| 脚本 | 作用 |
| --- | --- |
| `den_auth.py` | 登录态：`status / login / import-browser / import-cdp / import-cookie / get / logout` |
| `export_meetings.py` | 导出+归档+生成 kb-upload：`--limit / --ids / --since / --force / --no-audio / --json` |
| `set_topic.py` | 写内容主题（决定知识库文档名）：`--list / --folder --topic / --from-json` |
| `upload_kb.py` | 上传（查重+权限域）：`--list-kb / --domain / --file / --dry-run / --kb` |

---

## 给客户电脑部署的检查清单

1. `python3 --version` 有 Python 3.8+（脚本零第三方依赖）。
2. `DEN_API_BASE` 若为私有化部署，先设好再执行任何命令。
3. `status` 返回 `ok: false` 时，先问用户一句：「你平时用 Chrome/Edge 登录企业空间吗？」——用就走路 A；不用就走路 B（内置浏览器），不要上来就要密码。
4. 路径 A 在 macOS 会弹钥匙串授权，**提前提醒用户点允许**，否则会以为卡死。
5. 一次成功后可长期复用；`status` 显示剩余 <7 天时提醒重登。

## 权限与边界

- 只读 `scope=mine` 的会议，不导出别人的录音；不调用删除、修改、共享等写接口。
- 密码只经标准输入传给登录接口，不落盘、不入日志、不进知识库、不在回复里回显。
- 登录态文件权限 600，只在请求头里使用；不复制到工作区、不写进任何 md/日志。
- 不把 `den-session.json` 同步到网盘或知识库。
