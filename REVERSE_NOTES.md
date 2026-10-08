# 腾讯元宝 (yuanbao.tencent.com) 协议逆向笔记

> 2026-10-08 抓包实测。抓包证据在 `capture/`（yuanbao_run1/2.json 为 bsk 导出，
> _app.js 为前端 chunk 存档）。

## 1. 端点

| 用途 | 方法 | 路径 |
|---|---|---|
| 聊天/生图（同一个） | POST | `/api/chat/{conversationId}` |
| 建会话 | POST | `/api/user/agent/conversation/create` body `{"agentId":"naQivTmsDa"}` → `{"id":"xxx"}` |
| 模型列表 | POST | `/api/agent/model/list` |
| 切模型 | POST | `/api/user/agent/conversation/updateModel` |

- 聊天请求 `Content-Type: text/plain;charset=UTF-8`（不是 application/json！）
- 响应 `text/event-stream`（SSE），终止符 `data: [DONE]`
- `agentId` 是 URL `/chat/naQivTmsDa` 里这段（元宝助手 ID）

## 2. 签名三件套（核心壁垒）

每次聊天/生图请求必须带：

```
X-Uskey          ← Qimei SDK getUSKeySync("7800385", h38, signStr)，加密 protobuf，~800B
X-Bus-Params-Md5 ← md5(signStr)（不是 body 的哈希！）
X-Timestamp      ← 毫秒时间戳，与 uskey 内嵌时间一致
```

其中 `signStr = "h38={h38}&timestamp={ms}&platform=web"`，
`h38` = 设备 qimei36（即静态头 `X-HY92` 的值，本机为 `e9632faf...419610`）。

**生成代码位置**（webpack 模块，`_app.15ecb385d8da9152.js`）：
- module `28850` 导出 `TE`（内部函数 m）：输入 appKey `0WEB05U9OEC1ZNRY`，
  返回 `{X-Uskey, X-Bus-Params-Md5, X-Timestamp}` 三件套
- module `77004` 导出 `I5`（取 SDK 实例）、`PU`（appKey）

**脱离浏览器复现 uskey 不可行**（Qimei 风控 SDK，protobuf+加密）。
可行方案 = 页内铸造：`webpackChunk` push 拿 require → `req(28850).TE(req(77004).PU)`。

实测结论：
- 签名**不绑定请求 body**（换 body 仍通过）
- 签名**可短时复用**（同三件套立即重发 OK）
- `X-Timestamp` 与 `uskey` 必须配对（错配→软拒 "服务繁忙"）
- 三件套放几分钟后再用 → 软拒（TTL 未精确测量，估计 1-5 分钟）
- 软拒表现：HTTP 200 + SSE `{"type":"error","msg":"服务繁忙，请稍后再试。"}`

## 3. 静态头（照抄真实请求即可）

`X-Input-Type / X-Requested-With: XMLHttpRequest / X-Instance-ID / X-Source: web /
X-Language / X-device-id / X-HY92(=h38) / X-HY93(=device-id) / X-HY106 / X-os_version /
X-Platform: mac / X-webdriver: 0 / X-ybuitest / X-Exp-Params / x-commit-tag /
X-WebVersion / X-Event-Input-Type(聊天11、生图15) / X-AgentID({agentId}/{convId}) / chat_version: v1`

cookie 由浏览器环境携带；生图 403 的 COS URL 用 curl 直下即可（防盗链不校验 Referer）。

## 4. 聊天 body（v2 版本）

```json
{
  "model": "gpt_175B_0404",              // 固定值
  "prompt": "用户消息",
  "plugin": "", "displayPrompt": "...", "displayPromptType": 1,
  "agentId": "naQivTmsDa", "isTemporary": false, "projectId": "",
  "chatModelId": "hunyuan_gpt_175B_0404",  // 模型选择放这里
  "chatModelExtInfo": "{\"modelId\":\"hunyuan_gpt_175B_0404\",\"agentModeModelSetting\":{\"modelId\":\"hunyuan_gpt_175B_0404\"},\"supportFunctions\":{\"internetSearch\":\"\"},\"internetSearch\":\"autoInternetSearch\"}",
  "supportFunctions": ["openAutoSearchSwitch","autoInternetSearch"],
  "options": {"imageIntention":{...}}, "multimedia": [], "supportHint": 1,
  "applicationIdList": [], "version": "v2", "isAtomInput": false,
  "conversationId": "0R4Xo4hehE1", "offsetOfHour": 8, "offsetOfMinute": 0
}
```

