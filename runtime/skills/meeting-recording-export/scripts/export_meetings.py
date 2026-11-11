#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把企业空间会议归档到本地，并生成可直接上传知识库的单文件纪要。

依赖：den_auth.py（同目录），无第三方库。

用法：
  python3 export_meetings.py                    # 导出全部（已存在则跳过）
  python3 export_meetings.py --limit 3          # 最近 3 条
  python3 export_meetings.py --ids mtr_xxx,mtr_yyy
  python3 export_meetings.py --since 2026-10-01 # 该日期之后上传的
  python3 export_meetings.py --force            # 覆盖已存在目录
  python3 export_meetings.py --no-audio         # 不下载音频

产出：
  meeting-exports/<日期>-<slug>/{recording.*,transcript.md,summary.md,meeting-notes.md}
  kb-upload/<规范化文件名>.md        # 单文件纪要，文件名即知识库文档名
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import den_auth  # noqa: E402

SRC_LABEL = {"recording_card": "录音卡", "phone": "手机录音", "upload": "网页上传"}
SRC_SLUG = {"recording_card": "card", "phone": "phone", "upload": "upload"}


def log(msg):
    print(msg, flush=True)


def api_get(path, api_base=None):
    s = den_auth.load_session()
    if not s:
        raise SystemExit("本机没有登录态，请先运行 den_auth.py status 并按提示获取。")
    base = (api_base or s.get("api_base") or den_auth.DEFAULT_API_BASE).rstrip("/")
    st, _, _, raw = den_auth.http_request(
        "GET", base + path, headers={"cookie": den_auth.cookie_header(s.get("cookies") or [])}
    )
    if st != 200:
        raise SystemExit(f"接口 {path} 返回 HTTP {st}：{raw[:200].decode('utf-8', 'replace')}")
    return json.loads(raw)


def slug_time(created_at):
    """2026-11-09T11:19:30.000Z -> ('2026-11-09', '1119') 按本地时区"""
    try:
        ts = time.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S")
        epoch = time.mktime(ts)  # 视为本地时间，保持与用户看到的日期一致
        lt = time.localtime(epoch)
        return time.strftime("%Y-%m-%d", lt), time.strftime("%H%M", lt)
    except Exception:
        return created_at[:10], "0000"


def fmt_duration(ms):
    sec = int((ms or 0) / 1000)
    if sec <= 0:
        return "未知"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}小时{m}分{s}秒" if h else f"{m}分{s}秒"


