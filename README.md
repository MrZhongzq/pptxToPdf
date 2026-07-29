# pptx → PDF

把课程 pptx 转成能直接导入 GoodNotes / OneNote 的 PDF。

**当前进度：六期（账号体系 + Admin 面板 + 转换后处理）开发完成，本机可验证；按计划暂不部署，待审核通过。** 一期的占位 PDF 已被二期真实转换取代；三期的 Graph 引擎需在管理入口完成凭证配置后才可达，见下方「管理入口」一节；四期新增了管理入口（口令登录 + Azure 凭证配置）。

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

## 两段式上传（五期）

上传完成不会自动开始转换。文件传完后停在一张「就绪」卡片上（状态
`ready`），展示文件名与体积；引擎与转换选项可以在卡片上随时改，改好点
「开始转换」（`POST /api/tasks/{task_id}/start`）任务才真正入队，状态转
`pending`。用户原话：「有时候手没那么快，想先上传再选转换引擎和选项。」

上传时仍然可以选引擎和选项（`POST /api/uploads` 的 `engine` / `options`
字段没有废弃）——这份选择会作为就绪卡片的初始值；点「开始转换」时不传
新的引擎/选项就沿用上传时选的，传了就覆盖，两者互不冲突。

**就绪任务不会无限期占盘。** 单份原始 pptx 可能有 80–500MB，一直不点
「开始转换」会一直占着磁盘（机器只有 35G 可用）。超过
`PPTX2PDF_READY_TTL_HOURS`（默认 1 小时）未开始转换的任务会被回收：原
文件删除，任务标记为 `failed`，`error_code` 为 `READY_EXPIRED`，需要重新
上传。**这不是一个精确到点的定时任务**——回收在服务启动时、任意任务
转换流程跑完时、以及任意新的上传完成（`complete`）时顺带触发一次，
不代表「一小时整就被删」。第三个触发点是专门补的：只传不点「开始
转换」正是 ready TTL 要防的场景，而那个场景下恰恰不会有任何转换跑完，
若只挂前两个触发点，站点长期没有别的上传/转换活动时这个任务会一直
不被回收；接上"新上传完成"这个更频繁的事件之后，只要还有人在正常
使用这个站点，实际回收延迟通常远小于一小时。真把服务长期晾着、完全
没有任何上传/转换/重启发生，才会退化成"要等下一次这类事件才触发"。

回收后再点已过期的就绪卡片，后端返回 410 `READY_EXPIRED`；如果任务已经
真的被启动过一次（比如另一个标签页抢先点了按钮，任务仍在正常转换中），
返回的是 409 `TASK_ALREADY_STARTED`——两者语义不同，客户端要分开处理：
前者退回可重新上传的界面，后者接上轮询。

`PPTX2PDF_READY_TTL_HOURS` 与 `PPTX2PDF_UPLOAD_TTL_HOURS`（默认 24 小时）
管的不是一回事：后者管**未完成的分块上传会话**，支持断点续传，调短了会
让大文件传到一半、暂停超时后必须从头重传；`PPTX2PDF_READY_TTL_HOURS` 管
的是**已经传完、只差点一下按钮**的任务，重传成本相对小，可以更快回收。

## Graph 通道（三期）

Microsoft Graph 用微软自己的渲染服务转换，保真度是天花板，但它有硬限制：
约 100 页、约 50MB、45 秒同步超时，且没有异步 API。超出限制的文件会被
切分成多片分别转换再合并（`12 × 40MiB` = 480MiB 是分片 **pptx 产物**总量
的上界，不是「保证能过」的原文件体积——每片各自还带一份 masters/主题/
字体等共享部分，实际能接受的原文件上限严格小于 480MiB；合并预算另外
卡 240MiB，见下方「关键配置」表）。自动路由只覆盖小文件（≤80 页且
≤40MB）；更大的文件要显式在上传时选择 Graph 引擎才会走切片路径，因为
切片意味着数十次 HTTP 往返和几分钟等待，不适合当默认行为。

**三期本机可验证，凭证配置入口在四期落地。** 用户可以在上传时显式选择
`graph` 引擎并跑通全部单元/集成测试；生产部署里，Graph 凭证的写入路径
（`app.services.graph_credentials.save_credentials`）只有四期的管理入口
（`/admin`）会调用——没有别的路由把 tenant_id / client_id / client_secret
写进数据库。也就是说：**在 `/admin` 完成配置并通过五步自检之前，选择
Graph 引擎的任务会稳定收到 `GRAPH_NOT_CONFIGURED`（503）**——这是预期
行为，不是 bug，`select_engine` 的 `auto` 分支因此也会一直选
`libreoffice`（见 `app/services/engine_router.py`）。配置步骤见下方
「管理入口（四期）」一节。

