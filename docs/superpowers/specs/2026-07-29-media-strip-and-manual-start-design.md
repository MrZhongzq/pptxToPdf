# 五期设计：内嵌媒体剥离与手动触发转换

日期：2026-07-29
状态：已定，待生成实施计划

---

## 1. 背景

四期部署到测试机后，真机测试暴露了两个问题：

**一份 83.7MB / 59 页的课件转不了。** 报 `SHARD_TOO_LARGE`：第 25 页单页就有 56MB，超过 40MB 的分片上限，而单页无法再切分。那 56MB 是一段内嵌视频——**PDF 放不了视频**，这些字节从进入系统的第一刻起就是纯浪费，却让一个本来能转的 deck 彻底转不了。

**上传完就自动开始转换，手慢就来不及选引擎。** 用户想先把文件传上去，传的过程中再决定用哪个引擎、勾哪些选项。

本期解决这两件事。同期真机还发现两个问题（上传卡住、`p:timing` 动画未分页），经用户裁决**推迟**，记录在 `.superpowers/sdd/phase5-backlog/deferred.md`。

## 2. 范围

### 做

- 转换前统一剥离内嵌视频/音频，剥离后的文件取代原文件参与后续全部判断
- 上传完成后不自动转换，新增 `ready` 状态与 `POST /api/tasks/{id}/start`
- 引擎与转换选项改由 `start` 时提交

### 不做

- 上传卡住的三个缺陷（`putChunk` 无超时、进度只在整块后更新、剩余时间用累计平均）——已记待办
- `p:timing` 动画分步展开——从未实现的独立功能，需单独立项。注意 `ConversionOptions` 里**已有** `expand_animations` 字段（一期就把契约定下来了，docstring 明写「后端目前只接收并落库，不实现任何一项」），所以那不是遗漏而是明确的占位；本期也不实现它
- 废弃 `POST /api/uploads` 的 `engine` / `options` 字段——用户明确要求保留，也许后续有用

## 3. 媒体剥离

### 丢弃的关系类型

```
http://schemas.openxmlformats.org/officeDocument/2006/relationships/video
http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio
http://schemas.microsoft.com/office/2007/relationships/media
```

PowerPoint 嵌入一段视频通常同时写 `video` 与 `media` 两条关系指向同一个 part，**两个都要丢**，漏一个文件就还留在包里。

### 实现方式：复用切片的流式重写

新增 `backend/app/services/media_strip.py`：

```python
@dataclass(frozen=True)
class StripResult:
    stripped: bool          # 是否真的删掉了东西
    removed_parts: int      # 删了几个 part
    bytes_before: int
    bytes_after: int

def strip_media(src: Path) -> StripResult   # 就地重写
```

`pptx_split.py` 已经解决过完全相同的问题——按关系类型丢 part、改写**所有** `.rels`、不碰 `presentation.xml` 的字节以保住 `mc:Ignorable`。媒体剥离是它的退化情形：不裁页，只裁媒体。

三期在这条路上踩过五轮修复（悬空 rels、内部跳转拖页、`mc:Ignorable` 丢失、正则手术对空格/命名空间前缀/非自闭合三种变体的处理），那些教训不该重走。**实现时应复用 `pptx_split` 的既有机制，而不是另写一套更简单的。**

### 接入点与取代语义

接在 `pipeline.run_task` 的 `probe(src)` **之前**。剥离后的文件**覆盖原件**，此后 `size_bytes`、`needs_sharding`、切片、转换全部基于剥离后的文件。

这样那份 83.7MB 的课件掉到约 28MB 后大概率根本不需要切片，直接走单次转换——既快又绕开了分片上限。

覆盖而非并存的理由：磁盘上只留一份，现有的 `drop_original`、清理、TTL 逻辑全都不用改。代价是转出来不对时没法在服务器上对照原件，但用户本机有原件，重传即可。

### 必须接受的后果

slide 正文里指向被删视频的 `r:id` 会**悬空**。这与三期已裁决接受的「内部跳转悬空 rId」是同一类：消费方忽略非关键内容，而不修的代价是那份 deck 根本转不了。

### 空操作的情形

不含媒体的 deck 剥离后 `stripped=False`，此时**不重写文件**，避免为零收益白做一次解压重打包。

## 4. 两段式上传

### 状态机

```
ready → pending → parsing → queued → converting → merging → done / failed
  └── 点「开始转换」后才离开 ready
```

`ready` 插在最前面，是新增状态。

### 后端改动

| 位置 | 改什么 |
|---|---|
| `uploads.py::complete_upload` | 拼装、校验、建 Task 全部保留；**不再调 `enqueue_conversion`**；Task 落 `ready` |
| 新增 `POST /api/tasks/{id}/start` | 接收引擎与选项 → 写进 Task → `enqueue_conversion` → 转 `pending` |
| `retention.py` | `ready` **不进** `NON_TERMINAL`；由新增的 `PPTX2PDF_READY_TTL_HOURS`（默认 **1** 小时）回收 |

