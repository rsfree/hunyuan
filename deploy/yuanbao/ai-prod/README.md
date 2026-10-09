# 元宝逆向服务 · 多账号 / 保活 / 安全更新

## 一、总体形态

```
                    ┌───────────── new-api（渠道：轮询 / 故障转移 / 限流）─────────────┐
                    │                                                            │
   https://yuanbao.1task.cn/v1        https://yuanbao2.1task.cn/v1        ...    │
                    │                              │                             │
              ┌─────▼──────┐                ┌──────▼─────┐                       │
              │  nginx     │                │  nginx     │                       │
              └─────┬──────┘                └─────┬──────┘                       │
                    │ 127.0.0.1:39177             │ 127.0.0.1:39178              │
        ┌───────────▼──────────┐      ┌──────────▼───────────┐
        │ 容器 yuanbao-acc01   │      │ 容器 yuanbao-acc02   │
        │  1 API + 1 chromium  │      │  1 API + 1 chromium  │
        │  profile: acc01      │      │  profile: acc02      │
        └──────────────────────┘      └──────────────────────┘
             一个实例 = 一个账号（互不干扰）
```

**关键分工（沿用你定的原则）**：本服务**不自己轮询**，一个实例只承载一个账号；
多号 = 多实例；**轮询/故障转移交给 new-api 的渠道机制**。

> 为什么不做"一个容器多账号"：yuanbao 的登录态落在 Chromium profile（cookie + localStorage）里，
> 一个 profile 只能有一个登录账号。硬塞多号要么共享指纹（互相关联、一封全封），要么就得在一个
> 容器里跑 N 个 chromium —— 复杂度和内存都不如直接多实例。

## 二、目录布局（一实例一目录内容）

```
/opt/yuanbao/
├── build/                      # 共享代码 + Dockerfile + compose + 脚本
│   ├── yuanbao_openai_proxy.py / cdp_minter.py / socks_bridge.py
│   ├── Dockerfile  docker-compose.yml  entrypoint.sh
│   ├── .env                    # acc01 的配置（默认项目）
│   ├── .env.acc02              # acc02 的配置
│   ├── auths/
│   │   ├── acc01/chrome-profile/   # ← 账号 1 的登录态（bind mount，重建容器不丢）
│   │   └── acc02/chrome-profile/   # ← 账号 2
│   ├── update-service.sh  add-account.sh
│   └── nginx/
└── backup/                     # profile 滚动备份（update-service.sh 自动写）
```

镜像 `yuanbao-proxy:latest` **只构建一份**，所有账号共用。

## 三、多账号：加一个号

```bash
cd /opt/yuanbao/build
./add-account.sh acc02 39178 yuanbao2.1task.cn   # 名字 端口 [域名]
```

脚本会：建 `auths/acc02/chrome-profile`、生成 `.env.acc02`（继承代理池/fleet key，独立 API key）、
把新实例登记进所有 `.env*` 的 `YB_POOL_PEERS`，并打印后续命令。

然后：

```bash
# 1) 起容器
docker compose --env-file .env.acc02 -p acc02 up -d

# 2) 扫码登录这个号（每个实例一个独立 key）
#    https://<该实例域名>/qr?k=<acc02 的 YUANBAO_API_KEY>
#    还没配域名就用隧道：ssh -L 9102:127.0.0.1:39178 root@<机器>
#                      然后开 http://127.0.0.1:9102/qr?k=<key>

# 3) 配公网入口（DNS A 记录 → nginx vhost → apply-https.sh 签证书）

# 4) new-api 加渠道：Base URL https://<域名>/v1 ，Key = acc02 的 YUANBAO_API_KEY
```

**整池总览**：任一实例都能看到全部账号 —— 打开 `https://yuanbao.1task.cn/pool`
（或 `curl -H "Authorization: Bearer <任一 key>" .../admin/pool`）。
跨实例查询用 `YB_FLEET_KEY`（各实例共享的**只读** key，加账号时自动继承）。

> `YB_FLEET_KEY` 权限边界：**只能**调 `/admin/state|keepalive|peers|pool`，
> 不能当 API key 调 `/v1/chat|images|models`（已验证：fleet key 调 /v1/models → 401）。
> 所以它写在网页上也不至于被人白嫖模型。