**Azure 凭证不写在 `.env` 里。** `.env` 只放 `PPTX2PDF_SECRET_KEY`——解密
数据库里凭证用的 Fernet 主密钥；tenant_id / client_id / client_secret /
SharePoint site_id 全部加密存在 `graph_credentials` 表，由四期的管理页面
写入。不要去 `.env` 里找 tenant_id，那里没有也不应该有。

配置步骤：

1. 注册 Azure AD 应用，拿到 tenant_id / client_id / client_secret
2. 授予应用权限并管理员同意
3. 建一个专用 SharePoint 站点作为中转库
4. 生成 Fernet 密钥并写入 `PPTX2PDF_SECRET_KEY`：
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
5. **改完 `.env` 必须 `docker compose up -d` 重启 api 与 worker 容器**——
   `settings = Settings()` 是模块级单例，只在进程启动时读一次环境变量，
   运行中的容器不会热加载 `.env`。漏了这一步会看到「未配置
   `PPTX2PDF_SECRET_KEY`，Graph 引擎不可用」，容易误以为是密钥格式写错，
   反复检查 `.env` 却找不到问题——其实只是容器还没重启
6. 重启生效后，登录 `/admin`（见下方「管理入口（四期）」一节）填写
   Azure 凭证，落库前用上一步的密钥加密

**密钥丢失等于凭证全废**——数据库里的 client secret 再也解不开，只能去
Azure 重新生成。请把 `PPTX2PDF_SECRET_KEY` 与 `.env` 一起妥善备份；换密钥
后旧凭证会在下次读取时报「Graph 凭证无法解密」。

**合并内存预算的由来**：`graph_max_merge_bytes`（240MiB）基于 tracemalloc
（不是 RSS）测得的 3.01× 内存倍率——4 片共 54.1MB 的图片密集型 PDF，
tracemalloc 测得峰值 162.9MB。`merge_pdfs` 把所有分片一次性载入同一个
`PdfWriter`（pypdf 没有真正的流式合并 API），峰值随分片 PDF 总字节而不是
分片数增长，所以单独卡这一条而不是只卡分片数。tracemalloc 不含解释器
基线与分配器碎片，真实 RSS 更高——**四期上真实租户后应实测 RSS 再回调
这个值**（以及下方 worker 的内存限额）。

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

## 管理入口（四期）

Azure 凭证不再靠手工改数据库——`/admin` 是唯一的写入路径，登录后可以
查看当前配置（`client_secret` 从不回显）、改配置，以及在保存前跑一次
五步连通性自检。

**1. 设管理口令。** 哈希值写进 `PPTX2PDF_ADMIN_PASSWORD_HASH`，未配置则
整个管理入口返回 503（`ADMIN_NOT_CONFIGURED`）。生成命令（会提示输入
口令，与 `.env.example` 里那条一致）：

```bash
python -c "import hashlib,os,binascii; s=os.urandom(16); pw=input('口令: ').encode(); print('scrypt:'+binascii.hexlify(s).decode()+':'+binascii.hexlify(hashlib.scrypt(pw,salt=s,n=16384,r=8,p=1,dklen=32)).decode())"
```

把输出整行填进 `.env` 的 `PPTX2PDF_ADMIN_PASSWORD_HASH`，改完照下方规则
重启容器，然后访问 `http://<host>:<port>/admin` 用这个口令登录（部署默认
端口是 `18993`，即 `http://<host>:18993/admin`）。

**2. 五步自检。** 保存 Graph 凭证前会自动跑一遍，任意一步失败则后续步骤
标记为「未执行」（不是「通过」），且**不会写库**——先测后存，避免配错的
凭证进了数据库后每次真实转换才发现。五步依次是：

| 步骤 | 验证什么 | 常见失败原因 |
|---|---|---|
| `token` | 用 client credentials 换 access token | `AADSTS90002` 租户不存在/tenant_id 写错；`AADSTS700016` client_id 写错或应用未创建；`AADSTS7000215` client_secret 错误或已过期 |
| `drive` | 能读到 `site_id` 对应站点的文档库 | HTTP 404 site_id 写错；HTTP 403 应用权限未授予或管理员未同意 |
| `upload` | 把内置的自检 pptx 上传到中转库 | HTTP 403 没有写权限 |
| `convert` | 用 Graph 把刚上传的文件转成 PDF | 依常规 Graph 错误诊断；返回内容不是 PDF 时通常是被重定向到了登录页 |
| `delete` | 用 `permanentDelete` 清掉自检产生的中转文件 | HTTP 403——见下一条 |

