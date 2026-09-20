"""桥接侧 trick 语义防御验收（在训练机上、以 pdk15 引擎为 BOT_ROOT 运行）。"""
import sys
sys.path.insert(0, '.')
import ai_bridge as AB

print('BOT_ROOT =', AB.BOT_ROOT)
H15 = [20, 21, 24, 25, 28, 29, 32, 33, 0, 1, 4, 5, 8, 9, 12]

def show(name, fn):
    try:
        print('  %-26s OK   %s' % (name, fn()))
    except Exception as e:
        print('  %-26s %s: %s' % (name, type(e).__name__, str(e)[:120]))

show('null trick', lambda: AB._check_trick(None))
show('合法连对 [2,2,2,0]', lambda: AB._check_trick([2, 2, 2, 0]))
show('连对 len=张数 [2,2,4,0]', lambda: AB._check_trick([2, 2, 4, 0]))
show('飞机 len=张数 [6,1,6,0]', lambda: AB._check_trick([6, 1, 6, 0]))
show('炸弹 [8,2,4,1]', lambda: AB._check_trick([8, 2, 4, 1]))
show('四带三(默认关) [9,2,7,0]', lambda: AB._check_trick([9, 2, 7, 0]))
show('四带三(four3 开)', lambda: AB._check_trick([9, 2, 7, 0], AB.build_cfg({'four3': True})))
show('形状错 [1,2]', lambda: AB._check_trick([1, 2]))
show('越界 [2,2,99,0]', lambda: AB._check_trick([2, 2, 99, 0]))

print('--- 整条 decide 链路 ---')
show('decide 非法 trick', lambda: AB.do_decide(
    {"my_hand": H15, "opp_n": 11, "trick": [2, 2, 4, 0], "history": []})[1])
out, code = AB.do_decide({"my_hand": H15, "opp_n": 11, "trick": [2, 2, 2, 0],
                          "history": []})
mv = out.get("move") or []
print('  decide 合法连对: HTTP=%s move=%s legal_count=%s' % (code, mv, out.get('legal_count')))
assert code == 200 and len(mv) == 4, '合法连对跟牌应为 4 张同长解'
print('全部通过')
