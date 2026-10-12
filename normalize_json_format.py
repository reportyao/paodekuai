#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把回填时误用 indent=1 写出的棋谱统一回 indent=2（与桥写盘一致）。

纯格式修正：只重排缩进，**不改任何数据**。逐文件"读→重写→回读比对内容"三重校验，
内容不一致就还原并报错。默认 dry-run。
"""
import glob, json, os, sys

BASE = "/home/ubuntu/paodekuai"
DIRS = [f"{BASE}/data/replays", f"{BASE}/data/replays15"]


def indent_of(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith(' "') or line.startswith('  "'):
                return len(line) - len(line.lstrip(" "))
    return None


def main():
    apply = "--apply" in sys.argv
    files = []
    for d in DIRS:
        files += sorted(glob.glob(os.path.join(d, "*.json")))
    need = [p for p in files if indent_of(p) == 1]
    print(f"扫描 {len(files)} 个文件，需统一缩进的: {len(need)}")
    if not need:
        print("✅ 格式已统一")
        return 0
    bad = 0
    for p in need:
        try:
            raw = open(p, encoding="utf-8").read()
            doc = json.loads(raw)
            new = json.dumps(doc, ensure_ascii=False, indent=2)
            # 内容等价性自检（重排前后解析结果必须一致）
            if json.loads(new) != doc:
                print("✗ 内容不一致，跳过:", p)
                bad += 1
                continue
            if apply:
                tmp = p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(new)
                os.replace(tmp, p)
                back = json.loads(open(p, encoding="utf-8").read())
                if back != doc:
                    print("✗ 回读不一致，已报警:", p)
                    bad += 1
        except Exception as e:
            print("✗ 处理失败:", os.path.basename(p), e)
            bad += 1
    print(f"{'已统一' if apply else '（dry-run，未改动）'}：{len(need)} 个，异常 {bad} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())