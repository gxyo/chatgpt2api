# sentinel 求解服务

2026-10-14 起 chatgpt.com 的对话接口开始严格校验 sentinel：

- `chat-requirements` 的挑战必须在**真实 chatgpt.com 页面**里执行，才会得到上游认可的
  Turnstile token。纯 Python / Node 模拟出来的结果会被判无效，缺 token 时上游返回
  403 "Unusual activity"，或者干脆把请求伪装成 404 `{"detail":"Model not found"}`。
- TLS 指纹也要跟上新版 Chrome，主服务的 curl_cffi 默认值已改为 `chrome146`。
- requirements / proof token 里编码的 UA 必须和算 Turnstile 的那个页面一致。

只要校验通不过，其他对话接口不会直接报 403，而是返回伪装过的 404
`{"detail":"Model not found"}`——看起来像模型没了，实际是账号被上游拒了。

这个服务常驻一个无头 Chromium（`channel="chromium"`，加 playwright-stealth）挂在
chatgpt.com 页面上，按需跑挑战，不保存任何账号状态。

求 Turnstile 有两条路，优先第一条：

1. **页面自带的模块**：`import()` chatgpt.com 自己加载的那个 `turnstile-*.js` chunk，
   调它的 `getEnforcementToken(requirements)`（用 `WeakMap.prototype.get` 打个小补丁，
   让它拿我们提交的 p 去查 seed）。上游改前端时这条路自动跟上。
2. **移植的 VM（`vm.js`）**：上一条失败时的退路，得跟着前端指令集人工维护。

chunk 文件名带内容哈希、每次发版都变，所以运行时**动态发现**：先看页面实际加载了哪些
资源，再翻页面脚本里的字面路径，最后才用写死的路径兜底。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 页面状态、页面使用的 UA，以及页面自己算出来的时区和偏移 |
| POST | `/v1/turnstile` | `{"dx", "p", "requirements"}` → `{"token"}`。dx 取自 prepare 响应，p 是 prepare 里提交的 requirements token，`requirements` 是整份 prepare 响应（给了它就调页面自带的模块）；算不出来返回 503 |
| POST | `/v1/so` | `{"collector_dx", "snapshot_dx", "p"}` → `{"token"}`，目前对话接口还没有强制要求 |

求解服务只接收 `dx`、`p` 和 prepare 响应这类挑战材料（prepare_token、PoW seed、SO
句柄），账号 access token、Cookie 和用户 prompt 都不会离开主服务进程。

设置了 `SENTINEL_SOLVER_KEY` 时，所有接口都要带 `Authorization: Bearer <key>` 头。

## 部署

```bash
docker compose up -d --build sentinel-solver
```

主服务通过环境变量或 `config.json` 里的 `sentinel_solver_url` / `sentinel_solver_key`
指向本服务：

```
CHATGPT2API_SENTINEL_SOLVER_URL=http://sentinel-solver:8080
CHATGPT2API_SENTINEL_SOLVER_KEY=<和 SENTINEL_SOLVER_KEY 相同>
```

未配置或者调用失败时，主服务**直接报错**，不退回本地求解器：本地算出来的 token
在当前版本上已经被上游拒绝，继续用只会换来伪装成 404 `Model not found` 的响应，
而那条错误会被上层理解成「这个账号不行」，白冷却一个账号。要刻意不跑这个服务，
把设置里的「生图握手模式」切成旧版。

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SENTINEL_SOLVER_KEY` | 空 | 服务密钥，建议设置 |
| `SENTINEL_SOLVER_PROXY` | 空 | 浏览器出网代理，必须与主服务访问 chatgpt.com 的出口一致 |
| `SENTINEL_SOLVER_USER_AGENT` | 按实际 Chromium 版本生成 | 页面 UA |
| `SENTINEL_SOLVER_TIMEZONE` | `America/Los_Angeles`（compose） | 页面时区，如 `America/Los_Angeles`，必须和出口 IP 所在时区一致。主服务会照抄页面报的值，所以这是生效来源 |
| `SENTINEL_SOLVER_REFRESH_SECONDS` | 1800 | 页面定期重开的间隔 |
| `SENTINEL_SOLVER_HEADLESS` | true | 排障时置为 false |
| `PORT` | 8080 | 监听端口 |

## 注意

- 必须用完整 Chromium 的新版无头模式（`channel="chromium"`），默认的 headless shell
  过不了 Cloudflare；页面卡住时先看容器日志里的 title。
- `vm.js` 是移植的挑战解释器，只在页面自带模块求解失败时才会用到，上游改指令集时
  需要同步更新。正常情况下走的都是页面自带的模块，那条路不需要我们跟着改。
- 代理、时区、UA 三者要和主服务实际出口一致：同一个代理供应商、同一个主机名不代表
  同一个出口，代理账号里的 session 不同也可能分到不同 IP。
- 主服务不自己拼时区和偏移，而是从 `/health` 照抄这个页面算出来的
  `(timezone, timezone_offset_min)`（两项一起取，取不到就整对退回本机配置），
  所以「时区名和偏移对不上」这种自相矛盾不会出现。页面时区由
  `SENTINEL_SOLVER_TIMEZONE` 决定，改它是唯一需要动的地方。
- 服务本身只做挑战计算，不登录账号；即使使用真实网页，被上游标记的账号仍然会返回
  "Model not found"，那是账号策略问题，换号即可。
