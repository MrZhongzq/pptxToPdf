# 四期设计：管理入口与 Azure 凭证配置

日期：2026-07-28
状态：已定，待生成实施计划

---

## 1. 背景与目标

三期把 Microsoft Graph 引擎、切片流水线、合并、前端分片进度全部做完了，但**这条通道端到端不可达**：`graph_credentials.save_credentials` 零生产调用方，没有任何路径能把 Azure 凭证写进库。因此 `is_graph_configured` 恒为 `False`，自动路由恒选 LibreOffice，用户显式选 Graph 必然报 `GRAPH_NOT_CONFIGURED`。

四期的唯一目标：**给这把锁配一把钥匙**——一个受口令保护的管理页面，能配置 Azure 凭证，并在保存前验证这套凭证真的可用。

原总体设计（`2026-07-25-pptx-to-pdf-design.md` §2）把四期描述为「账号、配额、风控、管理面板」。经确认当前仍是自用阶段，故本期**只做管理面板中的凭证配置部分**，其余三项等真正开放使用时另行立项。

## 2. 范围

### 做

- 管理入口的口令鉴权（哈希存环境变量 + 签名 cookie，3 天有效期，滑动刷新）
- 管理页面：Azure 凭证配置表单
- 五步连通性自检，保存前强制通过
- 凭证写入路径（接上三期已就绪的 `save_credentials`）
- `openapi.json` 漂移守卫测试（补三期终审留下的缺口）

### 不做

- 账号体系（注册、多用户、角色）
- 配额与限流
- 风控与设备指纹
- 用户管理界面
- `Task.user_id` 继续留空

不做的理由是当前为自用阶段，这些都没有真实需求。总体设计 §9 关于风控的既定决策（设备指纹作加权信号而非硬阻断、IP 维度放宽因校园网 NAT）在真正开放时仍然有效，此处仅记录不予实现。

### 对既有代码的影响

后端**只增不改**。三期的引擎、分片流水线、引擎路由一行都不动。`graph_credentials.py` 已有的 `save_credentials` / `load_credentials` / `is_graph_configured` 直接复用。

## 3. 架构与组件

```
frontend/src/
  pages/AdminPage.tsx        配置表单 + 自检结果清单
  lib/adminApi.ts            管理端点的前端客户端
  App.tsx                    (改) 按 pathname 分发到 AdminPage

backend/app/
  api/admin.py               四个端点：登录、登出、读配置、存配置
  services/admin_auth.py     口令校验 + cookie 签发/验证
  services/graph_selftest.py 五步连通性自检
  assets/selftest.pptx       内置的单页测试文件
scripts/
  make_selftest_pptx.py      生成上述 pptx（开发时用，运行时不需要）
```

组件边界：

- `admin_auth` 只管「这个请求是不是管理员」，不认识 Azure 也不认识凭证
- `graph_selftest` 只管「这套凭证能不能用」，不认识 cookie 也不写库
- `api/admin` 把两者接起来，并持有「先测后存」这条规则
- 三者互不依赖对方的内部实现

## 4. 鉴权设计

### 口令存储

环境变量 `PPTX2PDF_ADMIN_PASSWORD_HASH`，值为 `hashlib.scrypt` 的十六进制输出加盐，格式 `scrypt:<salt_hex>:<hash_hex>`。使用标准库，不引入新依赖。

分隔符用 `:` 而不是 `$`，是因为 Docker Compose 会把 `.env` 里的 `$` 当变量插值：十六进制段以 a–f 开头时会被替换成空串（两段都以数字开头才幸存，概率约 39%），管理员会拿到一个「格式非法」的 503，指着他刚刚正确生成的值。这个失败只在容器里出现——本地裸跑时 pydantic-settings 直读 `.env` 不做插值，所以任何测试都抓不到它。

`.env.example` 给出生成命令，与 Fernet 主密钥的生成命令并排：

```
python -c "import hashlib,os,binascii; s=os.urandom(16); pw=input('口令: ').encode(); print('scrypt:'+binascii.hexlify(s).decode()+':'+binascii.hexlify(hashlib.scrypt(pw,salt=s,n=16384,r=8,p=1,dklen=32)).decode())"
```

