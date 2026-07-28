# pptx → PDF

把课程 pptx 转成能直接导入 GoodNotes / OneNote 的 PDF。

**当前进度：三期（Microsoft Graph 引擎 + 转换切片合并）开发完成，本机可验证；按计划暂不部署，审核通过后再开四期。** 一期的占位 PDF 已被二期真实转换取代。

## 保真度边界

pptx 只存字体名不存字形，字体缺失时渲染端会替换，替换字体的字符宽度不同
导致换行位置改变——这是排版错位的根因。

- **西文接近完美**：镜像内置 Carlito / Caladea / Liberation，与 Calibri /
  Cambria / Arial 是 metric 兼容的，换行位置不变。
- **中文仍有偏差**：等线、微软雅黑受版权保护不能打进镜像，且没有 metric
  兼容的自由替代。镜像装的是 Noto CJK，保证中文不渲染成豆腐块，但中文段落
  的换行位置会偏移。

要消除中文偏差，把你自己 Windows 的 `C:\Windows\Fonts` 里的等线、微软雅黑
拷进宿主机的 `fonts-extra/` 目录，容器启动时会自动加载并优先使用。

## Graph 通道（三期）

Microsoft Graph 用微软自己的渲染服务转换，保真度是天花板，但它有硬限制：
约 100 页、约 50MB、45 秒同步超时，且没有异步 API。超出限制的文件会被
切分成多片分别转换再合并（分片 pptx 容量 = `12 × 40MiB` = 480MiB，合并
预算 = 240MiB，见下方「关键配置」表）。自动路由只覆盖小文件（≤80 页且
≤40MB）；更大的文件要显式在上传时选择 Graph 引擎才会走切片路径，因为
切片意味着数十次 HTTP 往返和几分钟等待，不适合当默认行为。

**三期本机可验证，但没有部署、也没有可用的凭证配置入口。** 用户可以在
上传时显式选择 `graph` 引擎并跑通全部单元/集成测试，但生产部署里，Graph
凭证的写入路径（`app.services.graph_credentials.save_credentials`）只有
四期的管理页面会调用；三期没有任何路由把 tenant_id / client_id /
client_secret 写进数据库。也就是说：**在四期管理页面上线之前，即便部署了
三期，选择 Graph 引擎的任务也会稳定收到 `GRAPH_NOT_CONFIGURED`（503）**——
这是预期行为，不是 bug，`select_engine` 的 `auto` 分支因此也会一直选
`libreoffice`（见 `app/services/engine_router.py`）。

**Azure 凭证不写在 `.env` 里。** `.env` 只放 `PPTX2PDF_SECRET_KEY`——解密
数据库里凭证用的 Fernet 主密钥；tenant_id / client_id / client_secret /
SharePoint site_id 全部加密存在 `graph_credentials` 表，由四期的管理页面
写入。不要去 `.env` 里找 tenant_id，那里没有也不应该有。

四期管理页面上线后，配置步骤会是：

1. 注册 Azure AD 应用，拿到 tenant_id / client_id / client_secret
2. 授予应用权限并管理员同意
3. 建一个专用 SharePoint 站点作为中转库
4. 生成 Fernet 密钥并写入 `PPTX2PDF_SECRET_KEY`：
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
5. 在管理页面填写 Azure 凭证，落库前用上一步的密钥加密

**密钥丢失等于凭证全废**——数据库里的 client secret 再也解不开，只能去
Azure 重新生成。请把 `PPTX2PDF_SECRET_KEY` 与 `.env` 一起妥善备份；换密钥
后旧凭证会在下次读取时报「Graph 凭证无法解密」。

**合并内存预算的由来**：`graph_max_merge_bytes`（240MiB）基于实测 3.01×
内存倍率——4 片共 54.1MB 的图片密集型 PDF，tracemalloc 测得峰值
162.9MB。`merge_pdfs` 把所有分片一次性载入同一个 `PdfWriter`（pypdf 没有
真正的流式合并 API），峰值随分片 PDF 总字节而不是分片数增长，所以单独卡
这一条而不是只卡分片数。tracemalloc 不含解释器基线与分配器碎片，真实 RSS
更高——**四期上真实租户后应实测 RSS 再回调这个值**。

**权限的已知不确定性**：清理中转文件用的 `permanentDelete`，文档标注需要
`Files.ReadWrite.All` 或 `Sites.ReadWrite.All`（租户级宽权限），与最小权限
的 `Sites.Selected` 可能冲突；社区也有报告称它有时仍把文件送进回收站。
代码里做了降级（403 时退回普通 DELETE 并告警），四期实测后再收敛。
中转文件若进了回收站，记得定期清空——两级回收站仍占 SharePoint 配额。