## 四、保活（keepalive）

**威胁模型**：登录态会因"长期不用"被服务端判失效。所以需要**低成本、零副作用**地定期证明"我还活着"。

**做法**：`GET /api/info/general`
- 只需 cookie + 静态头，**不需要签名、不需要浏览器执行页面**
- 零副作用（不改数据、不产生会话）

实现细节：
- cookie 由 CDP `Network.getCookies` 从 Chromium 现取
- **探活的 HTTP 往返不占 CDP 锁** ⇒ 不会拖慢正在进行的对话
- 200 = 存活；401 = 已失效（需要重新登录）

触发方式（二者都有）：

| 方式 | 说明 |
|---|---|
| 进程内定时 | `YB_KEEPALIVE_SEC`（默认 900s = 15 分钟）。单 worker 才启用，避免 N 份重复 |
| 外部触发 | `POST /admin/keepalive`，可用宿主机 cron 打 —— 与进程生命周期解耦 |

日志按连续失败次数分级：`OK` / `WARN` / `ERR`（`YB_KEEPALIVE_ALERT` 默认 3 次后升 ERR），
方便直接挂日志告警。

状态可在 `/pool` 页面或 `/admin/state` 的 `keepalive` 字段查看。

## 五、重启 / 更新不丢登录

掉登录通常**不是**因为"重启"，而是因为**没给 Chromium 落盘的机会**：

- Chromium 的 Cookies 是 SQLite 文件，登录态刷新后不是立刻落盘
- 容器停止时若直接 SIGKILL，最近的 cookie 变更可能丢

本服务的三道保障：

1. **profile 是 bind mount**（`./auths/<acc>/chrome-profile`）→ 容器重建/删除都不影响文件
2. **entrypoint 优雅关闭**：自己当 PID1 收 SIGTERM → 先停 API → 再给 chromium 发 SIGTERM
   等它 flush 落盘 → 最后才 KILL。配合 `stop_grace_period: 30s`
3. **`restart: unless-stopped`** + 启动时清 `SingletonLock*`（容器重建后 hostname 变，
   Chromium 会误判"profile 被别的电脑占用"而拒绝启动）

### 标准更新流程

```bash
cd /opt/yuanbao/build
./update-service.sh            # 更新 acc01
./update-service.sh acc02      # 只更新 acc02
./update-service.sh all        # 逐台串行 = 天然滚动更新（其他号继续服务）
```

`update-service.sh` 做四件事，**缺一不可**：
1. **备份** `auths/<acc>` → `/opt/yuanbao/backup/<acc>-<时间戳>`（保留最近 10 份）
2. 构建镜像
3. `up -d` 滚动重启
4. **校验**：`/v1/models` 200 → `/admin/state` `logged_in:true` → `POST /admin/keepalive` `ok:true`

失败时会打印回滚命令，不会"看着像成功"。

> ⚠️ 手工操作时**不要**用 `docker compose down` 之后不管 chromium ——
> 现在 entrypoint 已能优雅处理；但如果你在**旧镜像**的容器里手工停，
> 先 `docker exec <容器> pkill -TERM -f chrome-linux/chrome` 再停。

## 六、环境变量速查

| 变量 | 作用 |
|---|---|
| `YUANBAO_API_KEY` | 本实例的 API 门禁 key（每账号不同） |
| `YB_FLEET_KEY` | 号池内部**只读**互通 key（各实例相同） |
| `YB_INSTANCE_NAME` / `YB_PORT` | 实例名 / 宿主机回环端口 |
| `YB_BASE_URL` | 本实例对外地址（号池总览里显示） |
| `YB_POOL_PEERS` | 号池清单 `name=url,name2=url2`（把**所有**实例都列上） |
| `YB_KEEPALIVE_SEC` | 保活间隔秒数，0=关闭 |
| `YB_DATA_PLANE` | `page`（用容器登录态，推荐）/ `cookie`（出站 HTTP + 文件 cookie） |
| `YB_WORKERS` | **必须 1**（页面/Cdp 数据面：多 worker = 多标签页 + 无互斥） |
| `YB_PROXY_POOL` | 数据面代理池（逐请求换 IP） |

