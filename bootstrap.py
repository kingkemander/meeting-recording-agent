#!/usr/bin/env python3
"""会议纪要导出 · 通用引导器。

用法：
    python3 bootstrap.py --workspace "<工作区绝对路径>"

依赖：只用 Python 标准库，无需 pip install。
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

# ============ 需要修改 ============
REPO = "kingkemander/meeting-recording-agent"
VERSION = "v1.0.0"
ASSET = "runtime-v1.0.0.zip"
SHA256 = "f2a5aff5d0d810c7fd9a890a6b7443819a8eece6e5f945afcdadbeea62dffba2"
PLUGIN_ID = "meeting-recording-agent"
AGENT_NAME = "会议纪要导出"
# ==================================


def log(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="当前工作区绝对路径")
    args = ap.parse_args()

    ws = os.path.abspath(args.workspace)
    base = os.path.join(ws, ".spaceagents", "plugins", PLUGIN_ID)
    runtime = os.path.join(base, f"runtime-{VERSION}")
    pointer = os.path.join(base, "current.json")

    os.makedirs(base, exist_ok=True)

    # 幂等：已装好就不重复下载
    if os.path.isfile(pointer) and os.path.isdir(runtime):
        try:
            with open(pointer, encoding="utf-8") as f:
                info = json.load(f)
            if info.get("version") == VERSION:
                log(f"[跳过] 已安装 {VERSION}：{runtime}")
                log(f"[指针] {pointer}")
                return 0
        except Exception:
            pass  # 指针坏了就重装

    url = f"https://github.com/{REPO}/releases/download/{VERSION}/{ASSET}"

    log(f"[1/4] 下载 {url}")
    try:
        data = urllib.request.urlopen(url, timeout=180).read()
    except Exception as e:
        log(f"下载失败：{e}")
        log("排查：仓库是不是 Public？Release 是不是已 Publish？版本号对不对？")
        return 2

    log("[2/4] 校验 SHA-256")
    got = hashlib.sha256(data).hexdigest()
    if got.lower() != SHA256.lower():
        log("校验失败，已停止（文件可能不完整或被替换）")
        log(f"  期望：{SHA256}")
        log(f"  实际：{got}")
        return 3

    log("[3/4] 解压")
    if os.path.isdir(runtime):
        shutil.rmtree(runtime)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(runtime)

    log("[4/4] 写指针")
    info = {
        "version": VERSION,
        "runtime_root": runtime,
        "installed_at": int(time.time()),
        "source": url,
        "sha256": got,
    }
    with open(pointer, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    # 注册智能体
    tpl = os.path.join(runtime, "agent-template.md")
    if os.path.isfile(tpl):
        agents_dir = os.path.join(ws, ".opencode", "agents")
        os.makedirs(agents_dir, exist_ok=True)
        dst = os.path.join(agents_dir, f"{AGENT_NAME}.md")
        if os.path.exists(dst):
            log(f"[提示] 智能体已存在，未覆盖：{dst}")
        else:
            with open(tpl, encoding="utf-8") as f:
                body = f.read().replace("{{RUNTIME_ROOT}}", runtime)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(body)
            log(f"[完成] 智能体已注册：{dst}")
    else:
        log("[提示] zip 里没有 agent-template.md，跳过智能体注册")

    log("")
    log(f"安装完成：{AGENT_NAME} {VERSION}")
    log(f"运行时：{runtime}")
    log("下一步：新建一个会话，在智能体下拉里选择它")
    return 0


if __name__ == "__main__":
    sys.exit(main())