**分片目录的保留策略**：`purge_expired_shards()` 有两个触发点——
`pipeline.py` 的 `finally`（每次任务结束顺带清一次，惰性）和 `main.py`
的启动钩子（服务重启时补一次，覆盖 worker 被 OOM killer 杀掉、没有
`finally` 跑过的残骸）。它的安全性依赖一条不变量：
`OUTPUT_TTL_HOURS × 3600 > CONVERT_TIMEOUT_MAX_S`（默认 86400 > 1800，
48 倍余量）。**把 `PPTX2PDF_OUTPUT_TTL_HOURS` 调得比默认小很多之前，先确认
这条不变量还成立**——它纯按目录 mtime 判断，不查任务表，调太小会把仍在
转换中任务的分片目录当过期垃圾删掉。

**只读容量端点**：`GET /api/config/capacity` 返回 `max_file_size` /
`graph_max_shards` / `graph_max_shard_bytes` / `graph_max_merge_bytes`
四个数字，供前端在用户选择 Graph 引擎时做上传前的容量预判，避免白传几百
MB 才在分片规划阶段吃 422。不返回任何凭证状态。

## 部署

需要 Docker 与 docker-compose。目标平台 ARM64 或 x86_64 均可。

```bash
git clone https://github.com/MrZhongzq/pptxToPdf.git
cd pptxToPdf

cp .env.example .env      # 按需修改，默认值适合 4 核 24GB 的机器
mkdir -p fonts-extra      # 可选：把 Office 字体放进去

docker compose up -d --build
docker compose logs -f
```

起来之后访问 `http://<主机>:18993`。四个容器：`frontend`（nginx，唯一对外端口）、`api`、`worker` ×2、`redis`。

**前端不需要单独构建**——`deploy/frontend.Dockerfile` 是多阶段镜像，在 node 容器里跑 `npm ci && npm run build`，产物拷进 nginx 镜像。部署机只要有 Docker 就够，不用装 Node。

云主机还要放行端口（两层都要，少一层就连不上）：

```bash
# 1. 主机防火墙。Oracle/AWS 的 Ubuntu 镜像默认只放行 22，
#    且 INPUT 链末尾有一条 REJECT，新规则必须插在它前面
sudo iptables -I INPUT 5 -p tcp --dport 18993 -j ACCEPT
sudo netfilter-persistent save    # 没有这个命令就 apt install iptables-persistent

# 2. 云平台的安全列表 / 安全组，在控制台加一条入站规则放行 18993/tcp
```

### 关键配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEB_PORT` | 18993 | 对外 web 端口。api 只绑 `127.0.0.1:8000` 供宿主机排查，不对外 |
| `WORKER_REPLICAS` | 2 | 并发转换数。4 核机器建议 2，留 1 核给上传 |
| `PPTX2PDF_CONVERT_TIMEOUT_PER_SLIDE_S` | 4 | 每页超时系数。ARM 机器偏慢，转换总超时 = `min(max(180, 页数×4 + 体积MB×2), 1800)` 秒 |
| `PPTX2PDF_CONVERT_TIMEOUT_PER_MB_S` | 2 | 每 MB 超时系数，覆盖「页数少但内嵌大量图片/视频」的重课件 |
| `PPTX2PDF_OUTPUT_TTL_HOURS` | 24 | 输出 PDF 保留时长，过期自动清理；也是三期分片目录残骸清理的安全边界，见下方 Graph 通道一节 |
| `PPTX2PDF_STALE_TASK_MINUTES` | 45 | 孤儿任务回收阈值，必须大于最大转换超时 |
| `PPTX2PDF_SECRET_KEY` | 空 | Graph 凭证的 Fernet 主密钥。未配置则 Graph 引擎不可用 |
| `PPTX2PDF_GRAPH_MAX_PAGES_PER_SHARD` | 80 | 每片页数上限，对 Graph 的 100 页硬限留余量 |
| `PPTX2PDF_GRAPH_MAX_SHARD_BYTES` | 41943040（40MiB） | 每片体积上限，对 Graph 实测 ~50MB 失败点留余量 |
| `PPTX2PDF_GRAPH_MAX_SHARDS` | 12 | 分片数上限。Graph 路径实际容量 = 本值 × `GRAPH_MAX_SHARD_BYTES` = 480MiB，低于 `PPTX2PDF_MAX_FILE_SIZE`（600MiB）——这是 Graph 硬限的固有后果，不要调大本值去对齐 |
| `PPTX2PDF_GRAPH_MAX_MERGE_BYTES` | 251658240（240MiB） | 合并阶段各分片 PDF 总字节上限，基于 3.01× 实测内存倍率算出，详见下方 Graph 通道一节 |
| `PPTX2PDF_GRAPH_REQUEST_TIMEOUT_S` | 50 | 单次 Graph 转换请求超时，Graph 自身约 45 秒硬超时 + 5 秒网络余量 |
| `PPTX2PDF_GRAPH_MAX_RETRIES` | 3 | Graph 请求失败重试次数（429/5xx 退避重试）|

