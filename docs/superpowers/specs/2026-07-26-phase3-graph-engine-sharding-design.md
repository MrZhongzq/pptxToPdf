# 三期：Microsoft Graph 引擎与转换切片 · 设计文档

日期：2026-07-26
上游：`2026-07-25-pptx-to-pdf-design.md`（总体设计，§9 为三期预留）

---

## 1. 范围

**做**：Microsoft Graph 转换引擎、zipfile 流式切片与合并、分片任务模型、凭证加密存储、引擎路由改造。

**不做**：Azure 门户侧的配置（留四期，见 §2.2）、管理页面（四期）、前端 i18n / AMOLED / 玻璃动画（见记忆 `pending-backlog`）、p:timing 动画分步（独立立项）。

**交付方式**：三期**不部署**。质量关口是代码审查加单元测试，见 §9。

## 2. 前提与约束

### 2.1 Graph 的硬限制

| 限制 | 值 | 来源 |
|---|---|---|
| 页数 | 超过 100 页报 `OfficeConversion_Fatal` | 社区实测 |
| 同步超时 | 约 45 秒 `General_Timeout`，服务端硬限制，不可覆盖 | 社区实测 |
| 文件大小 | 文档标称 100MB，**实测 50MB 即失败** | 社区实测 |
| 异步 API | **不存在** | — |

我们的阈值取 **80 页 / 40MB 每片**，对两个实测值留余量。

### 2.2 Azure 凭证由四期配置

用户是 M365 E3 租户管理员，但明确要求**凭证通过四期的管理页面配置，而不是写在 `.env` 里**。因此三期的引擎不能从 `Settings` 读死凭证，必须从数据库读取，读不到时报明确的「引擎未配置」。

三期交付时 Azure 侧一次都不会被调用——这是 §9 测试策略的直接原因。

### 2.3 不做静默回退

用户显式选了 Graph，就**不允许**因为超限而偷偷改用 LibreOffice。静默回退等于欺骗：用户以为拿到的是 Graph 的高保真结果，实际是 LibreOffice 转的。超出能力时一律明确报错。

## 3. 任务模型与分片协调

### 3.1 数据模型

新增 `TaskShard` 表，主任务不再直接对应一次转换：

```
Task （新增一列）              TaskShard （新表）
  shard_total: int | None        shard_id     str  PK
                                 task_id      str  FK → Task，加索引
                                 index        int  0-based，决定合并顺序
                                 page_start   int  原 deck 的页范围，1-based 闭区间
                                 page_end     int
                                 status       str  pending|converting|done|failed
                                 output_path  str | None
                                 error_code   str | None
                                 error_message str | None
                                 created_at / updated_at
```

**`Task` 上不存 `shard_done` 计数。** 已完成片数由查询时现算（`SELECT count(*) FROM task_shards WHERE task_id=? AND status='done'`）：多个分片并发完成时自增同一行计数在 SQLite 上要么加锁要么丢更新，而 `TaskShard.status` 本来就是这件事的唯一真相源，冗余一个计数只会引入不一致的可能。

`shard_total = None` 表示不切片，走二期的原路径。**LibreOffice 永远不切片**——它没有页数与体积限制，切片只服务于 Graph。

### 3.2 状态机扩展

```
pending → parsing → queued → splitting → converting → merging → done
                              └─── 不切片时跳过这两个 ───┘
```

任一环节可转 `failed`。加 `splitting` / `merging` 而不是全塞进 `converting`，因为它们是真实且耗时的阶段——切一份 83MB 的 deck 要读写几十 MB，合并同理。用户看到「合并中」比看到 `converting` 长时间不动要好。

前端的 `STATUS_LABEL` 与 `useTaskPolling` 的 `TERMINAL` 集合要同步更新。

### 3.3 队列协调用 RQ 的 Dependency

```python
shard_jobs = [queue.enqueue(convert_shard, sid) for sid in shard_ids]
queue.enqueue(
    merge_shards, task_id,
    depends_on=Dependency(jobs=shard_jobs, allow_failure=True),
)
```

**`allow_failure=True` 是必须的。** 默认的 `False` 会让任一分片失败时汇总 job **永远停在 `DeferredJobRegistry` 里不执行**，任务卡死在 `converting`，只能等孤儿回收器 45 分钟后收尸。设成 `True` 后汇总 job 总会执行，由它自己检查所有分片状态再决定成败。