### ready 不进孤儿回收的理由

孤儿回收器（`stale_task_minutes`，45 分钟）的语义是「转换卡住了」，它会把任务标成 `failed`「任务在服务重启前未完成」。`ready` 任务没有卡住，它只是在等人点按钮——被标 failed 是错的诊断。

### 为什么不复用 upload TTL

两者管的东西不同：

- `PPTX2PDF_UPLOAD_TTL_HOURS`（24 小时）管的是**未完成的上传会话**——分块还在传，支持断点续传。把它调短会误伤续传：大文件传到一半、暂停超过阈值再回来，会话就没了。
- `ready` 管的是**已传完、只差点按钮**的任务。重传成本相对小，可以更快回收。

所以新增独立配置项 `PPTX2PDF_READY_TTL_HOURS`，默认 **1 小时**。用户裁决：重传成本小，更快回收存储是可以接受的——机器只有 35G 可用盘，而单份原件可能 80–500MB。

回收动作：删原文件（`drop_original`）+ 把 Task 标为 `failed`，`error_code` 用新增的 `READY_EXPIRED`，消息说明「上传后一小时内未开始转换，已回收，请重新上传」。标 failed 而不是直接删 Task 行，是为了让用户在任务列表里看到发生了什么，而不是文件凭空消失。

### 一处必须挪动的兜底

`complete_upload` 现有的失败兜底里有一段：Redis 不可达导致 `enqueue_conversion` 抛错时，要显式调 `drop_original`——因为「任务永远不会入队，也就永远走不到 `run_task` 的 finally」，而那是原文件唯一的删除路径，不删就留下一份 80–500MB 的孤儿。

拆开之后 `complete` 不再入队，**这段兜底必须整体挪到 `start`**。漏挪的后果是每次 Redis 抖动都留一份大文件。

### 引擎与选项的提交时机

改由 `start` 的请求体带。这正是本期的目的——先传着，传的过程中慢慢选。

`POST /api/uploads` 的 `engine` / `options` 字段**保留不废弃**（用户决定），但 `complete` 不再从 Upload 转写 `requested_engine` 到 Task。

### 前端

`uploadFile` 传完后不再自动跳转到任务卡片，改为停在一个「已就绪」卡片：文件名、体积、页数（若已知）、引擎选择、转换选项、「开始转换」按钮。

四期那条「有风险时点确认前不发任何上传请求」的交互仍然成立，只是容量预判的决策点后移——现在用户在 `ready` 卡片上换引擎时才需要重新评估风险。

**新增状态值必须同步三处**，一处不改就会在运行时崩：

| 文件 | 改什么 |
|---|---|
| `frontend/src/lib/api.ts` | `TaskDto['status']` 联合类型加 `'ready'`（现为 7 值） |
| `frontend/src/components/TaskCard.tsx` | `STATUS` 映射表加 `ready` 条目 |
| `frontend/src/hooks/useTaskPolling.ts` | `TERMINAL` 集合——`ready` **不是**终态，不要加进去 |

四期在这里踩过一次：`STATUS[未知值]` 是 `undefined`，取 `.badge` 直接抛 `TypeError`，而仓库没有 ErrorBoundary，React 18 会卸载整棵树。`TaskCard.test.tsx` 有一个遍历全部状态的 `ALL_STATUSES` 守卫，加状态时它也要跟着更新。

## 5. 数据模型

**无 schema 变更。** `ready` 只是 `Task.status` 的一个新取值，该列已是 `String(16)`。

> 注：部署时发现 `init_db` 用 `create_all`，不会给已存在的表加列（见 backlog 的 D1）。本期不新增列，正好不受影响。

## 6. API 契约