## 七、号池生命周期管理（启用 / 禁用 / 隔离 / 剔除）

每个账号是一个**状态机**（`pool_state.py`），状态落在 `auths/<acc>/state/account-<acc>.json`：

| 状态 | 含义 | 谁改的 | 参与轮询 |
|---|---|---|---|
| `enabled` | 正常可用 | 初始 / 手动 / 自动恢复 | ✅ |
| `disabled` | **手动**禁用（临时下线） | 人工 | ❌ |
| `quarantined` | **自动**隔离（健康规则） | 自动 | ❌ |
| `ejected` | 剔除（不再使用） | 人工或超期自动 | ❌ |

**自动规则**（在保活观测里评估，每隔 `YB_KEEPALIVE_SEC` 一次）：

| 条件 | 动作 | 开关 |
|---|---|---|
| 页面被**冻结** | 立即自动隔离 | 固定 |
| 保活连续失败 ≥ N 次 | 自动隔离 | `YB_AUTO_DISABLE_AFTER`（默认 3） |
| 连续成功 ≥ M 次 | 自动恢复（**只回滚自动造成的隔离**） | `YB_AUTO_REENABLE_AFTER`（默认 3，0=关） |
| 隔离超过 D 天 | 自动剔除 | `YB_AUTO_EJECT_AFTER_DAYS`（默认 0=关） |

> 🔴 **手动禁用不会被自动恢复覆盖** —— 状态里记了 `changed_by`，只有 `auto` 造成的隔离才自动回滚。
> 避免"我明明手动下线了，它自己又活了"。已单测覆盖。

**手动操作**（页面按钮或 API）：

```bash
K=<实例key>
# 查看本实例状态
curl -H "Authorization: Bearer $K" https://yuanbao.1task.cn/admin/account
# 禁用 / 启用 / 剔除 / 恢复 / 重置（重置=清健康计数并启用）
curl -X POST -H "Authorization: Bearer $K" -H 'content-type: application/json' \
     -d '{"action":"disable","reason":"疑似风控","name":"acc02"}' \
     https://yuanbao.1task.cn/admin/account
```

`action` ∈ `enable|disable|eject|restore|reset|note`；带 `name` 会**转发给对应实例**执行（不必逐台登）。

## 八、轮询入口（号池分发）

```
/v1/*       → 只用本实例自己的账号（直连，行为不变）
/pool/v1/*  → 在整个号池里**轮询**分发，失败自动 failover
```

```bash
# 调用方只认一个 key（本实例的 YUANBAO_API_KEY）
curl https://yuanbao.1task.cn/pool/v1/chat/completions \
  -H "Authorization: Bearer $K" -H 'content-type: application/json' \
  -d '{"model":"hunyuan","messages":[{"role":"user","content":"hi"}]}'
# 响应头 X-YB-Routed-To: acc01  ← 本次落到哪个号
```

- 只有 `state=enabled` 的账号参与轮询；遇 5xx / 连接错误自动换下一个（最多试 3 个）
- 全部不可用 → `503`，message 里带已尝试的账号
- 关闭入口：`YB_ROUTER=0`
- ⚠️ 跨实例转发需要**目标实例的 API key**，写在 peer 配置里：`name=url|key`

## 九、调用统计（账号 × 模型）

- 每次 `/v1/*` 调用落一行 JSONL：`auths/<acc>/stats/events-<acc>.jsonl`
- 字段：`ts/ts_iso, instance, endpoint, model, status, ok, ms, stream, usage, err, client, key(短指纹)`
- 内存聚合按天保留 `YB_STATS_KEEP_DAYS`（默认 30）天，超期自动清理

| 页面 / 接口 | 说明 |
|---|---|
| `/stats` | 统计页：账号×模型矩阵 + 模型明细 + 按天走势 + 明细日志（可筛选/导出） |
| `/manage` | 管理页：账号清单 + 状态 + 自动规则 + 批量运维 + 变更历史 |
| `GET /admin/stats?days=7` | 本实例汇总（按模型/端点/状态/天） |
| `GET /admin/stats/detail?limit=100&model=&ok=` | 明细日志（最近优先） |
| `GET /admin/stats/export` | 导出原始 JSONL |
| `GET /admin/fleet/stats?days=7` | **号池汇总**（合并各实例 ⇒ 账号×模型） |
| `GET /admin/accounts` | 号池账号清单（状态+健康+24h 调用） |
| `POST /admin/fleet/{keepalive\|watermark\|state}` | 批量保活 / 批量开关无水印 / 批量查状态 |