**3. `permanentDelete` 的权限坑。** 最小权限的 `Sites.Selected` **不够**，
`delete` 步骤会报 403；需要 `Files.ReadWrite.All` 或 `Sites.ReadWrite.All`
（租户级宽权限）。这个坑不在配置时用 `Sites.Selected` 测试连通性就能发现
——`token`/`drive`/`upload`/`convert` 四步用它都能过，只有真正跑到
`delete` 才会暴露；不跑自检的话，要等到某次真实转换后中转文件永久留在
租户里才会注意到。五步自检把它提前到配置阶段暴露，是自检存在的主要
理由之一。

**4. `PPTX2PDF_ADMIN_COOKIE_SECURE` 与 HTTPS 的关系。** 当前部署是
`http://<host>:18993`，**非 HTTPS**，这个变量必须保持默认的 `false`——
写死 `true` 会让浏览器根本不回传 cookie，表现是「登录成功但立刻掉线」，
且现象和「口令错」「session 过期」很像，容易走错排查方向。等部署切到
HTTPS（自有域名 + 证书）之后，再把它改成 `true`。

**5. 改完 `.env` 必须 `docker compose up -d` 重启。** `settings` 是模块级
单例，只在进程启动时读一次环境变量，运行中的容器不会热加载 `.env`——
这条规则同样适用于三个新变量（`PPTX2PDF_ADMIN_PASSWORD_HASH` /
`PPTX2PDF_ADMIN_COOKIE_SECURE` / `PPTX2PDF_ADMIN_SESSION_DAYS`），见上方
「排查：故障注入开关」一节已经写过的同一条规则。

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

`cp .env.example .env` 只对全新安装成立。**已有部署 `git pull` 之后不会
自动获得新增的环境变量**——`.env` 是 `.gitignore` 掉的持久文件，`git pull`
不碰它。升级前跑一下 `diff .env .env.example`，把 diff 里只在
`.env.example` 出现的新键手动补进 `.env`（当前仓库自己的 `.env` 和
`.env.example` 之间就漏了好几个键，包括二期就加入却一直没同步的
`PPTX2PDF_CONVERT_TIMEOUT_PER_MB_S`，以及四期新增的三个管理入口变量
`PPTX2PDF_ADMIN_PASSWORD_HASH` / `PPTX2PDF_ADMIN_COOKIE_SECURE` /
`PPTX2PDF_ADMIN_SESSION_DAYS`）。漏配不会报错、不会有任何日志提示，
只会静默落回代码里的默认值——`PPTX2PDF_ADMIN_PASSWORD_HASH` 是例外，
漏配会让整个管理入口返回 503，而不是静默放行。

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
| `PPTX2PDF_ADMIN_EMAIL` | admin 引导账号的邮箱，只在库里还没有 admin 时用一次 | `admin@localhost` |
| `PPTX2PDF_ORIGIN_GUARD_ENABLED` | 防跨站白名单开关。默认关闭，且白名单为空时即使开启也放行 | `false` |
| `PPTX2PDF_READY_TTL_HOURS` | 1 | `ready` 状态任务（已传完、未点「开始转换」）的原文件保留时长，超时未开始转换会被回收为 `failed` / `READY_EXPIRED`；与管未完成上传会话的 `PPTX2PDF_UPLOAD_TTL_HOURS`（默认 24）不是一回事，两者互不影响，见上方「两段式上传（五期）」一节 |
| `PPTX2PDF_STALE_TASK_MINUTES` | 45 | 孤儿任务回收阈值，必须大于最大转换超时 |
| `PPTX2PDF_MAX_FILE_SIZE` | 629145600（600MiB） | 单次上传允许的最大原文件体积。LibreOffice 路径不受此限约束；Graph 路径的实际可用上界更低，见下一行 |
| `PPTX2PDF_SECRET_KEY` | 空 | Graph 凭证的 Fernet 主密钥。未配置则 Graph 引擎不可用 |
| `PPTX2PDF_GRAPH_MAX_PAGES_PER_SHARD` | 80 | 每片页数上限，对 Graph 的 100 页硬限留余量 |
| `PPTX2PDF_GRAPH_MAX_SHARD_BYTES` | 41943040（40MiB） | 每片体积上限，对 Graph 实测 ~50MB 失败点留余量 |
| `PPTX2PDF_GRAPH_MAX_SHARDS` | 12 | 分片数上限。本值 × `GRAPH_MAX_SHARD_BYTES` = 480MiB 是分片产物总量的上界（不是保证能过的原文件体积，见上方 Graph 通道一节），且低于 `PPTX2PDF_MAX_FILE_SIZE`（600MiB）——这是 Graph 硬限的固有后果，不要调大本值去对齐 |
| `PPTX2PDF_GRAPH_MAX_MERGE_BYTES` | 251658240（240MiB） | 合并阶段各分片 PDF 总字节上限，基于 tracemalloc（非 RSS）测得的 3.01× 倍率算出，详见下方 Graph 通道一节 |
| `PPTX2PDF_GRAPH_REQUEST_TIMEOUT_S` | 50 | 单次 Graph 转换请求超时，Graph 自身约 45 秒硬超时 + 5 秒网络余量 |
| `PPTX2PDF_GRAPH_MAX_RETRIES` | 3 | Graph 请求失败重试次数（429/5xx 退避重试）|
| `PPTX2PDF_ADMIN_PASSWORD_HASH` | 空 | 管理入口（`/admin`）口令的 scrypt 哈希。未配置则整个管理入口返回 503，生成命令见下方「管理入口」一节 |
| `PPTX2PDF_ADMIN_COOKIE_SECURE` | false | 会话 cookie 的 `Secure` 属性。当前 HTTP 部署必须保持 `false`，切到 HTTPS 后再改 `true`，见下方「管理入口」一节 |
| `PPTX2PDF_ADMIN_SESSION_DAYS` | 3 | 管理会话有效期（天）。每次通过鉴权的请求都会滑动刷新，活跃使用不掉线 |

