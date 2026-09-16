#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 package.json / package.v2.json 里的 <GITHUB_USER> 占位符替换成你的 GitHub 用户名。

因为插件图标用的是绝对 raw 地址（前端会把裸文件名解析到公共图标库，
自建仓库必须写全路径），所以仓库创建后需要把用户名填进去。

用法：
    python tools/set_owner.py <你的GitHub用户名>
    python tools/set_owner.py michaelzhang
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "<GITHUB_USER>"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    user = sys.argv[1].strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", user):
        print(f"看起来不像合法的 GitHub 用户名：{user!r}")
        return 2

    changed = 0
    for name in ("package.v2.json", "package.json"):
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if PLACEHOLDER not in text:
            print(f"{name}: 没有占位符，跳过")
            continue
        text = text.replace(PLACEHOLDER, user)
        # 校验仍是合法 JSON
        json.loads(text)
        path.write_text(text, encoding="utf-8")
        changed += 1
        print(f"{name}: 已替换为 {user}")

    if changed:
        print("\n完成。别忘了同时确认插件目录名（必须是小写插件ID）：plugins/hardlinkverify/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
