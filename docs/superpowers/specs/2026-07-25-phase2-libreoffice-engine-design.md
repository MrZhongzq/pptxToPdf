# 二期：LibreOffice 转换引擎 · 设计文档

日期：2026-07-25
上游：`2026-07-25-pptx-to-pdf-design.md`（总体设计，§10 为二期既定决策）

---

## 1. 范围

把一期的占位 PDF 换成真实转换，并把周边治理一次做完。

**做**：容器化部署（docker-compose）、LibreOffice 引擎、字体、RQ 队列与并发闸门、转换超时与进程清理、资源保留策略、一期终审遗留的全部 8 项、故障注入开关。

**不做**：Graph 引擎与转换切片（三期）、账号配额风控管理面板（四期）、字体缺失诊断（用户明确推迟）、断点续传 UI 接线（§10.1 已定为非需求）。

**验收方式**：不为二期新增任何测试代码。一期已有的 49 个后端测试保留作回归网。功能验证全部在测试机上真跑——主路径用真实课程 pptx，异常路径用故障注入开关拨出来，全程看日志。

## 2. 目标运行环境

| 项 | 值 |
|---|---|
| 机器 | Oracle Cloud Ampere A1，**ARM64 / aarch64** |
| 规格 | 4 OCPU / 24 GB / 200 GB 块存储 |
| 系统 | Linux，Docker + docker-compose |
| 后备 | 本地 Ubuntu，16 GB+ / 20 核（云端吃不消时迁移） |
| 开发机 | Windows，**不装 LibreOffice**，只写代码不跑转换 |

ARM64 是硬约束：所有镜像与依赖必须有 aarch64 构建。Debian bookworm 的 LibreOffice 与字体包均有 arm64 版本，`python:3.12-slim-bookworm` 是 multi-arch。

Ampere Altra 的单核性能弱于同代 x86，转换耗时的经验系数要按此放宽。

## 3. 部署架构

```
docker-compose
├── api      FastAPI + uvicorn        上传/查询/下载，不碰转换
├── worker   RQ worker + LibreOffice  只做转换（replicas: 2）
└── redis    队列
     ↕
  storage volume（api 与 worker 共享）
     uploads/  originals/  outputs/  pptx2pdf.db
```

数据流：`api` 收完分片拼装到 `originals/` → `queue.enqueue(run_task, task_id)` → `worker` 取出、从共享 volume 读原文件、调 `soffice`、写 `outputs/` → 更新任务状态 → `api` 的轮询端点读到 `done`。**文件全程在 volume 上，跨容器零拷贝、零 HTTP 传输。**

### 3.1 SQLite 跨容器共享

数据库文件移入共享 volume（`storage/pptx2pdf.db`），`api` 与 `worker` 两个进程同时读写。启用 WAL 并设置 busy_timeout：

```python
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
```

可行性依据：写入频率极低（每个任务全程 4–5 次状态更新），且 volume 是本地文件系统。**前提是不挂载任何网络存储**——NFS 或对象存储 FUSE 上 SQLite 的文件锁不可靠。用户已确认不会挂外部存储。

### 3.2 资源限额

| 服务 | 限额 | 理由 |
|---|---|---|
| worker | memory 3 GB × 2 replicas | 单个 soffice 转 500MB 文件峰值 1–2 GB，3 GB 留余量。超限时 OOM killer 只杀这一个容器，不拖垮整机 |
| api | 不设硬限 | 只做 IO 编排 |
| redis | 不设硬限 | 队列极短 |

并发度即 worker replicas，默认 **2**，compose 里写 `replicas: ${WORKER_REPLICAS:-2}`。4 核机器留 1 核给 api 处理分片写入——ARM 单核弱，全占满会让上传变卡。

`redis` 服务加 healthcheck（`redis-cli ping`），`api` 与 `worker` 用 `depends_on: {redis: {condition: service_healthy}}`，避免 worker 先起导致连接失败刷屏。

### 3.3 配置项

