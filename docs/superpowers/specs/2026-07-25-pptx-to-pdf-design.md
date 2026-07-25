# pptx → PDF 转换站点 · 设计文档

日期：2026-07-25
状态：一期设计已定，二~四期为架构预留

---

## 1. 背景与目标

课程讲师只发布 pptx。直接导入 GoodNotes / OneNote 会出现排版错位、内容出屏幕、图片叠加。根因是**字体替换**：pptx 只存字体名不存字形，渲染端字体缺失时替换，替换字体的字符宽度不同导致换行位置改变，文字撑出占位符压到下方图片上。

目标是自建一个 web 站点做高保真 pptx → PDF，摆脱对本机 MS PowerPoint「Acrobat PDF: 生成PDF」的依赖，使 iPad 与手机可直接获取可用于笔记的 PDF。

**硬性质量要求**：输出 PDF 必须保留矢量与可选中文本层。GoodNotes 的搜索、OneNote 的 OCR 都依赖它，栅格化输出等于交付失败。

## 2. 总体架构（四期全景）

```
┌──────────────┐   分片上传    ┌──────────────┐
│  React SPA   │ ───────────▶ │  FastAPI     │
│ 三端响应式    │ ◀─────────── │  上传/任务API │
└──────────────┘   状态轮询    └──────┬───────┘
                                      │
                              ┌───────▼────────┐
                              │ ConversionEngine│  ← 可插拔接口
                              │   (抽象基类)     │
                              └───────┬────────┘
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
            PlaceholderEngine  LibreOfficeEngine   GraphEngine
                （一期）           （二期）           （三期）
```

分期边界：

| 期 | 交付 |
|---|---|
| 一期 | 前端三端 UI + 分片上传前后端全链路 + 元信息解析 + 任务落库 + 占位 PDF |
| 二期 | LibreOffice 引擎（主力通道，无页数与超时限制） |
| 三期 | Graph 引擎（高保真，受限于 100 页 / 45 秒，仅小文件）+ 转换切片与合并 |
| 四期 | 账号、配额、风控、管理面板 |

**引擎定位说明**（与直觉相反，需要牢记）：LibreOffice 不是「Graph 挂掉时的兜底」，而是超长 deck 的**主力**。Graph 受 100 页上限与 45 秒同步超时约束，恰好转不了课程 slides 这类一两百页的文件，因此它是后加的「小文件高保真增强」。详见 `graph-pdf-conversion-limits` 记忆条目。

## 3. 一期范围

**做**：

- React 三端响应式 UI，全量 backdrop-filter 液态玻璃风格
- 自实现分片上传协议，前后端全链路，支持断点续传与乱序并发块
- 上传完成后解析 pptx 元信息（页数、幻灯片尺寸、字体清单）
- 任务落库，完整状态机
- 生成**占位 PDF**：页数与尺寸真实，每页标注「引擎待接入」
- OpenAPI 契约固化

**不做**：任何真实转换、账号、配额、管理面板、队列中间件、转换切片。

**关键约束**：前端从第一天起就按**异步轮询**模型编写。占位引擎虽然瞬时返回，也必须走完 `pending → parsing → queued → converting → done` 全状态机，否则二期接入真引擎时前端要返工。

## 4. 技术选型

| 层 | 选择 | 理由 |
|---|---|---|
| 前端 | React + Vite + TypeScript | 生态最大，玻璃拟态与动画参考最多；纯 SPA，不上 Next.js（后端是 Python，SSR 徒增复杂度） |
| 后端 | Python 3.12 + FastAPI | `python-pptx` / `pypdf` / `reportlab` 生态决定性，后续动画分步与书签生成也集中在 Python 侧 |
| 数据库 | SQLite + SQLAlchemy | 自用单机足够；经 ORM 抽象，四期需要时可迁 PostgreSQL |
| 异步 | FastAPI BackgroundTasks | 一期无需真队列；二期起换 Redis + ARQ，接口不变 |
| PDF 生成 | reportlab | 仅一期占位用 |

## 5. 组件划分与边界

```
backend/
  app/
    main.py              FastAPI 装配
    config.py            配置（路径、限额、块大小）
    db.py                SQLAlchemy 会话
    models.py            Upload / Task 表
    schemas.py           Pydantic 请求响应模型
    api/
      uploads.py         分片上传四端点
      tasks.py           任务查询与结果下载
    services/
      chunk_store.py     块落盘、已收块查询、拼装（纯文件操作，不访问 DB）
      pptx_probe.py      元信息解析（不加载整包）
      engines/
        base.py          ConversionEngine 抽象基类
        placeholder.py   一期实现
    errors.py            统一错误码
```