`Dependency` 支持多 job 依赖，1.11.0 引入，RQ 2.0 可用。

两个 job 函数的职责边界：

- **`convert_shard(shard_id)`** —— 只管一片：读分片 pptx、调 Graph、写分片 PDF、更新该 `TaskShard` 的状态。**不碰主任务的状态**，避免多个并发分片同时写同一行。
- **`merge_shards(task_id)`** —— 汇总：检查所有分片状态，全成功则按序合并、校验页数、写 `Task.output_path`、置 `done`；有任一失败则置 `failed` 并汇总错误码。无论哪条路径都在 `finally` 清理分片目录（§4.6）。

主任务的状态只由 `merge_shards` 改写。`convert_shard` 只写自己那行 `TaskShard`，因此多个分片并发运行时不存在对同一行的写竞争。

### 3.4 失败语义

**部分失败一律整体失败。** 10 片里 9 片成功也不合并——与二期「页数不符标 `failed` 而非交付缺页 PDF」是同一条原则。合并出一份缺了中间几页的 PDF，比明确报错糟糕得多。

**分片不做 job 层重试。** Graph 的 429 限流与 5xx 由引擎内部退避处理（那是 HTTP 层的事）；job 层重试会覆盖掉已记录的错误码，与二期判断一致。

## 4. zipfile 流式切片

### 4.1 为什么不用 python-pptx

`python-pptx.Presentation()` 会把整包读进内存。用户的半学期打包是 500MB 量级，加载可能吃 2–3GB。即使把 worker 限额提到 8G（见 §4.5），也只是把天花板往上挪，而不是消除它。

流式切片的内存开销等于**最大单个 part**（一张图片，几 MB），与整包大小无关。这是选它的全部理由——所以实现时**绝不能图省事先 `read()` 整个 part 再写**。

### 4.2 包结构

```
[Content_Types].xml              每个 part 的 MIME 声明
_rels/.rels                      包级 rels → presentation.xml
ppt/presentation.xml             sldIdLst 按序引用各 slide
ppt/_rels/presentation.xml.rels  rId → slide / master / theme
ppt/slides/slideN.xml
ppt/slides/_rels/slideN.xml.rels slide → media / layout / notesSlide
ppt/slideLayouts|slideMasters|theme|media/…
```

### 4.3 算法

1. 读 `ppt/_rels/presentation.xml.rels`，建 `rId → target` 映射
2. 读 `ppt/presentation.xml` 的 `sldIdLst`，得到**有序**的 `(sldId, rId)` 列表
3. 按页范围确定本片保留哪些 slide
4. 从每个保留 slide 的 rels **递归收集依赖**（slide → layout → master → theme，以及 media）
5. 写新 zip：只复制收集到的 part，重写 `presentation.xml`（只留保留的 `sldId`）、`presentation.xml.rels`、`[Content_Types].xml`

### 4.4 三个关键决定

**不重编号 rId，只删条目。** 保留的 slide XML 内部有 `r:embed="rId3"` 这类引用；重编号就要同步改写每个 slide 的 XML 正文，那是引入 bug 的捷径。只从 rels 里删掉不需要的条目，保留的 rId 原样不动，slide 正文一个字节都不用改。

**逐 part 流式复制。** `zin.open(name)` 读、`zout.open(name, "w")` 写，分块搬运。

**notesSlide、comments、`docProps/thumbnail.jpeg` 直接丢弃。** 讲稿备注不进 PDF，带着只会增加 part 数量和出错面。

### 4.5 阈值与兜底

分片数按 `avg_page_size = total_size / slide_count` 估算，取 `pages_per_shard = min(80, floor(40MB / avg_page_size))`。

这个估算对媒体分布不均的 deck 不准（有的页一张大图、有的页只有标题），所以**切完要实测每片体积**：

- 某片仍超 40MB 且页数 > 1 → 对该片再切一轮（**最多再切一轮，不递归**）
- 第二轮之后**仍有片超 40MB**（无论剩几页）→ **停止再切，明确报错** `SHARD_TOO_LARGE`