一期的 `Settings` 加 `env_prefix="PPTX2PDF_"`（§8 第 7 项），所有配置经环境变量注入容器：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PPTX2PDF_STORAGE_ROOT` | `/app/storage` | 容器内绝对路径，指向共享 volume |
| `PPTX2PDF_DATABASE_URL` | `sqlite:////app/storage/pptx2pdf.db` | 移入共享 volume（注意四条斜杠＝绝对路径） |
| `PPTX2PDF_REDIS_URL` | `redis://redis:6379/0` | compose 服务名解析 |
| `PPTX2PDF_MAX_FILE_SIZE` | `629145600` | 600 MB，沿用一期 |
| `PPTX2PDF_CHUNK_SIZE` | `5242880` | 5 MiB，沿用一期 |
| `PPTX2PDF_UPLOAD_TTL_HOURS` | `24` | 沿用一期 |
| `PPTX2PDF_OUTPUT_TTL_HOURS` | `24` | 新增，§7 |
| `PPTX2PDF_STALE_TASK_MINUTES` | `45` | 新增，§8 孤儿任务阈值，必须 > 最大转换超时 30 分钟 |
| `PPTX2PDF_CONVERT_TIMEOUT_BASE_S` | `180` | 新增，§6.2 |
| `PPTX2PDF_CONVERT_TIMEOUT_PER_SLIDE_S` | `4` | 新增，§6.2 |
| `PPTX2PDF_CONVERT_TIMEOUT_MAX_S` | `1800` | 新增，§6.2 |
| `PPTX2PDF_SOFFICE_BIN` | `soffice` | 新增，便于本地排查时指向别处 |
| `WORKER_REPLICAS` | `2` | compose 层面，不进 Settings |

一期 `storage_root` 的 `.resolve()` validator 保留——容器内已是绝对路径，validator 变成无操作但不冲突。

## 4. LibreOffice 引擎

新增 `backend/app/services/engines/libreoffice.py`，实现 `ConversionEngine`。

### 4.1 调用形式

```bash
soffice --headless --norestore --invisible \
  -env:UserInstallation=file:///tmp/lo_<uuid4> \
  --convert-to pdf:impress_pdf_Export \
  --outdir <outputs_dir> <originals_dir>/<task_id>.pptx
```

`-env:UserInstallation` **每次调用必须给独立目录**，用完即删。多个实例共用默认 profile 会争抢锁文件，表现为随机失败或挂死——这是 headless 并发的经典坑。

输出命名天然合上一期约定：soffice 把 `<task_id>.pptx` 转成 `<outdir>/<task_id>.pdf`，与 `outputs/{task_id}.pdf` 一致，一期的路径逻辑不用改。

### 4.2 超时必须杀整个进程组

`soffice` 启动后会 fork 出真正干活的子进程。`subprocess.run(timeout=)` 超时只 kill 父进程，子进程变孤儿继续吃内存，累积几个就把机器打死。

```python
proc = subprocess.Popen(cmd, start_new_session=True, ...)
try:
    proc.communicate(timeout=timeout_s)
except subprocess.TimeoutExpired:
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait()
    raise ConversionTimeout(...)
finally:
    shutil.rmtree(profile_dir, ignore_errors=True)
```

`start_new_session=True` 建独立进程组，`killpg` 整组端掉。profile 目录无论成败都要清理。

### 4.3 退出码不可信

LibreOffice 转换失败时经常照样返回 0。判定成功的依据**不是 returncode**，而是四条同时成立：

1. 输出文件存在
2. 大小非零
3. `pypdf.PdfReader` 能打开
4. **页数等于 `meta.slide_count`**

只信退出码会让失败任务被标成 `done`，用户下载到 0 字节 PDF。

第 4 条页数不符时**标 `failed` 而非放行**：给用户一份缺页的 PDF 比明确报错更糟——他可能直到考前复习到那一页才发现。错误码 `CONVERSION_PAGE_MISMATCH`，消息里带上期望页数与实际页数。

### 4.4 新增错误族

`backend/app/errors.py` 增加：

| 类 | code | http_status |
|---|---|---|
| `ConversionFailed` | `CONVERSION_FAILED` | 500 |
| `ConversionTimeout` | `CONVERSION_TIMEOUT` | 504 |
| `ConversionPageMismatch` | `CONVERSION_PAGE_MISMATCH` | 500 |
| `EngineUnavailable` | `ENGINE_UNAVAILABLE` | 503 |

