"""部署补丁 C: H5 侧对接 A/B —— 记住桥给的 `variant` 并显示在链路自检里。

为什么 H5 也要动
--------------
* 用户要求"前端 H5 有地方调用接口来使用模型, 完成对接";
* 桥已经在 `/init` 返回 `variant`, 但前端没接 ⇒ 状态栏/报障看不到本局落在哪个臂,
  出问题时无法判断"这一局到底开没开顶牌规则";
* 记录本身由**桥**落盘（16 张不走 /replays/save, 见补丁 B）, 所以这里只做
  **状态对接 + 可观测**, 不引入第二套写入。

安全: JS 无法 ast 校验 ⇒ 先写临时文件, 用 `node --check`(只解析不执行) 过了再落盘。
"""
from pathlib import Path
import shutil
import subprocess
import sys

APP = Path('app.js')
s = APP.read_text(encoding='utf-8')
if 'bridge.variant' in s:
    print('已打过 H5 补丁, 跳过')
    raise SystemExit(0)


def sub_unique(text, anchor, new, what):
    n = text.count(anchor)
    assert n == 1, f'锚点[{what}]出现 {n} 次（必须 1 次）——先核对文件版本'
    return text.replace(anchor, new, 1)


# 1) 状态对象加字段
a1 = "no: null, file: null, lastErr: '', _syncTask: null };"
s = sub_unique(s, a1, "no: null, file: null, variant: null, lastErr: '', _syncTask: null };",
               'bridge 状态对象')

# 2) 开局 init 时记住臂
a2 = "      bridge.no = r.no || null; bridge.file = r.file || null;   // 本局编号（复盘当局用）"
s = sub_unique(s, a2, a2 + "\n      bridge.variant = r.variant || bridge.variant || null;"
                         "   // A/B 臂（桥按局号哈希分流; A=现行为, B=开顶牌规则）",
               'bridgeNewRound 赋值')

# 3) 掉线重连重放后同样记住（重连会 reuse_no, 臂应一致）
a3 = "      bridge.no = r.no || bridge.no; bridge.file = r.file || bridge.file;"
s = sub_unique(s, a3, a3 + "\n      bridge.variant = r.variant || bridge.variant || null;"
                         "   // 重连后臂不变（按局号哈希）",
               '重连赋值')

# 4) 链路自检面板显示臂（报障时一眼可见）
a4 = "    bits.push(`<div class=\"st-line dim\">本次 rid：${esc((st && st.rid) || bridge.lastRid || '-')}</div>`);"
s = sub_unique(s, a4, a4 + "\n    if (bridge.variant) {"
                         "\n      bits.push(`<div class=\"st-line dim\">AI 策略组：${esc(bridge.variant)}"
                         "（A=现行为, B=新规则; 由服务端按局号分流）</div>`);\n    }",
               '自检面板 rid 行')

# 临时文件必须以 .js 结尾 —— node 的 ESM 加载器会拒未知扩展名
# （踩过: 用 app.js.new 时 node --check 报 ERR_UNKNOWN_FILE_EXTENSION, 被 fail-safe 拦下）
tmp = APP.with_name('_ab_check_app.js')
tmp.write_text(s, encoding='utf-8', newline='\n')
node = shutil.which('node')
if node is None:
    if '--force' not in sys.argv:
        print('!! 本机没有 node, 无法语法校验 ⇒ 拒绝落盘（线上有 node, 那里会真校验）。'
              '仅本地核对替换结果可加 --force')
        tmp.unlink(missing_ok=True)
        sys.exit(2)
    print('  [warn] 无 node, 跳过语法校验（--force）')
else:
    r = subprocess.run([node, '--check', str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        print('!! node --check 未通过, 已放弃写入:\n', (r.stderr or r.stdout)[:400])
        tmp.unlink(missing_ok=True)
        sys.exit(1)
    print('  node --check 通过')
APP.write_text(s, encoding='utf-8', newline='\n')
tmp.unlink(missing_ok=True)
print('OK: app.js 已对接 variant')
