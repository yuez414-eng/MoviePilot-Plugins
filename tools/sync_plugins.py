# -*- coding: utf-8 -*-
"""把 plugins.v2/ 的源码同步到 plugins/（v1 兼容目录），或反向校验一致性。

MoviePilot 取包规则（app/helper/plugin.py::__async_get_file_list）：
    contents/plugins{'.' + package_version}/{pid}/
即：v2 市场从 plugins.v2/ 拉，v1 兜底从 plugins/ 拉。
两个目录内容必须一致，改完一侧记得跑本脚本。
"""
import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V2 = os.path.join(ROOT, "plugins.v2")
V1 = os.path.join(ROOT, "plugins")


def digest(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def walk(base):
    out = []
    for r, d, fs in os.walk(base):
        d[:] = [x for x in d if x != "__pycache__"]
        for f in fs:
            if f.endswith((".pyc", ".pyo")):
                continue
            out.append(os.path.relpath(os.path.join(r, f), base))
    return sorted(out)


def main():
    check_only = "--check" in sys.argv
    diffs = []
    for rel in set(walk(V2)) | set(walk(V1)):
        a, b = os.path.join(V2, rel), os.path.join(V1, rel)
        if not os.path.exists(a):
            diffs.append(("only-in-plugins", rel)); continue
        if not os.path.exists(b):
            diffs.append(("only-in-plugins.v2", rel)); continue
        if digest(a) != digest(b):
            diffs.append(("content-differs", rel))

    if diffs:
        print("发现 %d 处不一致：" % len(diffs))
        for kind, rel in diffs:
            print("   [%s] %s" % (kind, rel))
        if check_only:
            sys.exit(1)
    else:
        print("两个目录完全一致 ✅")
        if check_only:
            return

    if check_only:
        return

    # 以 plugins.v2 为准，镜像到 plugins
    for rel in walk(V2):
        s, d = os.path.join(V2, rel), os.path.join(V1, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)
        print("   copied  %s" % rel)
    print("已把 plugins.v2/ 同步到 plugins/")


if __name__ == "__main__":
    main()