这些码通过 `TaskDto.error_code` 返回给前端（HTTP 状态仍是 200，因为查询本身成功）。一期的 `pipeline` 会把 `AppError` 的 `code`/`message` 落库，无需改动。

在此之前所有引擎异常都被归一成 `INTERNAL_ERROR`，前端会把原始 Python 异常字符串直接显示给用户。

### 4.5 `ConversionEngine` 抽象变更

```python
class ConversionEngine(ABC):
    name: str

    @abstractmethod
    def convert(self, src: Path, meta: PptxMeta, dest: Path, *, timeout_s: float) -> None:
        """把 src 转成 PDF 写到 dest。失败抛 AppError 子类。"""
```

`PlaceholderEngine.convert` 增加同名参数并忽略它（占位引擎瞬时完成）。

## 5. 字体

字体是保真度的 80%——pptx 只存字体名不存字形，替换字体的字符宽度不同导致换行位置改变，这正是项目要解决的根因。

### 5.1 镜像内置（全部自由许可）

| 用途 | 包 | 说明 |
|---|---|---|
| Calibri 替代 | `fonts-crosextra-carlito` | **metric 兼容**，换行位置不变 |
| Cambria 替代 | `fonts-crosextra-caladea` | **metric 兼容** |
| Arial / Times / Courier 替代 | `fonts-liberation`、`fonts-liberation2` | **metric 兼容** |
| 中日韩 | `fonts-noto-cjk` | 无 metric 兼容替代，见 5.2 |
| 希腊语、西里尔等 | `fonts-noto-core` | 公式里的希腊字母 |
| 广覆盖符号 | `fonts-dejavu-core` | |
| 数学符号 | `fonts-stix` | 顶替 Cambria Math |

### 5.2 中文的现实边界

等线（DengXian，Office 默认中文字体）与微软雅黑受版权保护，**不能合法打进镜像**，且**没有任何自由字体与它们 metric 兼容**。

处理方式是 fontconfig 映射到 Noto Sans CJK（`/etc/fonts/local.conf`），保证中文不渲染成豆腐块。但字宽不同，**中文段落的换行位置仍会偏移**。

因此二期完成后的保真度预期：**西文课件接近完美；中文或中英混排课件仍有排版偏差**，症状从「文字压到图片上」缓解为「换行位置不太一样」。

### 5.3 私有字体挂载点

镜像不含 Office 字体，但预留只读挂载点：

```yaml
volumes:
  - ./fonts-extra:/usr/share/fonts/truetype/extra:ro
```

用户自行从 Windows 的 `C:\Windows\Fonts` 拷贝等线、微软雅黑等放入宿主机的 `fonts-extra/`，容器启动时 `fc-cache -f` 生效。目录为空时一切照常工作。这是自部署自用范围内的字体使用，与公开分发镜像是两回事。

`fonts-extra/` 加入 `.gitignore`，不入库。

## 6. 队列、并发与超时

### 6.1 选型修正：RQ 而非 ARQ

上游设计文档 §4 写的是「二期起换 Redis + ARQ」。具体到 LibreOffice 场景改用 **RQ**：

- ARQ 是 async 优先的，job 必须是 `async def`。我们的活是**阻塞式调 subprocess 等几分钟**，塞进 async 要包一层 executor，白白多一层间接。
- RQ 是同步模型，且**每个 job fork 独立子进程执行**——soffice 崩溃、内存泄漏、段错误都被隔离在子进程里，不累积到常驻 worker 上。对长期运行的转换服务，这个隔离性比 async 的并发能力值钱。
- RQ 的 fork 模型不支持 Windows，但目标环境是 Linux 容器。

`run_task(task_id)` 的签名一期就是为此设计的（只吃 id、自开 session、路径从 settings 取），函数体基本不动，改的是投递方式与它跑在哪个进程里。

### 6.2 双层超时

