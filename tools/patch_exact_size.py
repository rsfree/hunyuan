#!/usr/bin/env python3
"""补丁：4K/2K 输出尺寸精确化（CI 链式 cut）。

背景（2026-10-22 线上实测）：
  `imageMogr2/thumbnail/!3840x2160r` 是 **cover** 语义 ⇒ 上游 16:9 实际出 2240x1248
  （比例 1.7949，非 16/9）⇒ 放大后得到 **3877x2160**，比请求宽 37px。
  实测 `imageMogr2/thumbnail/!WxHr|imageMogr2/cut/WxH`（`|` 链式）能精确到 3840x2160；
  而 `/cut/` 直接挂在 thumbnail 后面会被**整体忽略**（返回原图，静默失败）。

风险控制：只有「请求比例 ≈ 上游槽位比例（≤2%）」时才裁，否则 cover 出来的图比例
本身就和请求差得远，硬裁会削掉主体（实测横图硬裁成 1080x1920 会丢掉左右大幅画面）。
"""
import ast
import sys

P = "yuanbao_openai_proxy.py"
src = open(P).read()
orig = src

# ---- 1) 新增 _exact_cut_ok() ----
anchor = 'def _ci_join(url: str, op: str) -> str:'
assert src.count(anchor) == 1, "锚点 _ci_join 不唯一"
NEW_FN = '''_CUT_TOL = 0.02        # 请求比例与上游槽位比例的允许偏差（超过就不裁，宁可尺寸略大）


def _exact_cut_ok(size: str) -> bool:
    """请求尺寸能否安全裁成「精确像素」。

    能裁的前提是上游给出的比例与请求比例足够接近：
    `_size_to_ratio()` 已把任意 WxH 映射到最近的槽位，所以两者最多差一点
    （实测上游 16:9 出的是 2240x1248 = 1.7949，比 16/9 宽 0.96%）。
    差距大时（如客户要 21:9、上游只能给 16:9）cover 后硬裁会削掉画面主体，
    此时保留 cover 结果并把差异写进 `upscale.info`，不假装拿到了精确尺寸。
    """
    import math
    if not size:
        return False
    m = re.match(r"^(\\d{2,4})x(\\d{2,4})$", size.strip().lower())
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return False
    slot = _size_to_ratio(size)
    if slot not in _RATIO_SLOTS:
        return False
    return abs(math.log((w / h) / _RATIO_SLOTS[slot])) <= math.log(1 + _CUT_TOL)


'''
src = src.replace(anchor, NEW_FN + anchor, 1)

# ---- 2) _maybe_upscale：算符改成链式 thumbnail|cut ----
old_head = '''    box = _size_to_target_box(size) if YB_UPSCALE_ENABLE else None
    if not box or not urls:
        return urls, None
    out, applied = [], 0
    for u in urls:
        if _ci_supported(u):
            out.append(_ci_join(u, "imageMogr2/thumbnail/!%dx%dr" % box))
            applied += 1'''
new_head = '''    box = _size_to_target_box(size) if YB_UPSCALE_ENABLE else None
    if not box or not urls:
        return urls, None
    exact = _exact_cut_ok(size)
    out, applied = [], 0
    for u in urls:
        if _ci_supported(u):
            op = "imageMogr2/thumbnail/!%dx%dr" % box
            if exact:
                # `|` 是腾讯 CI 的链式分隔符。写成 `thumbnail/!WxHr/cut/WxH`（用 `/`）
                # 会被整体忽略并静默返回原图 —— 2026-10-22 实测确认。
                op += "|imageMogr2/gravity/center/cut/%dx%d" % box
            out.append(_ci_join(u, op))
            applied += 1'''
assert src.count(old_head) == 1, "_maybe_upscale 头部不唯一"
src = src.replace(old_head, new_head, 1)

# ---- 3) info 增加 exact 标记 ----
old_info = '''    info = {"requested": size, "box": "%dx%d" % box,
            "upscaled": applied, "total": len(urls),
            "note": "图床等比插值放大（非 AI 超分）；不支持 CI 的图床原样返回"}'''
new_info = '''    info = {"requested": size, "box": "%dx%d" % box,
            "upscaled": applied, "total": len(urls), "exact": exact,
            "note": "图床等比插值放大（非 AI 超分）；不支持 CI 的图床原样返回"
                    + ("" if exact else "；请求比例与上游能力差 >2%，尺寸为 cover 结果、可能略大于请求值")}'''
assert src.count(old_info) == 1, "info 块不唯一"
src = src.replace(old_info, new_info, 1)

# ---- 4) 日志带出 exact ----
old_log = '''    print("[upscale] %s → %dx%d：%d/%d 张已放大" % (size, box[0], box[1], applied, len(urls)), flush=True)'''
new_log = '''    print("[upscale] %s → %dx%d（%s）：%d/%d 张已放大"
          % (size, box[0], box[1], "精确裁切" if exact else "cover 保留原比例", applied, len(urls)),
          flush=True)'''
assert src.count(old_log) == 1, "日志行不唯一"
src = src.replace(old_log, new_log, 1)

# ---- 语法校验后才落盘 ----
ast.parse(src)
assert src != orig
open(P, "w").write(src)
print("OK 已打补丁；新增字符数 =", len(src) - len(orig))