⚠️ 切模型规则（UI 实抓验证）：顶层 `model` 固定 `gpt_175B_0404`；
`chatModelId` = 目标模型；extInfo 里 `modelId` 恒为基础模型，
**`agentModeModelSetting.modelId` 才是目标模型**。写错 → 401 或软拒。

可用 chatModelId：`hunyuan_gpt_175B_0404`(快速) / `hunyuan_t1`(深度思考) /
`deep_seek_v3` / `deep_seek`(深度思考)。

## 5. SSE 事件

- `data: {"type":"text","msg":"增量文本"}` —— 正文 chunk
- `data: {"type":"step",...}` —— 阶段提示（优化提示词/生成图片中）
- `data: {"type":"progress","value":0.87,"desc":"Hy Image3.5 preview"}` —— 生图进度
- `data: {"type":"replace","replace":{"multimedias":[{url,previewUrl,...}]}}` —— **取最后一次** replace 的 url（前面的 replace 是占位，签名无效！）
- `data: {"type":"meta",...}` —— 收尾：`tokenUsageInfo{promptTokens,completionTokens,totalTokens}`、`performance.dataIndex{ThinkingStart/End}`
- `data: [DONE]`
- 错误：`{"type":"error","msg":"服务繁忙..."}`（多为风控软拒/限流）
- thinking 内容：未见独立事件类型（推测按 meta.dataIndex 的 ThinkingStart/End 切 text 序列，未验证——deepseek 当日额度被打爆）

## 6. 生图 body

同聊天 body，追加：
```json
{
  "skillIdParam": "ai_image", "skillId": "ai_image", "chatSource": "ai_image",
  "msgScene": 13, "question": "...",
  "extra": {"image_gen_param": {"model": "Hy Image 3.5", "resolution": "1.5K"}},
  "applicationIdList": ["application_id_ai_image"]
}
```
- resolution：`1K` / `1.5K` / `2K`；**只控总像素档位（≈1MP / ≈2.3MP / ≈4MP）**
- **比例（实测 UI 抓包）**：`extra.image_gen_param.ratio`，槽位 `1:1 / 4:3 / 3:4 / 16:9 / 9:16`，
  配置源 `POST /api/v1/config/aigc/get-image-proportion`（"智能比例" = 不带 ratio 字段，
  模型按 prompt 语义自定构图）。比例与 resolution 正交（UI 可同时选 16:9 + 2K，实测 2240×1248）
- **代理 size 映射**：`auto`/缺省 → 不带 ratio（智能比例）；`WxH` → 对数距离最近槽位
  （1792x1024→16:9、1024x1792→9:16、1536x1024→4:3、1024x1024→1:1）；`16:9` 显式比例直传
- **代理 size 映射**（同上，两个端点 chat images 与 images/generations 均生效）
- 每次默认 4 张（多图时 mediaId 后缀 _0.._3）
- **水印规则**：multimedia 里 `url`/`previewUrl`/`downloadUrl` 都指向 `_h1_` 对象
  （带"混元AI生成"水印）；**`originUrl` 指向 `_h0_` 对象 = 无水印原图**，同样十年期签名可直下。
  代理已默认返回 originUrl，水印版放附加字段 `url_watermarked`
- 产出 COS 带签名 URL，q-sign-time 10 年，curl 可直下，PNG，右下角"混元AI生成"水印

## 6.5 图生图（i2i）协议（实测通过）

UI 流程（上传参考图 + prompt → 风格改写/重绘）：

1. **铸上传凭证**：`POST /api/resource/genUploadInfo`
   body `{"fileName":"ref.png","docFrom":"localDoc","docOpenId":"","needAuth":true}`
   → 返回 `{resourceUrl, cosURL, postAuthorization, ...}`（resourceUrl 即 resourceId 长链）
   该接口只需静态头，**不需要 uskey 签名**