不递归是有意的：递归到最后必然收敛到「单页仍超限」这个无解状态，只是多绕几圈、多写几十 MB 临时文件。两轮足以处理「媒体分布不均导致估算偏差」这个真实场景；两轮还搞不定的说明有单页级别的超大媒体，再切也没用。

`SHARD_TOO_LARGE` 的消息要带上**是哪一页范围、实际多大、阈值多少**，并建议改用 LibreOffice——这是 §2.3「不静默回退」的落点，用户得知道为什么以及下一步怎么办。

**worker 内存限额从 3G 提到 8G。** 流式切片本身不需要，但合并阶段 pypdf 要把多份 PDF 读进内存，几十 MB × 若干片仍然吃内存。24GB 机器、2 个 worker × 8G = 16G，留 8G 给 api、redis 与系统。

### 4.6 分片文件的存储与清理

新增一层目录，与 `originals/` `outputs/` 平级：

```
storage/shards/{task_id}/
    000.pptx  000.pdf
    001.pptx  001.pdf
    …
```

分片 pptx 在 `splitting` 阶段写入，分片 PDF 在各自的 `convert_shard` job 里写入。

**整个 `shards/{task_id}/` 目录在汇总 job 的 `finally` 里删除**，不论合并成功还是失败——与二期 `drop_original` 同一个原则：中间产物没有诊断价值，而它的体积是原文件的两倍（分片 pptx 加分片 PDF）。

`retention.purge_expired_outputs` 同时扩展到扫描 `shards/` 下的孤儿目录（汇总 job 从未执行时留下的），按目录 mtime 判过期。否则一次 worker OOM 就会留下一份永不回收的几十 MB 残骸。

### 4.7 合并

用 `pypdf` 按 `TaskShard.index` **升序**合并。合并后校验总页数等于 `Task.slide_count`——这是二期那条校验的延续。

**页序是三期最危险的失败模式**：顺序错了页数完全正确，任何页数校验都抓不到，用户可能翻到考前复习才发现第 30 页跑到了第 10 页。§9 的测试重点就在这里。

## 5. Graph 引擎

新增 `backend/app/services/engines/graph.py`，替换二期那个抛 `EngineUnavailable` 的桩。

### 5.1 认证

client credentials 流：

```
POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
  grant_type=client_credentials
  client_id / client_secret
  scope=https://graph.microsoft.com/.default
```

token 有效期约 1 小时，在引擎实例内缓存并在过期前 5 分钟刷新。

**但要清楚这个缓存在分片场景下几乎不起作用**：每个分片是独立的 RQ job，每个 job 各自调 `get_engine()` 拿到一个新实例（二期定的惰性构造），而 RQ 的每个 job 还跑在 fork 出的独立子进程里——12 片就是 12 次 token 请求。

这是可接受的：token 端点响应在百毫秒级，相比每片几十秒的转换可以忽略，而共享 token 要引入跨进程缓存（Redis 存 token）才做得到，那等于为了省 12 次快请求引入一个新的密钥存储面。**不做进程级或 Redis 级的 token 缓存。**

### 5.2 转换流程

每个分片：

1. **上传到 SharePoint 中转库**。片大小 ≤40MB，超过 4MB 就必须用 `createUploadSession` 分片上传：
   `POST /sites/{site_id}/drive/root:/{path}:/createUploadSession`，然后按返回的 `uploadUrl` 分块 `PUT`。
2. **转换**：`GET /sites/{site_id}/drive/items/{item_id}/content?format=pdf`
   **返回 302**，`Location` 是几分钟内有效的预授权 URL。httpx 必须开 `follow_redirects=True`，否则拿到的是空响应体。
3. **下载 PDF** 到本地分片输出路径。
4. **清理**：见 §5.3。

### 5.3 清理与权限的冲突（已知风险）

上游设计文档 §9 写的是用 `Sites.Selected`（只授权单个站点，最小权限）。但核实发现：

- `permanentDelete`（`POST /drives/{drive_id}/items/{item_id}/permanentDelete`）在 v1.0 已 GA，**不进回收站**
- 但它文档标注的应用权限是 `Files.ReadWrite.All` 或 `Sites.ReadWrite.All`——**都是租户级宽权限**，与 `Sites.Selected` 的最小权限意图冲突
- 社区另有报告称 `permanentDelete` 有时**仍然把文件送进回收站**

