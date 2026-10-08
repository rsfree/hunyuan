# hunyuan — 腾讯元宝 (yuanbao.tencent.com) OpenAI 适配器

把腾讯元宝的聊天与生图能力适配成 OpenAI 标准接口。抓包逆向 → 协议破解 → 本地代理，全链路实测。

## 端点

| 端点 | 说明 |
|---|---|
| `GET /v1/models` | hunyuan / hunyuan-t1 / deepseek-v3 / deepseek-r1 / dall-e-3 |
| `POST /v1/chat/completions` | 纯文本聊天；messages 带 `image_url` 即图生图（参考 image-adapter 折叠约定） |
| `POST /v1/images/generations` | 文生图；带 `image` 字段即图生图；`n` 最多 4 |

- 生图默认返回**无水印**原图（`originUrl/_h0_`），另附 `url_watermarked` 水印版
- `size` 两层语义：`auto` → 智能比例；`WxH` → 最近比例槽位（1:1/4:3/3:4/16:9/9:16）+ 分辨率档位（1K/1.5K/2K）
- 聊天为缓冲伪流式；usage 为元宝真实 token 数
- 会话策略（反风控优先）：默认走官方 `conversation/create` 建会话 + 用完即删（与前端行为一致）；
  `YB_CREATE_CONV=0` 可切客户端 UUID 捷径（少一次请求，非官方行为模式，自担风控风险）

## 凭证（三种姿势）

```bash
# 1. 无凭证 —— 走本机浏览器登录会话（默认）
# 2. 元宝凭证透传 —— 最小集就 2 个 cookie（缺一不可）
curl -H "X-Yuanbao-Cookie: hy_token=xxx; hy_user=xxx" http://127.0.0.1:8177/v1/chat/completions ...
# 3. Bearer 携带 cookie 串（自动识别）或门禁 key（设 YUANBAO_API_KEY 时校验）
```

## 启动

```bash
pip install fastapi uvicorn
python3 yuanbao_openai_proxy.py        # 127.0.0.1:8177
```

依赖：本机 bsk CLI + 浏览器扩展在线 + 已登录元宝的 Chrome。签名三件套
（X-Uskey/X-Bus-Params-Md5/X-Timestamp）由页面 Qimei SDK 现场铸造，无法离线复现。

## 工具

| 文件 | 用途 |
|---|---|
| `cookie_minimizer.py` | 19 对 cookie 二分精简到最小集（实测 `hy_token + hy_user` 两对，缺一不可） |
| `token_monitor.py` | 凭证探活监控（30 分钟/次，过期 macOS 通知） |
| `image_multi_test_v2.py` | 多形态生图回归（比例/分辨率/数量全槽位校验） |

## 已知限制

- hy_token 无自动续期机制（服务端不轮换、无 renew 端点），过期需重新扫码登录抓 cookie
- deepseek 系有独立配额，打爆后软拒"服务繁忙"
- uskey 绑定设备指纹（h38），跨设备 cookie + 签名组合未验证

协议细节、字段对照、踩坑清单见 [REVERSE_NOTES.md](REVERSE_NOTES.md)。