2. **直传文件**：`PUT {cosURL}`（cos.accelerate 加速域名，`multimedia_96/` 路径）
3. **聊天请求**：与文生图 body 相同，追加：
   - `msgScene: 12`（文生图是 13）
   - `multimedia: [{type:"image", docType:"image", url:"https://hunyuan.tencent.com/api/resource/download?resourceId=xxx_96", signUrl:"..."}]`
     （原图 + 缩略图两条；url 字段用 resourceId 链接，非 COS 签名）
4. 响应同文生图 SSE（replace → originUrl 无水印）

实测：参考图"雪山湖泊" + prompt"改成吉卜力动画风格" → 构图保留、风格重绘成功，
产物 `samples/06_图生图_吉卜力.png`（2112x1104，无水印 _h0_）。

浏览器自动化注意：`bsk upload` 的 input/drop 模式都被 Chrome 扩展权限挡
（"Allow access to file URLs"），可行方案 = 页内 `DataTransfer` 塞
`input[type=file].files` + 派发 change 事件（元宝 accept `.jpg,.png,.jpeg`）。

## 7. OpenAI 适配器

`yuanbao_openai_proxy.py`（FastAPI, 127.0.0.1:8177）：
- `GET /v1/models`、`POST /v1/chat/completions`(stream/非流式)、`POST /v1/images/generations`
- 每请求：bsk evaluate 页内铸签名 → 页内 fetch 发请求 → 解析 SSE → 转 OpenAI 格式
- 聊天为缓冲伪流式（等完整回答后快速分块回放）
- messages 摊平成单 prompt、每请求新建会话（无状态）；可 `YUANBAO_CONVERSATION` 固定会话
- 依赖：bsk CLI + 浏览器扩展在线 + 已登录元宝的浏览器 profile

## 8. 踩坑

1. bsk replay 对该站不可用（"replay failed or timed out"，别浪费时间）
2. 页内 fetch 手动铸 uskey（`mod.I5().getUSKeySync`）会失败；必须走 `modHdr.TE()`（其内部
   可能用单例 SDK 状态）——直接调应用自己的 header 构造函数最稳
3. bsk evaluate 传 JS 里的 `\n` 要小心 shell/Python 转义吃掉
4. agent tab 会话停止/导航走后 evaluate 会打到错误页面，先 `location.href` 校验
5. deepseek 系模型有独立配额，打爆后软拒"服务繁忙"（与签名无关）
6. **hy_token 单传 ≠ 元宝鉴权**（实测 cookie `hy_token=xxx` 带不带签名三件套都是 401
   code=20000"未知错误"；且 `hy_token` 在元宝前端 chunk 零引用）——元宝 web 凭证就是
   浏览器会话 cookie（httpOnly）+ 页内签名，无法用裸 token 驱动
7. **COS 参考图直传的 Authorization 要用 `putAuthorization`**（genUploadInfo 返回四个
   签名：post/put/get/delete——PUT 用 post 的会 SignatureDoesNotMatch 403）

## 9. 代理鉴权与凭证透传（客户端 → 代理）

三种姿势（自动路由，实测通过）：
```
X-Yuanbao-Cookie: <cookie串>        # 显式元宝凭证 → 透传模式
Authorization: Bearer <cookie串>    # cookie 形态（含=和;）自动识别 → 透传模式
Authorization: Bearer <YUANBAO_API_KEY>  # 门禁 key（设了才校验）→ 浏览器会话模式
无任何凭证                           # 浏览器会话模式
```

**凭证最小集（cookie_minimizer.py 实测二分）**：19 对 → **2 对**
```
hy_token=<RSA加密blob约900B>; hy_user=<32位hex userId>
```
两者缺一不可（hy_token 单独 → 401 code=20000）；其余 17 对全是统计/埋点垃圾可全删。
最小集存 /tmp/yb_cookie_minimal.txt；精简器 `cookie_minimizer.py`（基线不过=cookie
失效或与签名设备不匹配）。

透传模式架构：cookie 来自客户端，数据面走代理出站 HTTP（conversation/create、
chat、genUploadInfo+COS PUT 全出站）；签名三件套仍借页面铸造（uskey 离线不可得）。
⚠️ 跨设备风险未验证：uskey 绑本机 h38，若 cookie 来自其他设备登录，可能 401。