三期无法实测（Azure 未配置）。设计上按以下顺序处理，四期实测后收敛：

1. **首选** `permanentDelete` + `Sites.Selected`。若权限不足会返回 403，此时
2. **降级**为普通 `DELETE`（进回收站）并**记 WARNING 日志**，提示需要定期清空回收站或改授 `Sites.ReadWrite.All`
3. 无论哪条路径，清理失败**不影响转换结果**——文件已经转好了，中转文件残留是运维问题不是转换失败。但必须记日志，否则 SharePoint 配额会被悄悄吃满（两级回收站仍占配额）

清理放在 `finally`，转换成功或失败都执行。

### 5.4 超时与退避

- 单次转换请求超时设 **50 秒**（Graph 自身 45 秒硬超时，留 5 秒网络余量），超时抛 `CONVERSION_TIMEOUT`
- **429** 读 `Retry-After` 头退避重试，最多 3 次；无该头时用指数退避 2/4/8 秒
- **5xx** 同样退避重试最多 3 次
- **4xx（除 429）** 立即失败，不重试——那是请求本身的问题，重试无意义

## 6. 凭证存储

### 6.1 数据模型

```
GraphCredential          单行表（id 固定为 1），四期管理页面 CRUD，三期只读
  id            int  PK = 1
  tenant_id     str
  client_id     str
  client_secret_encrypted  str   ← Fernet 密文
  site_id       str                SharePoint 站点 ID
  drive_path    str                中转目录，默认 "pptx2pdf-staging"
  updated_at    datetime
```

### 6.2 加密

用 `cryptography` 的 Fernet 对称加密 `client_secret`，主密钥来自新增的环境变量 `PPTX2PDF_SECRET_KEY`（32 字节 urlsafe base64）。

理由：数据库文件在 volume 里，误备份、误提交或四期开放后任何能读到该文件的路径都会泄露一个能操作 SharePoint 站点的凭证。密钥留在环境变量，DB 泄露时仍然用不了。

**代价要写进 README**：密钥丢失等于凭证全废，必须去 Azure 重新生成 client secret。

`PPTX2PDF_SECRET_KEY` 未配置时，引擎构造直接抛 `EngineUnavailable("未配置 PPTX2PDF_SECRET_KEY")`——不要用默认密钥兜底，那等于没加密。

### 6.3 三期的读取接口

`backend/app/services/graph_credentials.py`：

```python
def load_credentials() -> GraphCredentialData     # 无记录或解密失败 → EngineUnavailable
def save_credentials(data) -> None                # 四期管理页面用，三期先实现好
```

三期实现两个函数，但只有 `load_credentials` 被引擎调用。`save_credentials` 一并写好，四期直接用。

## 7. 引擎路由

`select_engine` 的自动判定分叉在三期才真正生效：

```python
def select_engine(meta, size_bytes, requested=None) -> str:
    if requested:
        return requested          # 用户显式指定，一律尊重
    if meta.slide_count <= 80 and size_bytes <= 40 * MIB:
        return "graph"            # 小文件走高保真
    return "libreoffice"
```

**自动判定不选切片路径。** 切片带来的是数十次 HTTP 往返与几分钟等待，作为默认行为太重。用户显式选 Graph 且文件超限时才切片——那是他知情的选择。

## 8. 前端改动

### 8.1 基础

- `STATUS_LABEL` 增加 `splitting`（「拆分中」）与 `merging`（「合并中」）
- `useTaskPolling` 的 `TERMINAL` 集合不变（仍是 `done` / `failed`）
- `TaskDto` 增加 `shard_total: int | None` 与 `shard_done: int`（后者由 `GET /api/tasks/{id}` 现算，见 §3.1）
- 新错误码 `SHARD_TOO_LARGE` 由既有的错误展示逻辑自动覆盖，无需特殊处理

### 8.2 长耗时任务必须一眼可辨

切片转换是数十次 HTTP 往返、几分钟到十几分钟的活。如果它和一个 12 秒转完的普通任务长得一样，用户点完就干等，体验最差。所以要在**两个时机**给出信号。

**时机一：上传前预判。** 用户选中 Graph 引擎、且文件超过 `GRAPH_MAX_SHARD_BYTES`（40MB）时，在上传区下方立刻显示提示：