**未配置口令时管理入口整体返回 503**，与 `secret_key` 未配置时的处理同构。不提供「没设密码就免密进入」的默认行为。

### 会话

用 Fernet 签发不透明 token 放进 cookie。Fernet 自带时间戳与 TTL 校验（`decrypt(token, ttl=...)`），签发与验证各一行，无需自行处理过期逻辑。

- 有效期：**3 天**
- **滑动刷新**：每个通过鉴权的请求重新签发并 `Set-Cookie`。活跃使用不掉线，停用满 3 天才失效。管理页不做轮询，不存在频繁重签的开销问题。

**密钥复用的判断**：session 与 `client_secret` 加密共用 `PPTX2PDF_SECRET_KEY`，不派生子密钥。密钥用途分离是通行做法，但在本威胁模型下收益为零——能读到 `SECRET_KEY` 的攻击者已可直接解密 `client_secret`，伪造 session 是更绕的路径。代码中注释说明此判断，避免后人误以为是疏忽。

### Cookie 属性

| 属性 | 取值 | 理由 |
|---|---|---|
| `HttpOnly` | 固定 `true` | 无条件正确 |
| `SameSite` | 固定 `Strict` | 管理端点无跨站需求 |
| `Secure` | 由 `PPTX2PDF_ADMIN_COOKIE_SECURE` 控制，**默认 `false`** | 当前部署是 `http://<host>:18993`，非 HTTPS |

`Secure` 默认 `false` 是刻意的：在 HTTP 部署下强制 `Secure` 会让浏览器根本不回传 cookie，表现为「登录成功但立刻掉线」，且排查困难。README 必须写明「切到 HTTPS 后应改为 `true`」。

### 暴力破解

口令错误时固定延迟 1 秒再返回，不做账户锁定。自用场景下锁定的唯一效果是把自己锁在门外。

## 5. 凭证配置的数据流

### 字段

| 字段 | 读 | 写 | 说明 |
|---|---|---|---|
| `tenant_id` | 明文回显 | 必填 | |
| `client_id` | 明文回显 | 必填 | |
| `client_secret` | **绝不回显** | 首次必填；修改时留空表示沿用 | |
| `site_id` | 明文回显 | 必填 | |
| `drive_path` | 明文回显 | 默认 `pptx2pdf-staging` | |

读取配置时返回四个明文字段加一个 `secret_configured: bool`。**不返回密文，也不返回解密后的值**——解密回显等于把凭证明文发到浏览器，那么加密存库这件事本身就失去意义。前端对该字段显示「已配置（不回显）」。

由此产生一条规则：**修改时 `client_secret` 留空即沿用已存的值**。自检因此需要处理两种输入——表单给了新 secret 就用新的，留空则从库中解密出旧的使用。首次配置（库中无行）时该字段必填，留空直接返回 422。

### 先测后存

保存请求的处理顺序是：解析入参 → 补全 secret（留空则取库中旧值）→ 跑完五步自检 → **全绿才写库**。任何一步失败则返回结构化诊断，数据库一个字节都不动。

这条规则是刻意的。若允许先存后测，配错的凭证会留在库里，而三期的引擎每次转换都去读它——期间所有走 Graph 的任务都会失败，且失败原因（一个 Graph 原始报错）与「你配错了」之间隔着数层抽象。先测后存把这个窗口彻底关闭。

代价是无法「先存着回头再调」。自用场景下此代价可接受，故**不提供跳过自检的旁路**。

## 6. 连通性自检

### 五个检查点

| # | 检查 | 排除的错误 |
|---|---|---|
| 1 | 取 access token | 租户 ID 错、client_id 错、secret 错或已过期（AADSTS 错误码可区分） |
| 2 | 访问目标 drive | 404 → `site_id` 错；403 → 权限未授予或管理员同意未点 |
| 3 | 上传内置 pptx | 403 → 写权限不足；`drive_path` 不存在 |
| 4 | 转换为 PDF | 转换接口可用性；顺带验证产物确为该文件 |
| 5 | `permanentDelete` | 已知权限坑——`Sites.Selected` 不足，需 `Files.ReadWrite.All` 或 `Sites.ReadWrite.All` |

