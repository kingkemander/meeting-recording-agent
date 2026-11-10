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

目录 `meeting-exports/<YYYY-MM-DD>-<slug>/` 下生成：

| 文件 | 内容 |
| --- | --- |
| `recording.<ext>` | 原版录音（第 4 步成功时才有） |
| `transcript.md` | 转写原文，按 `speaker_segments` 排序，格式 `[mm:ss] 发言人N：文本`；没有分段就用 `full_text`；两者都空则写明后端状态 |
| `summary.md` | 元信息（标题、时间、时长、来源、状态、meetingId、是否同空间共享）+ AI 摘要 + 待办 + 关键词 |
| `meeting-notes.md` | 面向知识库的合并稿：元信息 + 摘要 + 待办 + 完整转写 |

文件名英文、内容中文，便于知识库检索。空转写/无摘要要在文件里显式标注，不要留白让人误会。

## 6. 上传到企业知识库

使用桌面端内置的 **企业知识库（SpaceKB）** 扩展：

1. 先 `spacekb-knowledge-base.status` 确认已配置、已连通。未配置时提示用户到「设置 → 扩展 → 企业知识库」补齐，不要自己去请求知识库接口。
2. `spacekb-knowledge-base.batch_upload_documents`，`filePaths` 传第 5 步的文件：

```json
{
  "filePaths": [
    "meeting-exports/2026-11-01-产品周会/summary.md",
    "meeting-exports/2026-11-01-产品周会/transcript.md",
    "meeting-exports/2026-11-01-产品周会/meeting-notes.md"
  ]
}
```

3. 默认**只传 Markdown**，不传音频：多数知识库不索引音频，大文件白占空间。用户明确要求才加 `recording.<ext>`。
4. 知识库拒绝音频格式时，去掉音频只传三个 Markdown，并告知用户音频未入库、本地已保留。
5. 上传失败保留原始错误，不重试超过一次。

## 7. 收尾报告

一张简表说清：

- 导出了哪几场会议、本地目录在哪
- 录音是否拉取成功（失败要说明原因）
- 哪些文件已上传知识库、哪些跳过或失败
- 哪些会议**没有转写 / 没有 AI 总结**，建议用户重新上传或稍后再导

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
