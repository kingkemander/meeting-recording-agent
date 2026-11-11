#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 kb-upload/ 里的会议纪要上传到 SpaceKB 企业知识库（幂等、按文件名查重）。

用法：
  python3 upload_kb.py --list-kb                       # 看有哪些库和权限域
  python3 upload_kb.py --domain 总经办                 # 上传 kb-upload/*.md
  python3 upload_kb.py --domain 总经办 --file a.md     # 只传一个
  python3 upload_kb.py --domain 总经办 --dry-run       # 只列计划不写
  python3 upload_kb.py --kb <kbId> --domain <域>       # 指定知识库

环境变量：SPACEKB_API_KEY（必需）、SPACEKB_KB_ID（可选）、SPACEKB_BASE_URL（可选）
"""

import argparse
import glob
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("SPACEKB_BASE_URL", "https://knowledgedb.spaceagents.cn").rstrip("/")
API_KEY = os.environ.get("SPACEKB_API_KEY", "").strip()
KB_ENV = os.environ.get("SPACEKB_KB_ID", "").strip()


def req(path, method="GET", body=None, headers=None, timeout=120):
    r = urllib.request.Request(BASE + path, data=body, method=method)
    r.add_header("Authorization", f"Bearer {API_KEY}")
    r.add_header("User-Agent", "SpaceAgents/1.0")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jget(path):
    st, raw = req(path)
    if st != 200:
        raise SystemExit(f"GET {path} -> HTTP {st}: {raw[:200].decode('utf-8', 'replace')}")
    return json.loads(raw)


def multipart(file_path, fields):
    boundary = "----SpaceAgentsKb" + uuid.uuid4().hex
    parts = []
    for k, v in (fields or {}).items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )
    name = os.path.basename(file_path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
         f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    )
    parts.append(open(file_path, "rb").read())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default=None)
    ap.add_argument("--domain", default=os.environ.get("SPACEKB_DOMAIN", ""))
    ap.add_argument("--glob", default="kb-upload/*")
    ap.add_argument("--no-audio", action="store_true", help="只传 Markdown，不传录音")
    ap.add_argument("--file", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-kb", action="store_true")
    args = ap.parse_args()

    if not API_KEY:
        raise SystemExit("缺少 SPACEKB_API_KEY。")

    kbs = jget("/api/knowledge-bases")
    if args.list_kb:
        print("知识库：")
        for k in kbs:
            print(f"  {k['id']}  {k['name']}  (chunk {k.get('chunk_size')}/{k.get('chunk_overlap')} {k.get('chunking_strategy')})")
        st, raw = req("/api/domains")
        if st == 200:
            print("权限域：")
            for d in json.loads(raw):
                print(f"  {d['id']:>3}  {d['name']}")
        return 0

    kb_id = args.kb or KB_ENV
    ids = {k["id"] for k in kbs}
    if kb_id not in ids:
        raise SystemExit(
            "知识库 ID 无效：" + str(kb_id) + "\n可用的库：\n  "
            + "\n  ".join(f"{k['id']}  {k['name']}" for k in kbs)
            + "\n（用 --kb 指定，或修正环境变量 SPACEKB_KB_ID）"
        )

    if not args.domain:
        raise SystemExit("必须指定目标权限域：--domain <名称>（用 --list-kb 查看可选域）")

    AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac"}
    if args.file:
        files = [args.file]
    else:
        files = [f for f in sorted(glob.glob(args.glob)) if os.path.isfile(f)]
        if args.no_audio:
            files = [f for f in files if os.path.splitext(f)[1].lower() not in AUDIO_EXT]
        # 音频排前面：先入库原始录音，再入库纪要文本
        files.sort(key=lambda f: (0 if os.path.splitext(f)[1].lower() in AUDIO_EXT else 1, f))
    if not files:
        raise SystemExit(f"没有匹配到文件：{args.glob}")

    existing = {d.get("filename") for d in jget(f"/api/knowledge-bases/{kb_id}/documents")}
    todo, skip = [], []
    for f in files:
        (skip if os.path.basename(f) in existing else todo).append(f)

    print(f"知识库 {kb_id}｜权限域 {args.domain}｜待传 {len(todo)}｜已存在跳过 {len(skip)}")
    for f in skip:
        print(f"  [跳过] 已存在同名文档：{os.path.basename(f)}")
    if args.dry_run:
        for f in todo:
            print(f"  [计划] {os.path.basename(f)}")
        return 0

    ok, bad = [], []
    for f in todo:
        body, ctype = multipart(f, {"target_domain": args.domain})
        st, raw = req(f"/api/knowledge-bases/{kb_id}/documents", "POST", body, {"Content-Type": ctype})
        try:
            d = json.loads(raw)
        except Exception:
            d = {"raw": raw[:200].decode("utf-8", "replace")}
        if st in (200, 201):
            ok.append((os.path.basename(f), d.get("id"), d.get("status")))
            print(f"  [上传] {os.path.basename(f)} -> {d.get('id')} ({d.get('status')})")
        else:
            bad.append((os.path.basename(f), st, d))
            print(f"  [失败] {os.path.basename(f)} HTTP {st} {d}")

    print()
    print(f"完成：成功 {len(ok)}，失败 {len(bad)}，跳过 {len(skip)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