各单元职责边界：

- **`chunk_store`** — 只管字节。接收块、列举已收块、按序拼装。不认识 pptx，也不认识数据库。
- **`pptx_probe`** — 只管解析。输入一个已落盘的文件路径，输出元信息或抛解析错误。不认识 HTTP。
- **`ConversionEngine`** — 只管转换。输入 pptx 路径与元信息，输出 PDF 路径。不认识数据库。
- **API 层** — 只管编排与 HTTP 语义，不含业务逻辑。

这样二期加 LibreOffice 引擎只需新增 `engines/libreoffice.py` 与一行注册，其余文件不动。

## 6. 关键设计细节

### 6.1 元信息解析不能用 python-pptx 全量加载

`python-pptx.Presentation()` 会把整个包的所有 part 读入内存。用户的真实文件是**单节课 ~80MB、半学期打包 ~500MB**，其中绝大部分是嵌入媒体，全量加载会吃掉数 GB 内存。

因此 `pptx_probe` 用 `zipfile` **只读需要的条目**：

- 数 `ppt/slides/slide*.xml` 的条目数 → 页数
- 读 `ppt/presentation.xml` 的 `<p:sldSz cx= cy=>` → 幻灯片尺寸（EMU，转 pt 时除以 12700）
- 扫 `ppt/slideMasters/` 与各 slide XML 的 `typeface=` 属性 → 字体清单（供二期做缺字体预警）

只读条目头与少量 XML，内存开销与文件大小无关。

### 6.2 分片上传协议

块大小 **5 MiB**，客户端并发 **3** 块。500MB 约 100 块。

```
POST   /api/uploads
  req  {filename, size, sha256?}
  res  {upload_id, chunk_size, total_chunks, expires_at}

PUT    /api/uploads/{upload_id}/chunks/{index}
  body raw bytes
  res  {index, received_count}

GET    /api/uploads/{upload_id}
  res  {received_indices[], bytes_received, total_chunks, status}

POST   /api/uploads/{upload_id}/complete
  res  {task_id}
```

- **断点续传**：客户端重连后先 `GET` 拿 `received_indices`，跳过已收块继续传。
- **乱序并发**：每块按 `chunks/{index:06d}.part` 独立落盘，`complete` 时按序拼装，天然支持乱序到达与重复投递（重复块幂等覆盖）。
- **`complete` 校验**：块数齐全 → 拼装 → 比对总字节数 → 若客户端提供了 sha256 则校验 → 通过后交给 `pptx_probe`。
- **会话过期**：`expires_at` 默认 24 小时，定时任务清理过期目录。

**这个协议顺带解决了 Nginx 大文件上传问题**：分片后单次请求体只有 5MB，`client_max_body_size` 设 `16m` 即可，无需为 500MB 放宽，也不必关闭 `proxy_request_buffering`。原先的大文件上传风险从架构上消失了。

### 6.3 存储布局

```
storage/
  uploads/{upload_id}/000000.part ...
  originals/{task_id}.pptx
  outputs/{task_id}.pdf
```

**唯一真相源是数据库**。文件系统只存块字节，不放 `meta.json` 之类的旁路元数据，避免双真相源在崩溃恢复时产生分歧。相应地，块存储层 `ChunkStore` 是**纯文件操作、不访问数据库**的单元，元数据读写全部由 API 层负责——这样 `ChunkStore` 可以完全脱离 DB 单测。

### 6.4 数据模型

`Upload`：`upload_id, filename, size_bytes, sha256, chunk_size, total_chunks, status, created_at, expires_at`

`Task`：`task_id, upload_id, user_id, original_filename, size_bytes, slide_count, slide_width_emu, slide_height_emu, fonts_json, status, engine, error_code, error_message, output_path, created_at, updated_at`

`user_id` 一期恒为 `NULL`，字段预留给四期。`engine` 一期恒为 `placeholder`。

状态机：`pending → parsing → queued → converting → done`，任一环节可转 `failed`。

### 6.5 尺寸与限额

- 单文件硬上限 **600 MB**（覆盖 500MB 真实场景并留余量）
- 前端拦截仅为体验，`POST /api/uploads` 必须复校验 `size`，`complete` 必须复校验实际字节数——前端限制可被绕过

### 6.6 前端结构

- **上传区**：拖拽 + 点击 + 移动端 `<input type=file>`
- **上传进度**：分片进度、瞬时速度、剩余时间估算、暂停/继续
- **任务列表**：轮询 `GET /api/tasks/{id}`，展示状态机进展
- **结果区**：页数、尺寸、下载

三端布局：桌面双栏（左上传右任务）、平板单栏宽、手机单栏紧凑。