| 层 | 值 | 作用 |
|---|---|---|
| subprocess | `max(180, slide_count × 4)` 秒，上限 1800 | 主超时，我们能优雅处理：杀进程组、清 profile、标 `CONVERSION_TIMEOUT` |
| RQ `job_timeout` | subprocess 超时 + 60 秒 | 兜底，防 job 在 subprocess 之外的地方卡住 |

正常情况下 subprocess 超时先触发，RQ 超时只在异常时兜底。固定值行不通：10 页与 500 页的合理超时差一个数量级。系数 4 秒/页是 ARM 单核性能下的保守估计，测试机上跑过真实文件后校准。

### 6.3 不自动重试

LibreOffice 转不动的文件重试还是转不动，只白占几分钟 CPU。直接标 `failed` 带错误码。worker 进程被 kill（部署、OOM）导致的中断不是自动重试的例外——RQ 2.0 在不配 `Retry` 的情况下，work-horse 死亡或 job 超时都是直接移进 `FailedJobRegistry`，并不会自动把 job 放回队列。这意味着这类任务会永远停在 `converting`/`parsing` 等中间态，前端轮询也永远等不到终态更新。因此仍需要 §8 终审 I3 落地的孤儿任务回收器（`reap_stale_tasks`）兜底，而且不能只挂 api 启动时跑一次：`worker` 容器有内存上限、OOM 是预期事件，work-horse 被杀后 api 未必会重启（`restart: unless-stopped`），所以 `pipeline.run_task` 的 `finally` 里也惰性触发一次，两处互为兜底。

### 6.4 引擎选择位置

从 `uploads.py` 的 `complete_upload`（写死 `engine="placeholder"`）挪到 `pipeline.py` 的 `probe()` **之后**——三期 Graph 的路由判据是页数，而页数要解析完才知道。

新增 `backend/app/services/engine_router.py`：

```python
def select_engine(meta: PptxMeta) -> str:
    """二期只有一个真引擎。三期在此加入 Graph 的页数与大小判据。"""
    return "libreoffice"
```

`Task.engine` 的模型默认值从 `"placeholder"` 改为 **`"unassigned"`**，`complete_upload` 不再写这个字段，由 `run_task` 在 probe 之后调 `select_engine(meta)` 写入。这样任何停留在 `unassigned` 的任务一眼就能看出它没走到 probe——保留 `"placeholder"` 作默认值会让「引擎未分配」和「真的用了占位引擎」两种状态无法区分。

### 6.5 引擎注册表

一期终审已把 `_ENGINES` 从存实例改为存类，`get_engine(name)` 里实例化。二期新增 `libreoffice` 注册项即可，无需再改结构。

## 7. 资源治理

一期完全没有保留策略，`originals/` 与 `outputs/` 无任何删除路径，磁盘随使用无限增长。

| 目录 | 策略 |
|---|---|
| `originals/` | **转换结束即删**，不论成败。用户要的是 PDF；失败了他会重传，留着诊断也用不上。这一条砍掉一半的磁盘增长 |
| `outputs/` | 保留 **24 小时**（`PPTX2PDF_OUTPUT_TTL_HOURS`），过期删除 |
| `uploads/` | 沿用一期的过期会话惰性清理 |

**清理时机用惰性触发**：每次任务结束后顺带扫一次过期文件，与一期 `_purge_expired` 同一模式。长期无新任务时不清理，但那也意味着磁盘没在增长，所以惰性成立，不需要额外的 cron 容器。

配合 §10.1 的「刷新即重置」——用户转完立刻下载，不存在长期回取需求。按 24 小时保留、每天十几个 500MB 文件估算，稳态占用十几 GB，200 GB 块存储余量充足。

`outputs/` 清理落地后，一期终审 Task 6 遗留的 `TASK_NOT_READY` 语义问题才有条件解决：结果文件因过期被清理时，返回新增的 `RESULT_EXPIRED`（410）而非复用 `TASK_NOT_READY`（409）。

## 8. 一期遗留项

上游 §10.4 列出的 8 项，二期全部处理：