def build_body(transcript):
    segs = (transcript or {}).get("speaker_segments") or []
    if segs:
        out = []
        for sg in sorted(segs, key=lambda x: x.get("beginMs") or 0):
            ms = sg.get("beginMs") or 0
            mm, ss = divmod(int(ms // 1000), 60)
            out.append(f"[{mm:02d}:{ss:02d}] 发言人{sg.get('speakerId')}：{sg.get('text', '').strip()}")
        return "\n\n".join(out)
    return ((transcript or {}).get("full_text") or "").strip()


def norm_item(x):
    """待办/关键词尽量取人话来展示，不要把原始 JSON 写进纪要。"""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        txt = x.get("content") or x.get("text") or x.get("title") or x.get("name")
        who = x.get("assignee") or x.get("owner") or x.get("due")
        if txt and who:
            return f"{txt}（{who}）"
        if txt:
            return txt
        return json.dumps(x, ensure_ascii=False)
    return str(x)


def export_one(meeting, detail, out_root, kb_root, force=False, audio=True):
    m = detail["meeting"]
    t = detail.get("transcript")
    s = detail.get("summary") or {}
    date, hhmm = slug_time(m.get("created_at") or "")
    src = m.get("source") or "upload"
    folder = f"{date}-{SRC_SLUG.get(src, src)}-{hhmm}"
    out = os.path.join(out_root, folder)
    if os.path.isdir(out) and not force and os.path.exists(os.path.join(out, "meeting-notes.md")):
        log(f"[跳过] 已存在：{folder}")
        return None

    os.makedirs(out, exist_ok=True)
    title = m.get("title") or "未命名会议"
    dur = fmt_duration(m.get("audio_duration_ms"))
    body = build_body(t)
    empty_tr = not body
    if empty_tr:
        body = f"_（该会议暂无转写内容，后端状态：{m.get('status')}）_"

    ai = s.get("summary")
    ai_txt = ai if isinstance(ai, str) else (json.dumps(ai, ensure_ascii=False) if ai else None)
    acts = [norm_item(a) for a in (s.get("action_items") or [])]
    kws = [norm_item(k) for k in (s.get("keywords") or [])]

    meta = (
        f"| 项 | 值 |\n| --- | --- |\n"
        f"| 标题 | {title} |\n"
        f"| 上传时间（UTC） | {m.get('created_at')} |\n"
        f"| 时长 | {dur} |\n"
        f"| 来源 | {SRC_LABEL.get(src, src)} |\n"
        f"| 状态 | {m.get('status')} |\n"
        f"| meetingId | `{m.get('id')}` |\n"
        f"| 同空间共享 | {'是' if m.get('shared_with_org') else '否'} |\n"
    )

    # transcript.md
    tl = []
    if (t or {}).get("speaker_segments"):
        tl = [f"[{int((sg.get('beginMs') or 0)//1000)//60:02d}:{int((sg.get('beginMs') or 0)//1000)%60:02d}] 发言人{sg.get('speakerId')}：{sg.get('text','').strip()}"
              for sg in sorted(t["speaker_segments"], key=lambda x: x.get("beginMs") or 0)]
        tr_body = "\n\n".join(tl)
    else:
        tr_body = body
    with open(os.path.join(out, "transcript.md"), "w", encoding="utf-8") as f:
        f.write(f"# 转写原文 · {title}\n\n{meta}\n---\n\n{tr_body}\n")

    # summary.md
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# 会议摘要 · {title}\n\n{meta}\n## AI 摘要\n\n"
                + (ai_txt or f"_（暂无 AI 总结，后端状态：{m.get('status')}）_")
                + "\n\n## 待办清单\n\n" + ("\n".join(f"- {a}" for a in acts) or "_（未提取到待办）_")
                + "\n\n## 关键词\n\n" + ("、".join(kws) or "_（无）_") + "\n")

    # meeting-notes.md（单文件合并稿）
    notes = (
        f"# 会议纪要 · {title}\n\n{meta}\n"
        f"## 一、摘要\n\n{ai_txt or f'_（暂无 AI 总结，转写原文见下）_'}\n\n"
        f"## 二、待办\n\n" + ("\n".join(f"- {a}" for a in acts) or "_（未提取到待办）_") + "\n\n"
        f"## 三、关键词\n\n" + ("、".join(kws) or "_（无）_") + "\n\n"
        f"## 四、完整转写\n\n{body}\n"
    )
    with open(os.path.join(out, "meeting-notes.md"), "w", encoding="utf-8") as f:
        f.write(notes)

    # 音频
    audio_ok = None
    if audio:
        ext = m.get("audio_format") or "wav"
        dst = os.path.join(out, f"recording.{ext}")
        declared = m.get("audio_size_bytes")
        if os.path.exists(dst) and declared and abs(os.path.getsize(dst) - declared) < 1024:
            audio_ok = (os.path.getsize(dst), declared, True)
        elif os.path.exists(dst) and not declared and os.path.getsize(dst) > 0:
            audio_ok = (os.path.getsize(dst), None, True)
        else:
            try:
                urllib.request.urlretrieve(m["audio_url"], dst)
                size = os.path.getsize(dst)
                audio_ok = (size, declared, declared is None or abs(size - declared) < 1024)
            except Exception as e:
                audio_ok = (None, declared, False)
                log(f"  [警告] 音频下载失败：{e}")

    # 知识库单文件：文件名即文档名
    os.makedirs(kb_root, exist_ok=True)
    topic = ""
    meta_path = os.path.join(out, "meta.json")
    if os.path.exists(meta_path):
        try:
            topic = (json.load(open(meta_path, encoding="utf-8")) or {}).get("topic") or ""
        except Exception:
            topic = ""
    src_name = re.sub(r'[\\/:*?"<>|]', "-", (m.get("title") or "").strip())
    label = topic.strip() or f"{SRC_LABEL.get(src, src)}{hhmm}"
    kb_name = f"MTG-{date} {label} · {src_name}.md" if src_name else f"MTG-{date} {label}.md"
    kb_path = os.path.join(kb_root, kb_name)
    with open(kb_path, "w", encoding="utf-8") as f:
        f.write(notes)

    # 音频同主名放一份到 kb-upload（硬链接，不复制；失败了再复制）
    kb_audio = None
    if audio:
        ext = m.get("audio_format") or "wav"
        src_audio = os.path.join(out, f"recording.{ext}")
        if os.path.exists(src_audio):
            kb_audio = os.path.splitext(kb_path)[0] + f".{ext}"
            if os.path.exists(kb_audio):
                try:
                    os.remove(kb_audio)
                except OSError:
                    pass
            try:
                os.link(src_audio, kb_audio)
            except OSError:
                try:
                    import shutil as _sh
                    _sh.copy2(src_audio, kb_audio)
                except Exception as e:
                    kb_audio = None
                    log(f"  [警告] 音频未放入 kb-upload：{e}")

    log(f"[完成] {folder}  转写 {len(body)} 字  音频 {audio_ok}  知识库名 {kb_name}")
    return {
        "folder": folder,
        "kb_file": os.path.relpath(kb_path, os.path.dirname(out_root) or "."),
        "kb_name": kb_name,
        "kb_audio": os.path.basename(kb_audio) if kb_audio else None,
        "transcript_chars": len(body),
        "has_summary": bool(ai_txt),
        "empty_transcript": empty_tr,
        "audio": audio_ok,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="meeting-exports", help="归档根目录（相对当前目录）")
    ap.add_argument("--kb-staging", default="kb-upload", help="知识库待上传目录")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--since", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 结果，便于自动任务解析")
    args = ap.parse_args()

    data = api_get("/v1/meetings?scope=mine&limit=50")
    meetings = data.get("meetings") or []
    if args.ids:
        want = {x.strip() for x in args.ids.split(",") if x.strip()}
        meetings = [m for m in meetings if m["id"] in want]
    if args.since:
        meetings = [m for m in meetings if (m.get("created_at") or "") >= args.since]
    meetings.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    if args.limit:
        meetings = meetings[: args.limit]

    results, skipped = [], []
    for m in meetings:
        detail = api_get(f"/v1/meetings/{m['id']}")
        r = export_one(detail["meeting"], detail, args.out, args.kb_staging, args.force, not args.no_audio)
        if r:
            results.append(r)
        else:
            skipped.append(m["id"])

    if args.json:
        print(json.dumps({"exported": results, "skipped": skipped}, ensure_ascii=False, indent=2))
    else:
        log("")
        log(f"共 {len(meetings)} 条，导出 {len(results)} 条，跳过 {len(skipped)} 条。")
        if results:
            log(f"归档目录：{os.path.abspath(args.out)}")
            log(f"知识库待上传：{os.path.abspath(args.kb_staging)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