新增一个端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks/{task_id}/start` | 请求体 `{engine?: str, options?: ConversionOptions}`；成功返回更新后的 `TaskDto`（status 已是 `pending`） |

错误：

| 情况 | 状态码 | 错误码 |
|---|---|---|
| 任务不存在 | 404 | `TASK_NOT_FOUND`（复用现有） |
| 任务不在 `ready` 状态（重复点、或已在转） | 409 | **`TASK_ALREADY_STARTED`（新增）** |
| 任务已被 ready TTL 回收 | 409 | `TASK_ALREADY_STARTED`（同上——此时 status 已是 `failed`，不在 `ready`） |
| Redis 不可达 | 503 | `ENGINE_UNAVAILABLE`（复用现有） |

被 TTL 回收后原文件已删，即使强行入队也只会在 `run_task` 里因找不到源文件而失败。409 加上 Task 行里已有的 `READY_EXPIRED` 错误信息，足以让用户明白发生了什么。

**不能复用 `TASK_NOT_READY`。** 它已经存在（`app/api/tasks.py:33`）且已有既定含义——`download` 端点用它表示「任务状态还不是 `done`，尚无可下载结果」。若拿它表示「任务已经离开 `ready` 状态」，同一个码会指向两个几乎相反的意思：一个是「还没到终点」，一个是「已经离开起点」。客户端无法区分。

故新增 `TASK_ALREADY_STARTED`，语义明确：这个任务已经启动过了。用 409 而非 422，因为这是状态冲突不是入参非法。

**新错误类的位置**：`TaskNotFound` / `TaskNotReady` 目前定义在 `app/api/tasks.py` 而不是 `app/errors.py`——这是既有的不一致。本期**沿用既有位置**（新类也放 `tasks.py`），不顺手搬迁：搬动会牵扯 `main.py` 的错误处理器与既有测试，属于本期范围外的重构。

## 7. 测试策略

沿用前四期标准：**这段逻辑能否在没有 Azure 账号的机器上运行？能则写测试。**

| 要测 | 怎么测 |
|---|---|
| 剥离本身 | python-pptx（dev 依赖）造带假视频 part 的 deck；断言媒体 part 没了、`.rels` 无悬空内部关系、体积降了、`mc:Ignorable` 还在、slide 数不变 |
| 剥离**真的被调用** | 删掉 `run_task` 里那次调用，必须有测试变红 |
| 剥离**真的取代原件** | 把 `strip_media` 改成空操作，切片判定应跟着变——证明 `size_bytes` / `needs_sharding` 吃的是剥离后的值 |
| 空操作不重写 | 不含媒体的 deck 剥离后文件 mtime 或字节不变 |
| `ready` 全链路 | `complete` 后 Task 是 `ready` 且**没入队**；`start` 后才 `pending` 且入队 |
| `ready` 不被孤儿回收 | 造 46 分钟没动的 `ready` 任务，`reap_stale_tasks` 后断言**不回收**（它归 ready TTL 管，不归 45 分钟的 reaper 管） |
| ready TTL 到点才回收 | 59 分钟的 `ready` 任务不回收；61 分钟的回收，且断言原文件已删、Task 落 `failed` + `READY_EXPIRED` |
| ready TTL 不误伤未完成的上传会话 | 造一个 2 小时没动的**未完成** upload 会话，跑 ready TTL 清理，断言该会话仍在（它归 upload TTL 管，24 小时） |
| Redis 兜底已挪到 start | 让 `enqueue` 抛错，断言 `drop_original` 被调用、Task 落 failed |
| 重复点 start | 第二次调应 409 且不重复入队 |
| 前端两段式 | 传完停在就绪卡片、不自动转；点按钮才发 start 请求 |

### 接线守护（重点）

本项目在「函数写了但没人调用」上栽过五次，四期靠跨任务变异才补住。以下三条必须能被变异检查抓住：

1. 剥离被调用
2. 剥离结果取代了原件（而非只在某一步用一下）
3. `start` 才入队（`complete` 不入队）

### 验收命令

四期终审的教训：`npm test` 与 `npm run build` 都绿而 `npm run lint` 有 9 条 error，而验收只跑了前两个。**五条都要跑**：

```
cd backend && .venv/Scripts/python.exe -m pytest -q
cd frontend && npm test -- --run
cd frontend && npm run build
cd frontend && npm run lint
docker compose config -q
```

## 8. 真机验收

有明确的判据素材：用户那份 **83.7MB / 59 页**、第 25 页含 56MB 视频的课件。

剥离后应当：

- 体积大幅下降（预期到 30MB 量级）
- **不再报 `SHARD_TOO_LARGE`**
- 走 Graph 单次转换（不再需要切片）并真正转出 PDF

这是本期改动成不成的唯一判据。

## 9. 已知限制

- **剥离是有损且不可逆的**（覆盖原件）。视频在 PDF 里本就无法保留，所以信息损失是零；但若某个 deck 剥完转出来不对，服务器上没有原件可对照，需用户重传。
- 悬空的 `r:id`（指向已删媒体）会留在 slide 正文里。同三期的既有裁决。
- `ready` 任务占的原文件磁盘空间最长 **1 小时**（`PPTX2PDF_READY_TTL_HOURS`）。上传后不点「开始转换」就走人的话，一小时后文件被回收、任务标 `failed`（`READY_EXPIRED`），需要重新上传。这是刻意的取舍：重传成本小于长期占盘，而机器只有 35G 可用。
- 新增的 `READY_EXPIRED` 与既有的 `TASK_ABANDONED`（孤儿回收器用的）是两个不同的失败原因，不要混用：前者是「你没点开始」，后者是「转换过程中卡死了」。
