#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_ban.py — 把「封号」变成系统可识别的故障（幂等、带断言）。
1) 新增 _note_ban()：上游 `20004 用户被封号` → 立即剔除该号（永久，不自动恢复）
2) _yb_post_json / 页内异常 / 流式 error 三处埋点
3) /admin/state：页内 frozen → 立刻落状态机（不再只显示不记账）
4) pool_state.record_health：冻结判定**优先于**"新号不计失败"
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "yuanbao_openai_proxy.py")
S = os.path.join(ROOT, "pool_state.py")

src = io.open(P, encoding="utf-8").read()
orig = src


def sub(label, old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    src = src.replace(old, new)
    print("  ok  %-40s (%d)" % (label, n))


# ---------------------------------------------------------------- 1. _note_ban 助手
sub("新增 _note_ban", '''def _yb_auth_error(result: dict):''',
    '''def _note_ban(*texts) -> bool:
    """识别上游「用户被封号」（错误码 20004）→ 立即把该号**剔除**。

    为什么用 eject 而不是 quarantine：
      冻结/封号是**账号级、确定性**的（页面上写着"您的账号有违规行为"，需人工申诉），
      不会因为再保活几次就恢复。留在隔离态只会让轮询反复打到死号上、日志刷错误。
      eject 后不参与轮询，也**不会被"连续成功自动恢复"翻回来**（人工 restore 才回池）。
    """
    blob = " ".join(str(t) for t in texts if t)
    if not blob:
        return False
    if not (("20004" in blob and "封号" in blob) or "用户被封号" in blob):
        return False
    if ACCOUNT is not None:
        try:
            ACCOUNT.apply("eject", "上游 20004 用户被封号（自动剔除，需申诉解封）", by="auto")
            print("[ban] 检测到 20004 用户被封号 → 已自动剔除该号", flush=True)
        except Exception:
            pass
    return True


def _yb_auth_error(result: dict):''')

# ---------------------------------------------------------------- 2a. 出站 HTTP 埋点
sub("_yb_post_json 埋点", '''    try:
        with _open(req, timeout=timeout) as r:
            return {"status": r.status, "text": r.read().decode("utf-8", "replace")}
    except _uerr.HTTPError as e:
        return {"status": e.code, "text": e.read().decode("utf-8", "replace")}''',
    '''    try:
        with _open(req, timeout=timeout) as r:
            _t = r.read().decode("utf-8", "replace")
            _note_ban(_t)
            return {"status": r.status, "text": _t}
    except _uerr.HTTPError as e:
        _t = e.read().decode("utf-8", "replace")
        _note_ban(_t)
        return {"status": e.code, "text": _t}''')

# ---------------------------------------------------------------- 2b. 流式 error 埋点
sub("流式 error 埋点", '''                        elif kind == "error":
                            err = ev["msg"]
                            retryable = True        # 200 包 error = 风控软拒，值得退避重试''',
    '''                        elif kind == "error":
                            err = ev["msg"]
                            # 封号是确定性失败，重试没意义（_note_ban 同时会剔除该号）
                            retryable = not _note_ban(ev["msg"])''')

# ---------------------------------------------------------------- 2c. 页内异常埋点（chat）
sub("chat 页内异常埋点", '''    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)''',
    '''    except Exception as e:
        import traceback
        traceback.print_exc()
        _note_ban(e)   # 页内报 "20004 用户被封号" 时立即剔除该号
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)''')

# ---------------------------------------------------------------- 2d. 页内异常埋点（images）
sub("images 页内异常埋点", '''    except Exception as e:
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)''',
    '''    except Exception as e:
        _note_ban(e)
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)''')

# ---------------------------------------------------------------- 3. frozen 落状态机
sub("/admin/state 冻结落账", '''    # 已登录但还没有过成功保活 → 立刻补一次，使该号尽快具备被轮询的资格
    if (st.get("logged_in") and not st.get("frozen") and ACCOUNT is not None''',
    '''    # 页内已显示"账号已冻结" ⇒ 立刻落状态机（隔离）。
    # 不能指望保活来发现：/api/info/general 对冻结号**仍可能返回 200**，
    # 冻结只在创建会话时才报 20004 —— 所以这里以页面事实为准。
    if st.get("frozen") and ACCOUNT is not None:
        _lh = ACCOUNT.get().get("last_health") or {}
        if not _lh.get("frozen"):
            try:
                ACCOUNT.record_health(False, None, "页内显示：账号已冻结（违规行为，需申诉）", True,
                                      count_failure=False)
                print("[pool_state] 检测到页内冻结 → 已隔离", flush=True)
            except Exception:
                pass
    # 已登录但还没有过成功保活 → 立刻补一次，使该号尽快具备被轮询的资格
    if (st.get("logged_in") and not st.get("frozen") and ACCOUNT is not None''')

assert src != orig
io.open(P, "w", encoding="utf-8").write(src)
print("\n写入:", P)

# ---------------------------------------------------------------- 4. pool_state：冻结优先
ps = io.open(S, encoding="utf-8").read()
p_orig = ps


def sub2(label, old, new, count=1):
    global ps
    n = ps.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    ps = ps.replace(old, new)
    print("  ok  %-40s (%d)" % (label, n))


sub2("冻结优先于新号豁免", '''            if ok:
                self._d["ever_ok"] = True
            elif (not count_failure) or (not self._d.get("ever_ok")):''',
     '''            # 🔴 冻结优先于"新号不计失败"：被冻结的号不管有没有成功过，都必须立刻隔离。
            # （否则 add-account 出来的新号第一次就被冻结会一直停留在 enabled）
            if frozen and self._d["state"] in ("enabled", "quarantined"):
                self._d.update({"state": "quarantined", "reason": "账号被冻结（自动隔离）",
                                "changed_at": int(time.time()), "changed_by": "auto"})
                self._log("quarantine", "账号被冻结", "auto")
                self._save()
                d = json.loads(json.dumps(self._d))
                d["label"] = LABELS.get(d["state"], d["state"])
                d["routable"] = d["state"] == "enabled"
                return d, "quarantine:frozen"

            if ok:
                self._d["ever_ok"] = True
            elif (not count_failure) or (not self._d.get("ever_ok")):''')

sub2("删除下方重复的冻结分支", '''            st = self._d["state"]
            # 1) 冻结 → 立即自动隔离
            if frozen and st in ("enabled", "quarantined"):
                self._d.update({"state": "quarantined", "reason": "账号被冻结（自动隔离）",
                                "changed_at": int(time.time()), "changed_by": "auto"})
                self._log("quarantine", "账号被冻结", "auto")
                act = "quarantine:frozen"
            # 2) 连续失败达阈值 → 自动隔离（只动 enabled，不覆盖手动 disabled/ejected）
            elif (not ok and st == "enabled"''',
     '''            st = self._d["state"]
            # 1) 冻结已在函数开头提前返回处理（见上方 frozen 分支）
            # 2) 连续失败达阈值 → 自动隔离（只动 enabled，不覆盖手动 disabled/ejected）
            if (not ok and st == "enabled"''')

assert ps != p_orig
io.open(S, "w", encoding="utf-8").write(ps)
print("写入:", S)