worker 单容器内存上限硬编码在 `docker-compose.yml`（三期从 3G 提到 8G），**不通过 `.env` 控制**——流式切片本身内存恒定，但合并阶段 pypdf 要把多份分片 PDF 一次性读进同一个 `PdfWriter`（没有真正的流式合并 API），几十 MB × 若干片仍然吃内存。24GB 机器上 2 个 worker × 8G = 16G，留 8G 给 api、redis 与系统；换机器规格需要同步改 `docker-compose.yml` 里的 `memory:` 值。

### 排查：故障注入开关

异常路径不容易自然触发，用这些开关主动跑一遍。默认全关，改完 `.env`
后 `docker compose up -d` 重启生效，worker 启动时会打 WARNING 提醒。

| 变量 | 触发什么 |
|---|---|
| `PPTX2PDF_DEBUG_FORCE_TIMEOUT` | 把超时压到 1 秒，验证进程组是否被杀干净、profile 是否清理 |
| `PPTX2PDF_DEBUG_FORCE_ENGINE_FAILURE` | 引擎直接失败，验证失败落库与前端错误展示 |
| `PPTX2PDF_DEBUG_FORCE_EMPTY_OUTPUT` | 输出截断为 0 字节，验证「退出码不可信」的检测 |
| `PPTX2PDF_DEBUG_FORCE_PAGE_MISMATCH` | 输出删掉一页，验证页数一致性检查 |

## 开发

后端：

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Linux 用 .venv/bin/
.venv/Scripts/python -m pytest -q                   # 期望 225 passed
```

前端：

```bash
cd frontend
npm install
npm test          # 期望 58 passed
npm run dev       # 开发服务器在 5173，/api 已代理到 8000
```

本机不需要装 LibreOffice——转换只在 worker 容器里跑。

## 已知限制

- 无鉴权、无配额，任何人都能上传 600MB（四期才做）
- UI 没有断点续传入口：客户端库和后端协议都支持，但按设计刷新即重置
- 任务列表只在 React state，刷新即丢，且没有列表端点可恢复
- SQLite 靠 WAL 支撑 api 与 worker 两个容器共享，**不要把 storage volume
  挂到 NFS 或对象存储 FUSE 上**，那种场景下文件锁不可靠
- 前端由 `frontend` 容器托管，镜像里是构建时的产物快照；改前端代码需要 `docker compose up -d --build frontend` 重新构建
- 转换结果只保留 24 小时（`PPTX2PDF_OUTPUT_TTL_HOURS`），过期自动清理，请
  及时下载；过期后再请求下载会返回 `RESULT_EXPIRED`，需要重新上传。原始
  pptx 在转换结束后立即删除，不论成败
- Graph 通道（三期）已实现且测试覆盖，但没有凭证配置入口——四期管理页面
  上线前，选 Graph 引擎的任务会稳定报 `GRAPH_NOT_CONFIGURED`，详见上方
  「Graph 通道（三期）」一节
- 分片转换的中间产物体积是原文件的两倍（分片 pptx + 分片 PDF），汇总后
  立即清理；worker 异常退出时残留的分片目录由保留策略按
  `PPTX2PDF_OUTPUT_TTL_HOURS` 回收

## 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 一 | 前端三端 UI + 分片上传全链路 + 元信息解析 + 占位 PDF | 完成 |
| 二 | LibreOffice 引擎 + 容器化 + 队列 + 资源治理 | 完成 |
| 三 | Microsoft Graph 引擎（小文件高保真）+ 转换切片合并 | 开发完成，未部署（审核通过后开四期） |
| 四 | 账号、配额、风控、管理面板（含 Graph 凭证配置入口） | 未开始 |

设计文档见 `docs/superpowers/specs/`，实施计划见 `docs/superpowers/plans/`。