液态玻璃按用户选择采用**全量 backdrop-filter**，不做低端设备降级。

## 7. 错误处理

统一错误码，前端按码显示中文提示：

| 码 | 场景 |
|---|---|
| `UPLOAD_SESSION_NOT_FOUND` | upload_id 不存在或已过期 |
| `UPLOAD_SESSION_EXPIRED` | 会话超时 |
| `UPLOAD_SIZE_EXCEEDED` | 超过 600MB |
| `UPLOAD_INCOMPLETE` | complete 时块数不齐 |
| `UPLOAD_CHECKSUM_MISMATCH` | 拼装后字节数或 sha256 不符 |
| `PPTX_INVALID_ZIP` | 不是合法 zip |
| `PPTX_NOT_PRESENTATION` | 缺少 `ppt/presentation.xml` |
| `PPTX_ENCRYPTED` | 加密文件（检测到 `EncryptedPackage`） |
| `STORAGE_FULL` | 磁盘写入失败 |
| `INTERNAL_ERROR` | 兜底 |

块上传失败客户端自动重试 3 次（指数退避），仍失败则标记该块待重传，不中断其他块。

## 8. 测试策略

后端 pytest：

- 分片乱序到达后拼装正确
- 断点续传：中断后 `GET` 返回正确的已收块集合，续传后拼装结果与原文件一致
- 重复投递同一块幂等
- `complete` 时块数不齐 → `UPLOAD_INCOMPLETE`
- 字节数/sha256 不符 → `UPLOAD_CHECKSUM_MISMATCH`
- 超限文件在 `POST /api/uploads` 即被拒
- 非 zip、非 pptx、加密 pptx 分别返回对应错误码
- `pptx_probe` 对已知页数的样本文件解析结果正确
- 占位 PDF 页数与尺寸等于解析结果

前端 vitest：分片切分边界（含最后一块不足 5MB）、重试与退避逻辑、进度计算。

端到端：生成一个约 600MB 的合成 pptx，跑完整链路并断线一次验证续传。

## 9. 非一期的设计预留

以下不实现，但接口与数据模型已为其留位：

- **转换切片**（三期）：长 deck 用 `python-pptx` 拆成 ≤80 页子文件分别送 Graph，再用 `pypdf` 合并恢复页序
- **引擎路由**（三期）：判据用**页数**而非字节数；≤80 页且 <50MB 走 Graph，否则 LibreOffice；Graph 429 或超时自动降级
- **Graph 存储中转**（三期）：租户内专用 SharePoint 文档库 + Azure AD 应用（client credentials，`Sites.Selected`），不碰个人 OneDrive；转完立即删且必须 `permanentDelete`，否则两级回收站仍占配额
- **字体全家桶**（二期）：LibreOffice 容器需装齐中文字体与 MS 核心字体，这是保真度的 80%
- **风控**（四期）：设备指纹作为**加权信号**而非硬阻断（降配额、加排队惩罚、触发复核），避免公用机房同型号设备指纹碰撞造成误伤；IP 维度放宽，校园网 NAT 后大量用户共享出口
- **D 类后处理**（最后）：动画分步展开成多页、PDF 书签大纲、页边距重映射

## 10. 二期开工前的既定决策

以下在一期收尾时拍板，二期用 writing-plans 生成实施计划时直接采纳，不必重新讨论。

### 10.1 会话模型：刷新即重置，不做跨刷新的续传恢复

采用在线转换站的通行心智模型——**一次页面加载 = 一个 session，刷新即全部重置**。

这条决策把一期终审提出的「断点续传在 UI 上不可达」从缺陷降级为**非需求**：用户不会指望刷新后还能接着传，因此不实现 `localStorage` 持久化 `upload_id`、不实现「检测到未完成的上传」提示。`uploadClient` 的 `resumeUploadId` 参数保留，但定位调整为**session 内网络抖动的重试路径**，不是跨会话恢复入口。

**多标签页不做适配**：假定用户只开一个标签页；多开时各标签页独立运行、互不感知，属可接受行为。因此 session 状态**纯前端**保存（内存即可），**不**在 `Upload`/`Task` 表增加 `session_id` 字段，后端无需任何改动。

### 10.2 文件身份用轻量指纹，不用完整密码学哈希

session 内判断「用户是不是又选了同一个文件」，用 **`size` + `lastModified` + 首尾各 1MB 的 sha256** 作为指纹，而非对整个文件算哈希。

