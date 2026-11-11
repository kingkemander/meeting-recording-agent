#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给已导出的会议写"内容主题"，并据此重命名知识库待上传文件。

知识库没有改名接口（文档名 = 上传文件名），所以必须在**上传前**把名字定好。
本脚本只写 `meeting-exports/<目录>/meta.json`，真正改名由 export_meetings.py 完成。

用法：
  # 看现有目录与当前主题
  python3 set_topic.py --list
  # 设定主题（主题会出现在知识库文档名里）
  python3 set_topic.py --folder 2026-10-14-phone-1047 --topic "职称评审材料填报培训"
  # 批量：从 JSON 文件读 {folder: topic}
  python3 set_topic.py --from-json topics.json

命名结果：MTG-<日期> <主题> · <源文件名>.md
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="meeting-exports")
    ap.add_argument("--folder")
    ap.add_argument("--topic")
    ap.add_argument("--from-json")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    root = args.root
    if not os.path.isdir(root):
        print(json.dumps({"ok": False, "message": f"目录不存在：{root}"}, ensure_ascii=False))
        return 1

    if args.list:
        rows = []
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if not os.path.isdir(p):
                continue
            meta = {}
            mp = os.path.join(p, "meta.json")
            if os.path.exists(mp):
                meta = json.load(open(mp, encoding="utf-8"))
            rows.append({"folder": d, "topic": meta.get("topic"), "source": meta.get("source_title")})
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    mapping = {}
    if args.from_json:
        mapping.update(json.load(open(args.from_json, encoding="utf-8")))
    if args.folder:
        if not args.topic:
            print(json.dumps({"ok": False, "message": "指定 --folder 时必须同时给 --topic"}))
            return 1
        mapping[args.folder] = args.topic

    if not mapping:
        print(json.dumps({"ok": False, "message": "没有要设置的主题"}, ensure_ascii=False))
        return 1

    done = []
    for folder, topic in mapping.items():
        p = os.path.join(root, folder)
        if not os.path.isdir(p):
            print(json.dumps({"ok": False, "message": f"目录不存在：{p}"}, ensure_ascii=False))
            return 1
        mp = os.path.join(p, "meta.json")
        meta = {}
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp, encoding="utf-8"))
            except Exception:
                meta = {}
        # 从 transcript.md 头部取源标题（首次写入时）
        if not meta.get("source_title"):
            tp = os.path.join(p, "transcript.md")
            if os.path.exists(tp):
                for line in open(tp, encoding="utf-8"):
                    if line.startswith("# 转写原文 · "):
                        meta["source_title"] = line[len("# 转写原文 · "):].strip()
                        break
        meta["topic"] = topic.strip()
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        done.append({"folder": folder, "topic": meta["topic"]})

    print(json.dumps(
        {"ok": True, "updated": done,
         "next": "python3 export_meetings.py --force   # 重新生成 kb-upload/ 里的规范文件名"},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