> 此文件较大，Graph 通道会将其切分后分批转换，耗时可能达到十几分钟。改用 LibreOffice 通常在一分钟内完成。

这一层只能按**文件大小**判断——上传前拿不到页数（那要 probe 之后才知道）。所以它是启发式的：可能提示了最终没切片（页数少、体积恰好卡线），也可能没提示却切了（页数超 80 但体积小）。这个不准确是可接受的，它的作用是在用户还能改主意的时候给出警告，而不是精确预测。

**时机二：任务卡片的视觉区分。** `shard_total` 非空时：

- 卡片左边框用 `--c-notable`（新增的紫色语义色）4px 竖条，与普通任务一眼分开
- 状态区显示「已完成 7 / 12 片」而不只是「转换中」
- 进度条从不定长动画改为按 `shard_done / shard_total` 的确定比例

新增设计令牌 `--c-notable` / `--c-notable-soft`（浅色主题下 `#7c3aed` / `#f3ecfe`，深色下 `#a78bfa` / `#2a1f3d`），用途固定为「这个任务会比你预期的久」。

**不做倒计时或剩余时间估算。** Graph 每片的耗时受服务端排队与文件复杂度影响，波动可能达数倍，给出一个不断跳变的「剩余 3 分钟」比不给更糟。分片计数本身已经是可信的进度信号。

## 9. 测试策略

三期**不部署**，二期那套「真机跑一遍故障注入」的兜底不存在。如果也不写测试，交付的就是一份从未被执行过的代码。所以三期**恢复写测试，但只覆盖不需要 Azure 凭证的纯逻辑**：

| 逻辑 | 写测试 | 重点 |
|---|---|---|
| 页范围计算、分片数估算 | ✅ | 边界：1 页、恰好 80 页、81 页 |
| 流式切片后 media 确实被清理 | ✅ | 今天那个实验就是雏形：切一半 → 体积减半、media part 减半 |
| **合并后页序与原 deck 一致** | ✅ | **最高优先级**，见下 |
| 切片后的 pptx 能被 `pptx_probe` 正确解析 | ✅ | 证明产出的包是合法的 |
| 分片状态协调、部分失败判定 | ✅ | 9 成 1 败必须整体失败 |
| 凭证加解密 | ✅ | 含密钥缺失、密文损坏 |
| Graph HTTP 调用、429 退避、SharePoint 清理 | ❌ | 留四期真机验证 |

**页序测试为什么最高优先级**：二期那条「页数必须等于 `slide_count`」的校验能抓住缺页，但抓不住顺序错乱——顺序错了页数完全正确。测试方法是给每页写上可识别的序号文字，合并后逐页 `extract_text()` 断言序号递增。

## 10. 新增配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PPTX2PDF_SECRET_KEY` | 无（未配置则 Graph 引擎不可用） | Fernet 主密钥，32 字节 urlsafe base64 |
| `PPTX2PDF_GRAPH_MAX_PAGES_PER_SHARD` | `80` | 每片页数上限，对 Graph 的 100 页硬限留余量 |
| `PPTX2PDF_GRAPH_MAX_SHARD_BYTES` | `41943040` | 40 MiB，对实测 50MB 失败点留余量 |
| `PPTX2PDF_GRAPH_REQUEST_TIMEOUT_S` | `50` | 单次转换请求超时，Graph 自身 45 秒 |
| `PPTX2PDF_GRAPH_MAX_RETRIES` | `3` | 429 / 5xx 的退避重试次数 |

compose 的 worker `memory` 从 `3G` 改为 `8G`（§4.5）。

## 11. 不做的事

- Azure 门户侧配置（四期）
- 管理页面与凭证的写入 UI（四期，但 `save_credentials` 三期就实现好）
- 递归切片（最多再切一轮，见 §4.5）
- 混合引擎（某片走 Graph 某片走 LibreOffice）——同一份 PDF 里渲染质量不一致比整体失败更糟
- 静默回退（§2.3）
- 分片级别的 job 重试（§3.4）
- 前端 i18n / AMOLED 暗黑 / 玻璃动画（见记忆 `pending-backlog`）
- p:timing 动画分步（独立立项）
