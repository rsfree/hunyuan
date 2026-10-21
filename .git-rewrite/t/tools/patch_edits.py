"""补丁：新增 OpenAI 标准图生图入口 `/v1/images/edits`（multipart 上传）。

为什么补：
  此前只有 `/v1/images/generations`（JSON 里 `image` 放 URL/dataURI）。
  但 OpenAI 官方的图生图端点是 `/v1/images/edits`（multipart/form-data 上传文件），
  多数 SDK / 第三方客户端走这个 ⇒ 不补就整条图生图兼容不了。

做法：
  1. 把 `images_generations` 的核心逻辑抽成 `_images_core(req, body, image_refs_in=None)`
     —— generations 与 edits 完全共用下游（2K 上游 / CI 放大 / 轮询 / 护栏 / 无水印）
  2. 新增 `_bytes_to_data_uri` / `_edits_parse` / `images_edits`
"""
import ast
import io

P = "yuanbao_openai_proxy.py"
src = io.open(P, encoding="utf-8").read()

START = '@app.post("/v1/images/generations")\nasync def images_generations(req: Request):'
END = '    return resp\n\n\nif __name__ == "__main__":'

i = src.find(START)
j = src.find(END)
assert i > 0, "找不到 generations 端点起点"
assert j > i, "找不到终点锚点"
old = src[i:j + len("    return resp\n")]
assert "GUARD.check(\"image\")" in old, "切片内容不对（应包含护栏）"
assert old.count("def images_generations") == 1
print("  切片长度 =", len(old), "行数 =", old.count("\n"))

