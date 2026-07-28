# pptx → PDF

把课程 pptx 转成能直接导入 GoodNotes / OneNote 的 PDF。

**当前进度：二期（LibreOffice 引擎）。** 一期的占位 PDF 已被真实转换取代。

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
| `PPTX2PDF_OUTPUT_TTL_HOURS` | 24 | 输出 PDF 保留时长，过期自动清理 |
| `PPTX2PDF_STALE_TASK_MINUTES` | 45 | 孤儿任务回收阈值，必须大于最大转换超时 |

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

后端（一期的 49 个测试是回归网，二期不新增测试）：

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Linux 用 .venv/bin/
.venv/Scripts/python -m pytest -q                   # 期望 49 passed
```

前端：

```bash
cd frontend
npm install
npm test          # 期望 19 passed
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

## 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 一 | 前端三端 UI + 分片上传全链路 + 元信息解析 + 占位 PDF | 完成 |
| 二 | LibreOffice 引擎 + 容器化 + 队列 + 资源治理 | 进行中 |
| 三 | Microsoft Graph 引擎（小文件高保真）+ 转换切片合并 | 未开始 |
| 四 | 账号、配额、风控、管理面板 | 未开始 |

设计文档见 `docs/superpowers/specs/`，实施计划见 `docs/superpowers/plans/`。