**每步独立报告成败与诊断**，前端渲染为清单、每步一个状态。不采用「失败即返回单条错误」的形式，那样使用者仍需自行推断卡在何处。

### 失败清理

第 3 步之后的任何失败，都必须删除已上传的中转文件；清理失败不得覆盖原始错误。这与三期 `merge_pdfs`、`GraphEngine._cleanup` 采用同一套写法（清理动作包在 `try/except OSError: pass` 或等价结构中，异常只记日志）。

### 不复用 GraphEngine

自检独立实现 HTTP 调用，不复用 `GraphEngine`。

理由：`GraphEngine` 的错误处理是**刻意归一**的——将各类失败收敛为 `ConversionFailed` / `EngineUnavailable` / `ConversionTimeout`，以便流水线统一处理。自检的需求恰好相反：**尽可能区分错误**，并需知道卡在第几步、Graph 返回的原始 error code 为何。复用只有两条路：改造 `GraphEngine` 使其暴露更多细节（污染一个已为转换场景优化好的接口），或在自检中解析归一后的中文消息字符串（脆弱）。

代价是两处 Graph HTTP 调用代码并存。可共用纯函数（URL 拼装），错误处理各自实现。判断依据是诊断价值高于这部分重复的维护成本。

### 内置测试文件

`backend/app/assets/selftest.pptx`：单页，数 KB，页面印一行可识别文字用于确认转换产物确为该文件。

上传到 SharePoint 时使用固定前缀 `pptx2pdf-selftest-` 加随机后缀命名，以便进程在上传与删除之间被杀时，能人工识别并清理残留。

由 `scripts/make_selftest_pptx.py` 配合 python-pptx 生成一次后提交入库。**python-pptx 仅在 `requirements-dev.txt` 中，不是生产依赖**，故运行时直接读取该文件，不在运行时生成。将来需要修改时重跑脚本即可。

## 7. 数据模型

**无变更。** 三期已建的 `GraphCredential` 单行表（`id` 恒为 1，`client_secret_encrypted` 加密存储）直接使用。`Task.user_id` 继续留空。

## 8. API 契约