**Why:** Web Crypto 的 `crypto.subtle.digest()` 没有流式接口，要求一次性传入完整 `ArrayBuffer`。500MB 文件意味着先 `await file.arrayBuffer()` 全读进内存再算——移动端 Safari 基本必崩，桌面也是数秒到数十秒卡顿，且发生在用户点击上传之后、进度条出现之前，表现为白屏等待。轻量指纹是毫秒级，要构造碰撞得同时凑齐大小、修改时间和首尾内容。

**这两件事的强度需求不同，不要混为一谈：**

| | 「是不是同一个文件」 | 「传上来的字节对不对」 |
|---|---|---|
| 性质 | UX 去重，防手滑重复选择 | 完整性校验，需密码学强度 |
| 误判代价 | 多传一次 | 数据损坏 |
| 方案 | 轻量指纹（前端，毫秒级） | 后端 `_sha256_of()` 流式校验（**已实现**） |

因此**不引入 `hash-wasm` 之类的 WASM 流式哈希依赖**，也**不把后端现有的 sha256 改成 sha512**——后者要动 schema 字段名、正则 pattern（64→128 hex）、`_sha256_of` 及相关测试，成本非零而收益为零：sha256 已是 2^128 量级抗碰撞，对文件身份判定绰绰有余。

### 10.3 任务超时：显性要求用户重传，不做自动恢复

任务卡在非终态（`pending`/`parsing`/`queued`/`converting`）超过阈值时，前端**停止轮询并提示用户重新上传**，不尝试自动恢复或后台重试。与 10.1 的刷新即重置一致。

这同时收口了一期终审的 I3：当前 `useTaskPolling` 的轮询没有终止条件，一个因进程重启而永久停在 `pending` 的任务会被每秒轮询到天荒地老。

### 10.4 一期终审遗留、需在二期处理的项

按优先级排列，前两条建议在写任何二期新代码**之前**先做——现在改是几行，等二期实现堆上去再改要动一大片：

1. **引擎选择从 HTTP 层挪到 `pipeline.py` 的 `probe()` 之后。** 现状是 `uploads.py` 写死 `engine="placeholder"`，但按 §9 的引擎路由，判据是**页数**，而页数要解析完才知道。不挪这个点，本文档 §5 声称的「二期加 LibreOffice 只需新增一个文件与一行注册」不成立。建议新增 `services/engine_router.py::select_engine(meta) -> str`。
2. **`ConversionEngine` 补超时与错误族。** 签名加 `timeout_s`，`errors.py` 加 `ConversionFailed` / `ConversionTimeout` / `EngineUnavailable`。Graph 是硬性 45 秒超时、LibreOffice 是几十秒外部进程，当前 `convert(src, meta, dest) -> None` 没有任何地方能挂超时控制；且所有引擎异常现在都被 `pipeline` 归一成 `INTERNAL_ERROR`，前端会把原始异常字符串直接显示给用户。
3. **`BackgroundTasks` 换真队列**（Redis + ARQ）。当前是单进程内存队列，进程重启丢任务。`run_task(task_id)` 的签名已为此设计（只吃 id、自开 session、路径从 settings 取），换队列时函数体几乎不动，只需替换投递方式。
4. **`originals/` 与 `outputs/` 的保留策略。** 目前**没有任何删除路径**，磁盘随使用无限增长（单节课 80MB、半学期 500MB，每次转换永久留下原文件 + 输出 PDF）。落地后 §7 的 `TASK_NOT_READY` 才有条件换成语义准确的 `RESULT_EXPIRED`(410)。
5. **`put_chunk` 改流式读取。** 当前先 `await request.body()` 再校验大小；`Content-Length` 缺失（`Transfer-Encoding: chunked`）时该调用无上限。生产有 Nginx `client_max_body_size 16m` 兜底，但后端裸跑无保护。
6. **业务错误码补进 `openapi.json` 契约。** 当前快照每个端点只声明 200 和 FastAPI 默认的 422，全部业务错误码（`UPLOAD_SESSION_NOT_FOUND` / `UPLOAD_SIZE_EXCEEDED` / `STORAGE_FULL` 等）都不在契约里，且 `VALIDATION_ERROR` 的实际返回形状与声明的 `HTTPValidationError` 不一致。建议给路由加 `responses={...}` 声明，并在 CI 跑 `dump_openapi.py && git diff --exit-code`。
7. **`Settings` 加 `env_prefix="PPTX2PDF_"`。** 当前 `CHUNK_SIZE`、`DATABASE_URL` 这类通用环境变量名容易与同机其他服务撞车。
8. **`deploy/nginx.conf.example` 的 `listen 443 ssl http2;` 在 nginx ≥1.25.1 已废弃**（应为 `listen 443 ssl;` + `http2 on;`），且缺 `ssl_certificate` 指令，照抄起不来。