worker 单容器内存上限硬编码在 `docker-compose.yml`（三期从 3G 提到 8G），**不通过 `.env` 控制**。这是保守取值，不是从测得的 RSS 反推的结论——上面 `GRAPH_MAX_MERGE_BYTES` 那行的 3.01× 倍率来自 tracemalloc（不含解释器基线与分配器碎片，真实 RSS 更高，且推算出的堆峰值约 720MB，原有的 3G 本就有约 4× 余量）；8G 是「提高限额 + 流式切片」两手一起上的既定方案，四期上真实租户后应实测 RSS 再回调。24GB 机器上 2 个 worker × 8G = 16G，留 8G 给 api、redis 与系统；换机器规格需要同步改 `docker-compose.yml` 里的 `memory:` 值。

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
.venv/Scripts/python -m pytest -q                   # 期望 318 passed
```

前端：

```bash
cd frontend
npm install
npm test          # 期望 84 passed
npm run dev       # 开发服务器在 5173，/api 已代理到 8000
```

本机不需要装 LibreOffice——转换只在 worker 容器里跑。

## 已知限制

- 上传/转换接口本身仍无鉴权、无配额，任何人都能上传 600MB——四期只做了
  管理入口（`/admin`）的口令鉴权与 Graph 凭证配置，普通用户侧的账号、
  配额、风控不在本期范围内
- UI 没有断点续传入口：客户端库和后端协议都支持，但按设计刷新即重置
- 任务列表只在 React state，刷新即丢，且没有列表端点可恢复
- SQLite 靠 WAL 支撑 api 与 worker 两个容器共享，**不要把 storage volume
  挂到 NFS 或对象存储 FUSE 上**，那种场景下文件锁不可靠
- 前端由 `frontend` 容器托管，镜像里是构建时的产物快照；改前端代码需要 `docker compose up -d --build frontend` 重新构建
- 转换结果只保留 24 小时（`PPTX2PDF_OUTPUT_TTL_HOURS`），过期自动清理，请
  及时下载；过期后再请求下载会返回 `RESULT_EXPIRED`，需要重新上传。原始
  pptx 在转换结束后立即删除，不论成败
- 内嵌视频/音频会在转换前被剥离，且不可逆（五期）：无论选哪个引擎，转换
  前都会统一剥掉 pptx 里的内嵌视频/音频。PDF 本来就放不了视频，所以这一
  步不会造成信息损失；但服务器上不再保留剥离前的原始文件（剥离是就地
  覆盖，不是另存一份），转换结果如有问题需要跟原件比对，请自己留一份或
  重新上传
- Graph 通道（三期）已实现且测试覆盖，但需先在 `/admin` 完成 Azure 凭证
  配置并通过五步自检才可用；未配置时选 Graph 引擎的任务会稳定收到
  `GRAPH_NOT_CONFIGURED`，详见上方「Graph 通道（三期）」与「管理入口
  （四期）」两节
- 分片转换的中间产物体积是原文件的两倍（分片 pptx + 分片 PDF），汇总后
  立即清理；worker 异常退出时残留的分片目录由保留策略按
  `PPTX2PDF_OUTPUT_TTL_HOURS` 回收

## 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 一 | 前端三端 UI + 分片上传全链路 + 元信息解析 + 占位 PDF | 完成 |
| 二 | LibreOffice 引擎 + 容器化 + 队列 + 资源治理 | 完成 |
| 三 | Microsoft Graph 引擎（小文件高保真）+ 转换切片合并 | 开发完成，未部署 |
| 四 | 管理入口（口令登录、会话）+ Azure 凭证配置与五步自检 | 开发完成，未部署（审核通过后合并） |
| 五 | 内嵌媒体（视频/音频）转换前统一剥离 + 上传后手动触发转换（`ready` 状态 + `start` 端点） | 开发完成，未部署（审核通过后合并） |
| 六 | 账号体系与 Admin 面板（用户管理 / 白名单 / 系统状态）＋ 转换后处理三件套 | 开发完成，未部署（审核通过后合并） |

设计文档见 `docs/superpowers/specs/`，实施计划见 `docs/superpowers/plans/`。


## 账号与管理面板（六期）

站点对匿名访客可用——上传、用 LibreOffice 转换、下载都不需要登录。
只有 **Microsoft Graph 通道要求登录**，因为它消耗租户配额。

**当前不开放注册。** 右上角「登录 / 注册」里的注册按钮点了只会提示联系
管理员，后端刻意没有注册端点——留一个关着的端点只是凭空多一个攻击面。

### admin 账号

数据库里还没有管理员时，启动会用 `PPTX2PDF_ADMIN_PASSWORD_HASH` 引导出
一个用户名为 `admin` 的账号。没配这个环境变量就不创建，管理入口继续 503——
绝不生成默认密码兜底。

引导之后环境变量就不再是真相源：在面板里改了密码，改的是数据库那一行，
重启不会把它冲回环境变量的值。

### 管理面板

`/admin`，四个分区：

| 分区 | 内容 |
|---|---|
| 用户管理 | 添加、暂停/激活、删除、改密码 |
| Azure 凭证 | Graph 通道配置与五步连通性自检 |
| 访问白名单 | 防跨站（**当前未启用**，见下） |
| 系统状态 | 任务统计与存储占用 |

未登录或非 admin 访问 `/admin` 会自动跳回主页。但**这只是体验**——真正的
边界是每个 `/api/admin/*` 端点上的 `require_admin`。

几条刻意的限制：不能暂停或删除自己的账号，不能删掉最后一个管理员。这个
系统没有第二个恢复入口，一次误操作就会把人锁在门外。

### 防跨站白名单

**默认关闭**（`PPTX2PDF_ORIGIN_GUARD_ENABLED=false`）。启用后只有白名单里
的来源能发起写请求（POST/PUT/PATCH/DELETE），读请求一律放行。

白名单为空时即使启用也放行，这是第二道保险：否则第一次打开开关就会把所有
写请求——包括管理员用来添加第一条白名单的那次——全部拒绝，变成无法自救的
死锁。

## 转换后处理（六期）

三个选项都在上传页的「后处理」区，可以任意组合。

### 动画分步展开

按 `p:timing` 把逐步出现的内容拆成多页：一页上按点击逐条出现的要点，会被
展开成多张，每张比上一张多一步。整个形状的进入动画与段落级（逐行）动画
都支持。

只处理**进入**动画——它是「元素出现」的唯一成因。强调、退出、路径动画不
改变可见性，对分页没有意义。

有两处会跳过并在任务上留下说明（不会静默）：含 `AlternateContent` 的页
（通常是 SmartArt 或墨迹），以及动画步骤超过 20 步的页。整份展开后超过
500 页也会整体跳过。

实测：一份 59 页的课件展开成 152 页。注意展开会显著增加页数，Graph 通道
有 100 页硬限，超出后会自动切片再合并。

### PDF 书签大纲

每页一个书签，标题取自该页的标题占位符；取不到就用「第 N 页」——缺页会让
大纲和实际页码对不上，比标题不好看糟糕得多。

### 页边距重映射

把每页往右扩宽 25%，留出批注栏。只改页面框而不动内容，矢量性、可选文字、
内部链接全部原样保留。

## 下载

超过 4 MiB 的 PDF 会分 4 块并发下载并显示百分比进度；超过 200 MiB 退回
浏览器原生下载（并发要先把整份攒进内存才能拼成文件，而原生下载是流式的）。