四个端点，全部挂在 `/api/admin` 前缀下。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/admin/login` | 无 | 入参 `{password}`；成功设置 cookie 并返回 204 |
| POST | `/api/admin/logout` | **不需要** | 无条件清除 cookie，返回 204 |
| GET | `/api/admin/graph-credentials` | 需要 | 返回四个明文字段 + `secret_configured` |
| PUT | `/api/admin/graph-credentials` | 需要 | 先自检后保存；成功返回自检结果，失败返回诊断且不写库 |

错误码：

| 情况 | 状态码 | 错误码 |
|---|---|---|
| 未登录或 cookie 失效 | 401 | `ADMIN_UNAUTHORIZED` |
| 口令未配置 | 503 | `ADMIN_NOT_CONFIGURED` |
| 口令错误 | 401 | `ADMIN_BAD_PASSWORD` |
| 首次配置缺 secret | 422 | `VALIDATION_ERROR`（复用现有） |
| 自检失败 | 422 | `GRAPH_SELFTEST_FAILED`，body 含每步状态 |

自检结果的结构：

```json
{
  "ok": false,
  "steps": [
    {"step": "token", "ok": true, "detail": null},
    {"step": "drive", "ok": true, "detail": null},
    {"step": "upload", "ok": false, "detail": "403 Forbidden：应用缺少对该站点的写权限"},
    {"step": "convert", "ok": null, "detail": "未执行"},
    {"step": "delete", "ok": null, "detail": "未执行"}
  ]
}
```

`ok: null` 表示因前序步骤失败而未执行，与 `false`（执行了但失败）区分。

`logout` 不要求鉴权是刻意的：它的语义是「清掉浏览器上的 cookie」，在 cookie 已过期时同样应该成功。若要求鉴权，过期后点登出会得到 401，而用户想做的事恰恰是清理这个失效状态。

## 9. 前端

`App.tsx` 按 `window.location.pathname` 分发：`/admin` 渲染 `AdminPage`，其余渲染现有上传界面。**不引入路由库**——只有两个页面，一个 `pathname` 判断足够，`react-router` 是不必要的依赖。

Nginx 需确认 `/admin` 落到 SPA fallback（`try_files ... /index.html`），二期配置若未覆盖则补上。

`AdminPage` 两个状态：未登录显示口令输入框；已登录显示配置表单与自检结果清单。沿用现有 `--c-*` 设计令牌与双主题机制，不新增视觉风格。

自检进行中禁用保存按钮并显示进度——五步真实网络调用可能耗时若干秒，无反馈会被误认为卡死。

## 10. 新增配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `PPTX2PDF_ADMIN_PASSWORD_HASH` | 无（未配置则管理入口 503） | `scrypt:<salt>:<hash>`，分隔符不能用 `$`，见 §4 |
| `PPTX2PDF_ADMIN_COOKIE_SECURE` | `false` | 切 HTTPS 后应改 `true` |
| `PPTX2PDF_ADMIN_SESSION_DAYS` | `3` | 会话有效期，滑动刷新 |

三项均需进 `.env.example` 与 README 的关键配置表。

## 11. 测试策略

沿用三期的判断标准：**这段逻辑能否在没有 Azure 账号的机器上运行？能则写测试。**

| 写测试 | 不写测试 |
|---|---|
| 口令哈希校验、cookie 签发/验证/过期/滑动刷新 | 自检的真实 HTTP 调用（留四期真机验证） |
| 配置读写：secret 不回显、留空沿用旧值、首次必填 | |
| 自检的诊断映射（HTTP 状态码与 AADSTS 码 → 诊断文本），用构造的假响应 | |
| 前端：表单逻辑、自检结果渲染、登录态处理 | |

### 接线守护

本项目在「函数写了但无人调用」这一模式上反复出现问题，三期依靠跨任务变异测试才补齐。四期以下三条必须有测试守护——删除对应调用即应有测试变红：

- 「先测后存」：删除保存路径中的自检调用
- cookie 滑动刷新：删除重新签发那一行
- 「留空沿用旧 secret」：改为留空即清空

### openapi 漂移守卫

补一条测试：重新运行 `python -m scripts.dump_openapi` 后 `git diff` 应为空。这是三期终审留下的缺口——Task 8 曾漏过一次快照重新生成，靠人工执行才发现。四期新增三个端点，正好一并补上。

## 12. 错误处理

- 未登录访问管理端点 → 401 `ADMIN_UNAUTHORIZED`
- 口令未配置 → 503 `ADMIN_NOT_CONFIGURED`（与 `GRAPH_NOT_CONFIGURED` 同构）
- 自检失败 → 422 加结构化每步状态，库不变
- 自检中途的网络异常 → 归入对应步骤的 `detail`，不作为裸异常抛出

## 13. 部署与文档

- `.env.example` 补三个新变量及口令哈希生成命令
- README 补「管理入口」一节：如何设口令、如何登录、五步自检各自的含义、`Secure` 属性与 HTTPS 的关系
- README 的「已知限制」中，「Graph 通道在四期管理页面上线前不可达」一条需改写——本期交付后凭证**有了**写入路径，但通道是否真正可用取决于管理员是否已完成配置。改为说明「需先在 `/admin` 完成配置并通过自检，未配置时 Graph 引擎返回 `GRAPH_NOT_CONFIGURED`」
- 重新生成 `openapi.json`

## 14. 已知限制与后续

- **口令为单一凭据，无找回机制。** 遗忘只能改环境变量后重启。自用场景可接受。
- **无审计日志。** 谁在何时改了凭证不留痕。单人使用无意义，多人时需补。
- **自检会在租户中短暂产生一个文件。** 正常路径与失败路径均会删除，但若进程在两者之间被杀，可能残留。文件名带固定前缀便于人工识别。
- 账号体系、配额、风控在真正开放使用时另行立项。总体设计 §9 关于风控的既定决策届时仍然适用。