NEW = '''def _bytes_to_data_uri(blob: bytes, mime: str = "") -> str:
    """上传的图片字节 → data URI（mime 缺失时按魔数嗅探）。"""
    import base64 as _b64
    m = (mime or "").split(";")[0].strip().lower()
    if not m.startswith("image/"):
        if blob[:8] == b"\\x89PNG\\r\\n\\x1a\\n":
            m = "image/png"
        elif blob[:3] == b"\\xff\\xd8\\xff":
            m = "image/jpeg"
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            m = "image/webp"
        elif blob[:6] in (b"GIF87a", b"GIF89a"):
            m = "image/gif"
        else:
            m = "image/png"
    return "data:%s;base64,%s" % (m, _b64.b64encode(blob).decode())


async def _edits_parse(req: Request):
    """解析 `/v1/images/edits` 入参 → (body, image_refs)。

    支持两种形态：
      - `multipart/form-data`（OpenAI 标准）：`image` 可重复（1~4 张），`mask` 忽略
      - `application/json`（部分客户端）：`image` 为 URL / dataURI / 裸 base64
    解析失败返回 (JSONResponse, None)。
    """
    ctype = (req.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in ctype:
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": {
                "message": "请求体既不是合法 JSON，也不是 multipart/form-data",
                "type": "invalid_request_error"}}, status_code=400), None
        img = body.get("image")
        if isinstance(img, str):
            refs = [img] if img.strip() else []
        elif isinstance(img, list):
            refs = [x for x in img if isinstance(x, str) and x.strip()]
        else:
            refs = []
        return body, refs

    try:
        form = await req.form()
    except Exception as e:
        return JSONResponse({"error": {
            "message": "multipart 解析失败（缺 python-multipart？）: %s" % str(e)[:120],
            "type": "api_error"}}, status_code=500), None

    refs, total = [], 0
    files = list(form.getlist("image")) + list(form.getlist("images"))
    for f in files:
        if not hasattr(f, "read"):
            # 有些客户端把 image 当普通文本字段传 URL
            if isinstance(f, str) and f.strip():
                refs.append(f.strip())
            continue
        try:
            blob = await f.read()
        except Exception:
            continue
        if not blob:
            continue
        if len(blob) > YB_EDITS_MAX_BYTES:
            return JSONResponse({"error": {
                "message": "单张图片超过 %d MB 上限" % (YB_EDITS_MAX_BYTES // 1048576),
                "type": "invalid_request_error"}}, status_code=413), None
        total += len(blob)
        if total > 3 * YB_EDITS_MAX_BYTES:
            return JSONResponse({"error": {
                "message": "参考图总量过大", "type": "invalid_request_error"}}, status_code=413), None
        refs.append(_bytes_to_data_uri(blob, getattr(f, "content_type", "") or ""))
        if len(refs) >= 4:
            break            # 元宝单次上限 4 张

    body = {}
    for k in ("prompt", "model", "size", "n", "response_format", "user"):
        v = form.get(k)
        if v is not None and str(v).strip() != "":
            body[k] = str(v)
    if body.get("n"):
        try:
            body["n"] = int(body["n"])
        except Exception:
            body["n"] = 1
    if form.get("mask") is not None:
        body["_mask_ignored"] = True     # 元宝没有局部重绘，mask 只能忽略（响应里会提示）
    return body, refs


async def _images_core(req: Request, body: dict, image_refs_in=None):
    """生图/图生图公共实现 —— `/v1/images/generations` 与 `/v1/images/edits` 共用。

    `image_refs_in` 不为 None 时优先用它（edits 的参考图来自 multipart 上传）。
    """
    cred = _check_auth(req)
    _mctx(endpoint=req.url.path)
    if isinstance(cred, JSONResponse):
        return cred
    cookie_str = cred[1] if isinstance(cred, tuple) else None
    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # 兜底：本实例浏览器号已封/掉登录时，改用组内健康的 cookie 账号（否则 /v1 全 502）
    if not cookie_str:
        cookie_str = _fallback_cookie()

    prompt = (body.get("prompt") or "").strip()
    if image_refs_in is not None:
        image_refs = [x for x in image_refs_in if isinstance(x, str) and x.strip()]
    else:
        # 规范 image 字段（约定对齐 image-adapter）：带 image 即图生图；null/[]/空串等同不传
        image_field = body.get("image")
        if isinstance(image_field, str):
            image_refs = [image_field] if image_field.strip() else []
        elif isinstance(image_field, list):
            image_refs = [x for x in image_field if isinstance(x, str) and x.strip()]
        else:
            image_refs = []
    if not prompt and image_refs:
        # edits 场景常见：只给图不给字。给一个保守默认，避免白等一下才报错。
        prompt = "保持主体不变，按原图风格自然重绘"
    if not prompt:
        return JSONResponse({"error": {"message": "prompt 为空", "type": "invalid_request_error"}}, status_code=400)

    chat_model = MODEL_MAP.get(body.get("model", "hunyuan"), body.get("model") or "hunyuan_gpt_175B_0404")
    if _is_image_model(chat_model):
        chat_model = "hunyuan_gpt_175B_0404"  # 生图端点里 model 仅是别名，chatModelId 归一到基础模型
    n = min(int(body.get("n", 1) or 1), 4)
    resolution = _size_to_resolution(body.get("size", ""))
    unwm = False

    # ---- 护栏：生图一律按 image 计（养号期直接挡掉）----
    _g = GUARD.check("image")
    if _g:
        print("[guard] 拒绝生图: %s" % _g["error"].get("code"), flush=True)
        return JSONResponse(_g["error"], status_code=_g["status"])
    GUARD.note()

    created = int(time.time())
    conv_created = None
    try:
        if cookie_str:
            result = _cookie_mode_run(cookie_str, (("naQivTmsDa" if YB_MINT_BACKEND == "cdp" else _ensure_page()["agentId"])), prompt,
                                      chat_model, image_refs, resolution,
                                      _size_to_ratio(body.get("size", "")))

        else:
            # 浏览器模式：执行器 = bsk（本机）| cdp（服务器 headless Chromium）
            if YB_MINT_BACKEND == "cdp":
                tab, agent = None, "naQivTmsDa"
            else:
                ctx = _ensure_page()
                tab, agent = ctx["tabId"], ctx["agentId"]
            with _bsk_lock:
                if YUANBAO_CONVERSATION:
                    conv = YUANBAO_CONVERSATION
                elif YB_CREATE_CONV == "1":
                    conv = _pev(f"({JS_CREATE_CONV})({json.dumps(agent)})", 60)
                    conv_created = conv
                else:
                    # 可选捷径（YB_CREATE_CONV=0）：客户端 UUID 直当会话 ID，跳过 create；默认关闭（反风控优先）
                    conv = str(uuid.uuid4())
                    conv_created = conv
                sig = get_sig(tab)
                if image_refs and not unwm:
                    # ---- 图生图（image 字段）----
                    import base64 as _b64
                    multimedia = []
                    for i, ref in enumerate(image_refs[:4]):
                        uri = _ref_to_data_uri(ref)
                        head, _, b64data = uri.partition(";base64,")
                        mime = head[5:].split(";")[0] or "image/png"
                        if not mime.startswith("image/"):
                            mime = "image/png"
                        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
                        entry = _pev(
                            f"({JS_UPLOAD_REF})({json.dumps({'b64Data': b64data, 'name': f'ref_{i}.{ext}', 'mime': mime})})",
                            timeout=300,
                        )
                        multimedia.append(entry)
                    result = _pev(
                        f"({JS_CHAT_I2I})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'multimedia': multimedia, 'resolution': resolution, 'ratio': _size_to_ratio(body.get('size', '')), 'chatModelId': chat_model, 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
                else:
                    result = _pev(
                        f"({JS_IMAGE})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'resolution': resolution, 'ratio': _size_to_ratio(body.get('size', '')), 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
    except Exception as e:
        _note_ban(e) or _note_expired(e)
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)

    # 用完即删（浏览器模式）：历史零残留
    if conv_created:
        try:
            _pev(f"({JS_DELETE_CONV})({json.dumps({'conv': conv_created})})", 30)
        except Exception:
            pass

    auth_err = _yb_auth_error(result)
    if auth_err:
        return auth_err
    if result.get("error") and not result.get("urls"):
        return JSONResponse({"error": {"message": result["error"], "type": "api_error"}}, status_code=502)
    urls = result.get("urls") or []
    wm_urls = result.get("wmUrls") or []
    if not urls:
        return JSONResponse({"error": {"message": "未获取到图片: " + str(result.get("raw"))[:200], "type": "api_error"}}, status_code=502)
    # 客户端要 >2K ⇒ 出图后追加一级 CI 放大（上游固定 2K，见 _size_to_resolution）
    urls, _up = _maybe_upscale(urls, body.get("size", ""))

    # url = 无水印原图(originUrl/_h0_)；url_watermarked = 带"混元AI生成"水印版(_h1_)
    data = []
    for i, u in enumerate(urls):
        item = {"url": u, "revised_prompt": prompt}
        if i < len(wm_urls):
            item["url_watermarked"] = wm_urls[i]
        data.append(item)
    if n < len(data):
        data = data[:n]
    # 上游 image_gen_param 里没有"数量"字段 ⇒ 一次只出 1 张（除非它自己回多变体）。
    # `n` 只做上限截断（data[:n]），不循环请求凑数量 —— 那是拿封号风险换数量。
    payload = {"created": created, "data": data}
    if _up:
        payload["upscale"] = _up
    resp = JSONResponse(payload)
    if _up:
        resp.headers["X-YB-Upscale"] = "%s -> %s (%d/%d)" % (
            _up.get("requested"), _up.get("box"), _up.get("upscaled"), _up.get("total"))
    return resp


@app.post("/v1/images/generations")
async def images_generations(req: Request):
    # 统一入口：实例 key ⇒ 走号池轮询（cookie 串 / 内部转发 ⇒ 直连本实例）
    if _want_pool_route(req):
        return await pool_router(req, "images/generations")
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求体不是合法 JSON",
                                       "type": "invalid_request_error"}}, status_code=400)
    return await _images_core(req, body)


@app.post("/v1/images/edits")
async def images_edits(req: Request):
    """**OpenAI 标准图生图入口**（multipart/form-data 上传参考图）。

    与 `generations` 的差别只在入参形态：这里 `image` 是**文件**（可重复 1~4 张）。
    解析完统一交给 `_images_core` —— 2K 上游 / CI 放大 / 轮询 / 护栏 / 无水印全部共用。

    ⚠️ `mask` 不支持（元宝没有局部重绘）⇒ 忽略并回 `X-YB-Warning`，别当它生效了。
    """
    if _want_pool_route(req):
        return await pool_router(req, "images/edits")
    body, refs = await _edits_parse(req)
    if isinstance(body, JSONResponse):
        return body
    if not refs:
        return JSONResponse({"error": {
            "message": "edits 需要至少 1 张参考图（multipart 的 image 字段，或 JSON 的 image）",
            "type": "invalid_request_error"}}, status_code=400)
    resp = await _images_core(req, body, image_refs_in=refs)
    if body.get("_mask_ignored") and hasattr(resp, "headers"):
        try:
            resp.headers["X-YB-Warning"] = "mask 不支持（元宝无局部重绘），已按整图重绘"
        except Exception:
            pass
    return resp
'''

src = src[:i] + NEW + src[j + len("    return resp\n"):]

# 配置：edits 单张图上限
anchor = 'YB_BULK_GAP = float(os.environ.get("YB_BULK_GAP", "1.2"))'
assert src.count(anchor) == 1
src = src.replace(anchor, anchor + '''
# /v1/images/edits 单张上传图上限（默认 8MB；元宝侧最终走 base64 内联，别设太大）
YB_EDITS_MAX_BYTES = int(os.environ.get("YB_EDITS_MAX_BYTES", str(8 * 1024 * 1024)))''')

ast.parse(src)
io.open(P, "w", encoding="utf-8").write(src)
print("AST OK, written")