## 十、无水印保存（新号自动开启）

逆向自 `yb_v2_yb-component` chunk，**无需签名、无需页面**：

```
资质门禁: GET  /api/info/general          → graySwitches.grayKeyWithoutWatermark !== false
读配置  : POST /api/userinfo/getuserconfig {scene:1, configFields:["watermarkConfig"]}
写配置  : POST /api/updateuserinfo         {updateFields:["watermarkConfig"],
                                            userConfig:{watermarkConfig:{...现值, ...patch}}}
开启    = patch {saveWithoutWatermark:true, hasPopupAgreement:true}
```

```bash
curl -H "Authorization: Bearer $K" https://yuanbao.1task.cn/admin/watermark         # 查
curl -X POST -H "Authorization: Bearer $K" -H 'content-type: application/json' \
     -d '{"enabled":true}' https://yuanbao.1task.cn/admin/watermark                  # 开
```

新号登录后由保活循环 + `/admin/state` **自动补开**（`YB_AUTO_WATERMARK=1`，默认开）；幂等，已开则不写。
换成新号（`/login/reset`）会清空判定，登录后重新补开。

## 十一、实例间互通：共享网络 + 跨实例代理

**共享 docker 网络 `yuanbao-net`**：所有实例加入同一个 external 网络，彼此用**容器名**寻址。
好处是 peer 之间不必依赖公网域名 / 回环端口（回环端口容器访问不到）。

```
YB_POOL_PEERS=acc01=https://yuanbao.1task.cn,acc02=http://yuanbao-acc02:39177
                                                    └─ 容器名，走 yuanbao-net
```

网络由脚本自动确保存在：`docker network inspect yuanbao-net || docker network create yuanbao-net`

**跨实例管理代理**（一个入口管所有号，`/pool/peer/<名字>/<路径>`）：

```bash
# 打开 acc02 的扫码页（登录那个号）
https://yuanbao.1task.cn/pool/peer/acc02/qr?k=<任一实例key>
# 读 acc02 的状态 / 版本
curl -H "Authorization: Bearer <key>" https://yuanbao.1task.cn/pool/peer/acc02/admin/version
```

- 🔴 **只按名字**从 `YB_POOL_PEERS` 查目标，**绝不接受 URL**（防 SSRF）
- 转发用 `YB_FLEET_KEY`；因此登录相关端点（`/login`、`/login/phone/*`、`/login/reset`）
  也归入管理面鉴权，fleet key 可用
- HTML 响应会注入垫片，把页面里的站内绝对路径请求改写到该 peer 前缀。
  **覆盖 fetch / XHR / `img.src` 直接赋值三处** —— 漏掉 `img.src` 会导致二维码图片仍从
  本实例取（本实例已登录时就会显示成"已登录页面"而不是二维码）。已实测踩过。

> 所以：**多号不需要每个号都配公网域名**。管理面走一个入口即可；
> 只有需要被 new-api 当独立渠道直连时，才给某个号配域名。

## 十二、路由健康门禁与新号冷启动

**门禁**：`/pool/v1/*` 只会把请求发给 **`state=enabled` 且最近一次保活成功** 的实例。

只判 `enabled` 是不够的 —— 刚 `add-account` 出来还没扫码登录的实例也是 `enabled`，
把它算进轮询只会把请求打到一个用不了的号上。

**新号冷启动（避免"等一个保活周期"）**：
- 进程内保活循环启动后 **15s** 就做第一次（原来是 60s）
- `/admin/state` 被轮询时，若发现**已登录但还没有过成功保活**，立刻补一次保活并落状态
  ⇒ 扫码登录后**约 20s 内**该号即可参与轮询

**新实例不被误隔离**：状态机有 `ever_ok` 标记 —— **从未成功保活过**的实例
（＝还没扫码登录）不计失败次数、不自动隔离。否则刚加出来的号会在 3 个周期后被隔离，
反而更难上手。一旦成功过一次，规则立刻恢复正常。
