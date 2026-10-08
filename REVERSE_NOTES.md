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

## 10. 反风控加固（v2）

| 对策 | 实现 |
|---|---|
| 签名复用 | 三件套缓存 TTL 60s（`YB_SIG_TTL`），实测可复用；软拒时强制重铸 |
| 动态指纹 | UA/os_version/webversion/commit-tag/HY92 运行时从页面提取（10 分钟缓存），元宝发版自动跟上 |
| 限速 | 同凭证最小间隔 `YB_MIN_INTERVAL=2s` |
| 软拒退避 | "服务繁忙" → 强制重铸签名 + 指数退避重试 `YB_SOFTRETRY=1` 次 |
| 临时会话 | `YUANBAO_TEMP_CONV=1`（默认开）：isTemporary=true，生成会话不进账号历史 |
| 401 明确化 | 透传凭证过期 → code=yuanbao_credential_expired |

hy-image 模型别名（chat 门直接生图）：`hy-image / hy-image-3.5 / hy-image-v3.5 / hy-image-v3.5-preview`，
无参考图=文生图（msgScene 13），带 image_url=图生图（msgScene 12），返回 content parts 图组。

## 11. 用完即删（历史零残留）

- 删除端点：`POST /api/user/agent/conversation/v1/delete`，body `{"cid": "<conversationId>"}`（无签名也放行）
- 代理默认 `YB_DELETE_CONV=1`：本次新建的会话（含软拒重试产生的中间会话）在响应前 best-effort 全部删除
- 临时会话真相：`isTemporary: true` 只在「UI 临时模式」下由服务端存储并在列表隐藏；
  裸 API 带该字段**不会**被隐藏（实测对照：UI 临时会话 detail 存 isTemporary=true 且不在列表，
  API 同字段会话在列表无标记）⇒ 代理改用「用完即删」保证历史干净
- 彩蛋：`/api/image/removewatermark` 接口存在（yb-util chunk），待逆向

## 12. 去水印端点逆向现状（实验性）

- 端点：`POST /api/image/removewatermark`，SSE 响应；空参 → "输入图片为空"/"images url is nil"
- 同族接口：`/api/image/clarity`（高清）、`/api/image/style`、`/api/image/outpainting`（扩图）、
  `/api/image/elimination`（消除）——均挂在图像编辑器（非会员功能）
- 已穷举参数形态（JSON `images`×6 种、包装 2 种、multipart×3）→ 全部"images url is nil"
  服务端真实契约在图像编辑器 chunk（按需加载），需从编辑器 UI 真实触发一次才能拿到
- 代理已留管道：model=`hy-image-unwatermark`（chat 门 + image_url 输入）→ 自动走多形态尝试，
  未破解时返回 501 + removewatermark_experimental；参数破解后即插即用
- 注意：**元宝自产图本就无水印**（originUrl/_h0_ + 账号「无水印保存」开关），此端点只对
  外部/水印图有价值
- 踩坑：往 Python `"""` JS 模板里写 `\n` 会被解成真实换行 → JS 字符串断裂 SyntaxError；
  必须写 `\\n`。大图 data URI 走 argv 会 E2BIG → 分块暂存 window.__payload 再执行

## 13. 无浏览器化调研结论（uskey 离线铸造可行性）

- **实验矩阵**：create 不校验 uskey（无/空/垃圾全过）；**chat 强校验**（无 uskey →
  假拒绝"抱歉，我无法回答这个问题"——风控软拒第二形态，与"服务繁忙"不同）
- **SDK 定位**：QimeiWeb 类在独立 vendor chunk `yb_v2_vendor_qimei.*.js`（208KB，
  模块 72101，零 webpack 依赖、自包含）；应用侧 12601 是薄封装（构造参数
  `{appKey, disableDebugger, disableConsoleDetection}`）
- **阻塞点**：模块 72101 是**字节码 VM 混淆**——switch 虚拟机解释器 + 编码操作数数组
  （`Y[a[++F]][Y[a[++F]]].call(...)` 栈机），getUSKeySync 逻辑在字节码里。
  离线复刻 = VM 逆向大工程，短期不可行
- **已实现**：Node 离线 harness（qimei-node/mint_uskey.js）：假 webpack runtime +
  浏览器 shims（CSS/navigator/document/localStorage/XHR）成功加载并实例化 SDK——
  VM 逆向突破后即可接入；大 payload 分块暂存 window.__payload 绕 argv 限制
- **部署结论**：当前最优 = 服务器 Docker 跑 headless Chrome（登录态 profile）+
  bsk 等价物；或本机代理 + 服务器反代。签名每 60s 一次铸签调用的浏览器依赖已最小化

## 14. 旧版 MeUtils 实现考古（2024-06）与借鉴

- 旧实现在今天已失效：仅 cookie 调 chat → 401；极简 payload → 400（缺 agentId 等新必填）
- **已借鉴落地**：
  1. **客户端 UUID 直当会话 ID**（跳过 conversation/create，省一次请求）——已实现为
     `YB_CREATE_CONV=0` 可选捷径；**默认仍走官方 create**（官方前端从不产生客户端自造会话 ID，
     该模式属非官方行为指纹，反风控优先）
  2. **GET /api/info/general 探活**（cookie + 静态头即可，无签名无浏览器、零副作用）——
     token_monitor 已改造为完全离线
  3. reasoning_content/search_content 的 SSE 语义（解析已支持，待按需透传）
- 教训：2024→2026 元宝安全演进 = cookie-only → 静态头 → 签名三件套（chat 强制）

## 15. 代理池接入（数据面反风控）

- 池型：`socks5h://user:pass@pool.livetest.cn:2088`（每请求换出口 IP，实测 .156→.157→.158）
  与 `:2089`（按目标域名哈希恒定出口，适合会话型流量/浏览器侧备选）
- 实现：数据面 `_open()` 检测 socks 前缀 → 走 `requests[socks]`（urllib 不支持 socks5）；
  http/https 代理与直连仍走 urllib。仅连接级错误标坏（HTTP 4xx/5xx 直通），60s 冷却，全冷却直连兜底
- 凭据纪律：代理 URL 只放服务器 `.env`（compose 用 `${YB_PROXY_POOL:-}` 注入），仓库零凭据
- 实测：聊天/生图经代理池 200（美区 IP 段对元宝数据面无地理限制）
- chromium 侧暂直连（`--proxy-server` 不支持 URL 内认证，需 CDP Fetch.authRequired 适配后启用 2089）