1. ✅ 引擎选择挪到 probe 之后 —— 见 §6.4
2. ✅ `ConversionEngine` 补 `timeout_s` 与错误族 —— 见 §4.4、§4.5
3. ✅ `BackgroundTasks` 换真队列 —— 见 §6.1
4. ✅ `originals/`、`outputs/` 保留策略 —— 见 §7
5. **`put_chunk` 改流式读取** —— 当前先 `await request.body()` 再校验大小；`Content-Length` 缺失（`Transfer-Encoding: chunked`）时该调用无上限。改成 `async for part in request.stream()` 边读边计数，累计超过 `chunk_size` 立即中断
6. **业务错误码补进 openapi 契约** —— 给路由加 `responses={404: ..., 413: ...}` 声明，覆盖全部 `AppError` 子类。当前快照每个端点只有 200 和 FastAPI 默认的 422，且 `VALIDATION_ERROR` 的实际返回形状与声明的 `HTTPValidationError` 不一致
7. **`Settings` 加 `env_prefix="PPTX2PDF_"`** —— 容器化后环境变量撞车风险显著上升，必须做
8. **nginx 样例修正** —— `listen 443 ssl http2;` 在 nginx ≥1.25.1 已废弃，改为 `listen 443 ssl;` + `http2 on;`，并补 `ssl_certificate` 指令（当前照抄起不来）

**另加终审 I3（孤儿任务）**，方向见上游 §10.3「显性要求用户重传」：

- **后端**：`api` 启动时扫描 `updated_at` 超过 `PPTX2PDF_STALE_TASK_MINUTES`（默认 45）的非终态任务，批量标 `failed`，错误码 `TASK_ABANDONED`。阈值必须大于最大转换超时（30 分钟）
- **前端**：`useTaskPolling` 加轮询总时长上限（45 分钟），到点停止并提示重新上传。签名改为 `useTaskPolling(taskId): { task: TaskDto | null; pollingTimedOut: boolean }`，`TaskCard` 相应展示

两边都要做——只做后端的话，前端在后端重启前仍会无限轮询。

## 9. 故障注入开关

二期不写测试代码，异常路径靠开关在测试机上主动触发。这些开关同时是运维工具——线上出问题时用它们复现。

| 环境变量 | 行为 | 验证什么 |
|---|---|---|
| `PPTX2PDF_DEBUG_FORCE_TIMEOUT` | 引擎在调用 soffice 前 sleep 超过 timeout | 进程组是否被杀干净、profile 是否清理、状态是否落 `CONVERSION_TIMEOUT` |
| `PPTX2PDF_DEBUG_FORCE_ENGINE_FAILURE` | 引擎直接抛 `ConversionFailed` | 失败落库、前端错误展示 |
| `PPTX2PDF_DEBUG_FORCE_EMPTY_OUTPUT` | 转换后把输出文件截断为 0 字节 | §4.3 的「退出码不可信」检测是否生效 |
| `PPTX2PDF_DEBUG_FORCE_PAGE_MISMATCH` | 转换后从输出 PDF 删掉一页 | 页数一致性检查是否拦住 |

默认全部关闭，只在 `LibreOfficeEngine` 内部生效，不影响正常路径。开关状态在 worker 启动时打一条 WARNING 日志，避免忘记关掉。

## 10. 日志

验收全靠看日志，所以日志是交付物的一部分，不是附属品。

每个转换任务至少记录：`task_id`、原文件名、页数、文件大小、选中的引擎、超时阈值、soffice 命令行、退出码、实际耗时、输出文件大小与页数、成功或失败原因。失败时带完整异常链。

`api` 与 `worker` 都输出到 stdout，由 `docker compose logs` 收集。格式用带时间戳的结构化文本（不引入日志聚合组件）。

## 11. 不做的事

- Graph 引擎与转换切片（三期）
- 账号、配额、风控、管理面板（四期）
- 字体缺失诊断与预警（用户明确推迟）
- 断点续传 UI 接线（上游 §10.1 已定为非需求）
- 为二期新增单元测试（用户明确要求，改用 §9 的故障注入开关在真机验证）
- 多标签页适配（上游 §10.1）
- 迁移到 PostgreSQL（§3.1 的 SQLite + WAL 足够，前提是不挂外部存储）
