# pptx → PDF

把 PowerPoint 转成能直接导入 GoodNotes / OneNote 的 PDF。

直接把 `.pptx` 拖进浏览器导入笔记应用，常常排版错位、内容跑出屏幕、图片叠在一起。
这个工具在服务端用真正的排版引擎转换，并针对「用来做笔记」做了几项处理。

[![CI](https://github.com/MrZhongzq/pptxToPdf/actions/workflows/ci.yml/badge.svg)](https://github.com/MrZhongzq/pptxToPdf/actions/workflows/ci.yml)

---

## 它能做什么

**两个转换引擎。** LibreOffice 无页数与超时限制，长 deck 的主力；Microsoft Graph
保真度更高（需要自己的 Microsoft 365 租户），超出它 100 页的限制时自动切片再合并。

**动画分步展开。** 一页上按点击逐条出现的要点，转成 PDF 后通常全叠在一起。勾上这项
会按 `p:timing` 把它拆成多页，每页比上一页多一步——实测一份 59 页的课件展开成 152 页。

**PDF 书签大纲。** 用每页标题生成书签，在 GoodNotes 里能直接跳转。

**页边距重映射。** 每页往右扩宽 25%，给 iPad 竖屏批注留出侧边空白。只改页面框不动
内容，矢量性、可选文字、内部链接都保留。

**自动剥离内嵌视频。** PDF 本来就放不了视频，而那些字节会让文件白白撑大好几倍——
一份 83.7 MB 的课件剥离后只剩 9.6 MB。海报帧会保留，那一页显示的是视频封面而不是空白。

**HTTP 接口。** 不想开浏览器时，一条 URL 换一份 PDF，见下方 [HTTP v1 接口](#http-v1-接口)。

**九种界面语言。** 按浏览器语言自动切换；不在支持列表里的一律显示英文。

---

## 安装

需要 **Docker Engine** 与 **Docker Compose v2**。一条命令：

```bash
curl -fsSL https://raw.githubusercontent.com/MrZhongzq/pptxToPdf/master/install.sh | bash
```

脚本会克隆仓库、生成配置（含随机的主密钥与管理员口令）、构建并启动。
首次构建要装 LibreOffice 与字体，大约 3–5 分钟。

装完打开 `http://localhost:18993`。**管理员口令只在安装时打印一次**，记得存下来。

<details>
<summary>手动安装</summary>

```bash
git clone https://github.com/MrZhongzq/pptxToPdf.git
cd pptxToPdf
cp .env.example .env
```

编辑 `.env`，至少要填两项：

```bash
# Fernet 主密钥，32 字节 urlsafe base64
python -c "
import base64, os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
"

# 管理员口令的哈希。把 你的口令 换成你要用的
python -c "
import binascii, hashlib, os
salt = os.urandom(16)
d = hashlib.scrypt(b'你的口令', salt=salt, n=16384, r=8, p=1, dklen=32)
print('scrypt:' + binascii.hexlify(salt).decode() + ':' + binascii.hexlify(d).decode())
"
```

这两条只用 Python 标准库，**不需要先装项目依赖**——此刻依赖还没装，走
`from app.services.auth import hash_password` 那条路会因为 config 要
pydantic-settings 而直接报 ModuleNotFoundError。没有 Python 的话，把
`python -c` 换成 `docker run --rm python:3.12-slim python -c` 一样跑。

分隔符是 `:` 而不是 `$`——Compose 会把 `$xxx` 当变量插值，把哈希吃掉一段。

```bash
docker compose up -d --build
```
</details>

### 用预构建镜像（跳过本地构建）

镜像发布在 ghcr，同时提供 **linux/amd64** 与 **linux/arm64**——树莓派、
Oracle Ampere 之类的 ARM 机器直接可用。省掉本地装 LibreOffice 那一层
（3–5 分钟，小机器上更久）。

一键安装脚本加个环境变量即可：

```bash
curl -fsSL https://raw.githubusercontent.com/MrZhongzq/pptxToPdf/master/install.sh   | PPTX2PDF_PREBUILT=1 bash
```

已经克隆了仓库的话，用 override 文件：

```bash
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

锁版本用 `PPTX2PDF_TAG`（默认 `latest`）。标签 `1.0.0` / `1.0` / `v1.0.0`
指向同一份镜像——发布流程两种写法都推，照着 git tag 名写也能拉到。

改过代码就别用预构建镜像：它是从 master 构建的，不含本地改动。

<details>
<summary>先拉镜像、再启动（群晖 / 威联通等 NAS，或任何不想让它自己去 pull 的环境）</summary>

群晖的 Container Manager、威联通的 Container Station 这类图形界面，通常要求
**镜像已经在本地**才能建项目或选模板。三个镜像先拉下来：

```bash
docker pull ghcr.io/mrzhongzq/pptxtopdf-api:latest
docker pull ghcr.io/mrzhongzq/pptxtopdf-worker:latest
docker pull ghcr.io/mrzhongzq/pptxtopdf-frontend:latest
docker pull redis:7-alpine
```

**先生成两个必填的值。** 下面两条命令各起一个一次性容器就跑完，不需要克隆仓库、
不需要装 Python——用的全是标准库。在群晖上开个 SSH，或者用 Container Manager
自带的终端都行：

```bash
# 1. Fernet 主密钥。Azure 凭证与会话签名都靠它，丢了等于所有已存的凭证都解不开
docker run --rm python:3.12-slim python -c "
import base64, os
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
"

# 2. 管理员口令的哈希。把 你的口令 换成你要用的，输出整行照抄
docker run --rm python:3.12-slim python -c "
import binascii, hashlib, os
salt = os.urandom(16)
d = hashlib.scrypt(b'你的口令', salt=salt, n=16384, r=8, p=1, dklen=32)
print('scrypt:' + binascii.hexlify(salt).decode() + ':' + binascii.hexlify(d).decode())
"
```

第一条输出形如 `YS4FbNGa8dbePE0kS64aAV4NMrWNkaLH1snU4qn8sFY=`，
第二条形如 `scrypt:0db979ba…:8f3c21…`（三段，冒号分隔）。

分隔符是 `:` 而不是 `$`——Compose 会把 `$xxx` 当变量插值，把哈希吃掉一段。

拉完镜像、生成完这两个值之后，用下面这份自包含的 compose（不引用本仓库任何
文件，可以直接粘进群晖「项目」的编辑框），把 `<粘贴上面第 N 条的输出>` 两处
替换掉：

```yaml
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  frontend:
    image: ghcr.io/mrzhongzq/pptxtopdf-frontend:latest
    restart: unless-stopped
    ports:
      - "18993:80"
    depends_on:
      - api

  api:
    image: ghcr.io/mrzhongzq/pptxtopdf-api:latest
    restart: unless-stopped
    volumes:
      - ./storage:/app/storage
      - ./fonts-extra:/usr/share/fonts/truetype/extra:ro
    environment:
      PPTX2PDF_STORAGE_ROOT: /app/storage
      PPTX2PDF_DATABASE_URL: sqlite:////app/storage/pptx2pdf.db
      PPTX2PDF_REDIS_URL: redis://redis:6379/0
      PPTX2PDF_SECRET_KEY: <粘贴上面第 1 条的输出>
      PPTX2PDF_ADMIN_PASSWORD_HASH: <粘贴上面第 2 条的输出>
    depends_on:
      redis:
        condition: service_healthy

  worker:
    image: ghcr.io/mrzhongzq/pptxtopdf-worker:latest
    restart: unless-stopped
    volumes:
      - ./storage:/app/storage
      - ./fonts-extra:/usr/share/fonts/truetype/extra:ro
    environment:
      PPTX2PDF_STORAGE_ROOT: /app/storage
      PPTX2PDF_DATABASE_URL: sqlite:////app/storage/pptx2pdf.db
      PPTX2PDF_REDIS_URL: redis://redis:6379/0
      PPTX2PDF_SECRET_KEY: <粘贴上面第 1 条的输出>
      PPTX2PDF_ADMIN_PASSWORD_HASH: <粘贴上面第 2 条的输出>
    depends_on:
      redis:
        condition: service_healthy
```

与仓库里那份的三点差别，都是为了脱离仓库也能用：

- **卷用 `./storage` 而不是具名卷**，NAS 上更容易在文件管理器里找到、备份。
  `./storage` 与 `./fonts-extra` 两个目录要**先手工建好**，见下面的[字体](#中文字体)一节。
- **密钥直接写在 `environment` 里**，因为没有 `.env` 文件可以引用。写进去之后
  这份 compose 就含密钥了，注意别外传、别截图发出去。
- **其余环境变量都有默认值**，不用写。转换超时、分片阈值、TTL 这些想调再加。
- **worker 只起 1 个**。仓库那份默认 2 个副本、每个限 8G 内存，NAS 上通常吃不消。
  要多开就加 `deploy.replicas`。

</details>

### 常用命令

```bash
docker compose logs -f api      # 看日志
docker compose down             # 停
docker compose up -d --build    # 更新后重启
```

---

## 中文字体

pptx 只存字体名不存字形。字体缺失时渲染端会替换，而替换字体的字符宽度不同，
换行位置就变了——这是排版错位的根本原因。

- **西文接近完美**：镜像内置 Carlito / Caladea / Liberation，与 Calibri / Cambria /
  Arial 是 metric 兼容的，换行位置不变。
- **中文仍有偏差**：等线、微软雅黑受版权保护不能打进镜像，也没有 metric 兼容的
  自由替代。镜像装的是 Noto CJK，能保证中文不显示成方块，但段落换行位置会偏移。

### 放自己的字体

装好之后，字体也可以直接在 **admin 面板 → 字体** 里上传和删除，不用碰宿主机。
下面讲的是手工放置的方式——两者可以并存，面板管的是 storage 卷里的
`fonts/`，手工放的是项目下的 `fonts-extra/`，面板能看见后者但删不掉它。

要消除中文偏差，就得提供真字体。等线与微软雅黑受版权保护，不能打进镜像。

**目录不用你建**——`fonts-extra/` 随仓库一起克隆下来，里面有个说明文件。
用上面的一键脚本或 `git clone` 装的，它已经在项目根目录下了。

只有一种情况需要手工建：**不克隆仓库、直接粘 compose 用**（比如群晖那种）。
这时 `./fonts-extra` 与 `./storage` 都要在 `docker compose up` **之前**建好：

```bash
mkdir -p fonts-extra storage
```

先启动再建也不是不行，但 Docker 会先替你建一个 **root 拥有**的空目录，
之后往里拷字体会遇到权限问题，还得 `chown` 一遍——不如一开始就建好。

**放进去，然后重启：**

```bash
cp /mnt/c/Windows/Fonts/Deng*.ttf  fonts-extra/     # 等线
cp /mnt/c/Windows/Fonts/msyh*.ttc  fonts-extra/     # 微软雅黑
docker compose restart api worker
```

**必须重启。** fontconfig 的缓存是镜像构建时生成的，而你的字体是构建之后才
出现的；容器启动时会跑一次 `fc-cache -f` 重建缓存，不重启就等于没放。

**映射关系：**

| 宿主机 | 容器内 | 挂给谁 |
|---|---|---|
| `./fonts-extra` | `/usr/share/fonts/truetype/extra` | api **和** worker，只读 |

两个容器都要挂，且必须是同一个目录：网页的转换在 worker 里跑，
[HTTP v1 接口](#http-v1-接口)的转换在 api 进程内跑。只挂一边的话，同一份 pptx
走两条路会出来两份排版不一样的 PDF。

支持 `.ttf` / `.ttc` / `.otf`。放进去的字体优先于镜像内置的替代字体——
`deploy/fontconfig-local.conf` 里的映射规则用的是 `binding="weak"`，
只在真字体缺失时才兜底，不会盖掉你放的。

**确认字体真的被加载了：**

```bash
docker compose exec worker fc-list | grep -i deng
```

---

## 使用

### 网页

拖入 `.pptx` → 选引擎和后处理选项 → 点「开始转换」。

上传完成后**不会自动开始**，会停在一张就绪卡片上，引擎和选项随时能改。
就绪任务超过 1 小时不开始会被回收（原文件删除，需要重新上传），因为单份原始
文件可能有几百 MB。

**Microsoft Graph 引擎需要登录**，因为它消耗你自己租户的配额。LibreOffice 匿名可用。

### HTTP v1 接口

一次请求换一份 PDF，不需要轮询。响应直接是 PDF 字节，浏览器里打开这个 URL 就会弹下载。

**最简：**

```
http://192.0.2.10:18993/v1/convert?fileUrl=https://files.example.com/lecture.pptx
```

只给一个文件地址，其余全走默认（LibreOffice 引擎、三项后处理都不做）。

**完整——所有参数都填上：**

```
http://192.0.2.10:18993/v1/convert
  ?fileUrl=https%3A%2F%2Ffiles.example.com%2Flecture-01.pptx
  &engine=graph
  &user=alice
  &pwd=hunter2%21
  &animations=true
  &outline=true
  &margins=true
```

拼成一行（实际调用时不能有换行和空格）：

```bash
curl -OJ "http://192.0.2.10:18993/v1/convert?fileUrl=https%3A%2F%2Ffiles.example.com%2Flecture-01.pptx&engine=graph&user=alice&pwd=hunter2%21&animations=true&outline=true&margins=true"
```

`-OJ` 让 curl 用服务端给的文件名保存（`lecture-01.pdf`）。不加的话会打印一堆二进制到终端。

**用 LibreOffice 的话不需要账号：**

```bash
curl -OJ "http://192.0.2.10:18993/v1/convert?fileUrl=https%3A%2F%2Ffiles.example.com%2Flecture-01.pptx&engine=libreoffice&animations=true&outline=true&margins=true"
```

#### 参数

| 参数 | 必填 | 取值 | 说明 |
|---|---|---|---|
| `fileUrl` | ✅ | URL | 要转换的 pptx 的**公网**地址。必须是 http/https，且不能指向内网 |
| `engine` | | `libreoffice`（默认）/ `graph` | 用哪个引擎转 |
| `user` | 用 graph 时 ✅ | 用户名 | 在管理面板里创建的账号 |
| `pwd` | 用 graph 时 ✅ | 密码 | 同上 |
| `animations` | | `true` / `false`（默认） | 按 `p:timing` 展开动画分步，一页变多页 |
| `outline` | | `true` / `false`（默认） | 用每页标题生成 PDF 书签 |
| `margins` | | `true` / `false`（默认） | 每页右侧扩宽 25%，留批注空间 |

**URL 编码**：`fileUrl` 里的 `://` 和 `/`、密码里的 `!` `@` `#` 这类字符要转义，
否则会被当成查询串的分隔符截断。上面例子里 `hunter2!` 编码成了 `hunter2%21`。

用 Python 拼参数最省心：

```python
import urllib.parse, urllib.request

params = urllib.parse.urlencode({
    "fileUrl": "https://files.example.com/lecture-01.pptx",
    "engine": "graph",
    "user": "alice",
    "pwd": "hunter2!",
    "animations": "true",
    "outline": "true",
    "margins": "true",
})
url = f"http://192.0.2.10:18993/v1/convert?{params}"
urllib.request.urlretrieve(url, "lecture-01.pdf")
```

#### 返回什么

成功是 `200` + `Content-Type: application/pdf`，文件名在 `Content-Disposition` 里。

出错是 JSON：

```json
{"code": "CROSS_ORIGIN_BLOCKED", "message": "来源不在 v1 白名单中"}
```

| 状态码 | code | 通常是 |
|---|---|---|
| 400 | `URL_NOT_ALLOWED` | `fileUrl` 指向内网，或不是 http/https |
| 401 | `AUTH_REQUIRED` | 用了 graph 但没给 user/pwd，或密码错 |
| 403 | `CROSS_ORIGIN_BLOCKED` | 调用方不在白名单里 ← **最常见** |
| 403 | `ORIGIN_BLOCKED` | 调用方在黑名单里 |
| 403 | `ENGINE_NOT_ALLOWED` | 白名单规则带了 `@no_graph` 却请求了 graph |
| 413 | `DOWNLOAD_TOO_LARGE` | 源文件超过上限 |
| 422 | `VALIDATION_ERROR` | 参数不合法，比如 `engine` 拼错 |
| 502 | `DOWNLOAD_FAILED` | 源站不通或返回了错误 |

#### 两件容易踩的事

**v1 默认拒绝所有来源。** 装好之后第一次调用一定是 403 —— 要先在管理面板的
「访问白名单」里加上调用方的 IP 或域名。网页不受这份白名单影响，所以浏览器
里能用不代表 v1 能用。

**凭据走查询串**会进服务器访问日志与浏览器历史。建议只在受控网络里用，
或者给调用方单独开一个只用于 v1 的账号。

---

## 管理面板

`/admin`，需要管理员账号。五个分区：

| 分区 | 内容 |
|---|---|
| 用户管理 | 添加、暂停、删除账号，改密码 |
| Azure 凭证 | Microsoft Graph 通道配置与五步连通性自检 |
| 访问白名单 | 谁能调用 v1 接口 |
| 网站黑名单 | 直接封禁，网页与 v1 一起拦 |
| 系统状态 | 任务统计与存储占用 |

**当前不开放注册**，账号由管理员在面板里创建。

### 访问控制

两份名单，语法相同，作用域与优先级不同：

| | 管什么 | 命中后 | 优先级 |
|---|---|---|---|
| **黑名单** | 网页 + v1 | 403 | **最高**，先查它 |
| **白名单** | 只有 v1 | 放行 | 黑名单之后 |

```
example.com                     精确匹配
*.example.com                   任意子域（不含 example.com 本身）
203.0.113.7                     IP
*.a.com||@except{x.a.com}       通配但排除某些子域
*.a.com||@match{api.a.com}      范围内只允许列出的
a.com||@no_graph                该来源不得使用 graph 引擎
```

> 配白名单时记得把自己的地址加进去，别把自己关在门外。

---

## 配置

改 `.env`，改完 `docker compose up -d` 生效。常用的几项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `WEB_PORT` | 对外端口 | `18993` |
| `PPTX2PDF_SECRET_KEY` | Fernet 主密钥，**必填** | 无 |
| `PPTX2PDF_ADMIN_PASSWORD_HASH` | 管理员口令哈希，**必填** | 无 |
| `PPTX2PDF_MAX_FILE_SIZE` | 单文件上限 | 600 MiB |
| `PPTX2PDF_READY_TTL_HOURS` | 就绪任务的原文件保留时长 | 1 |
| `PPTX2PDF_OUTPUT_TTL_HOURS` | 转好的 PDF 保留时长 | 24 |
| `WORKER_REPLICAS` | 转换 worker 数量 | 2 |

两个必填项**没有默认值**：没配就起不来，而不是悄悄用一个人人都知道的默认密码。

完整列表见 [.env.example](.env.example)。

---

## 开发

```bash
# 后端
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q

# 前端
cd frontend
npm install
npm test -- --run
npm run lint
npm run build
```

后端 500+ 测试、前端 149 测试，CI 里还会校验 OpenAPI 快照与语言文件是否与代码对齐。

### 架构

```
frontend  (nginx)  ──►  api  (FastAPI)  ──►  redis  ──►  worker × N
                          │                                 │
                          └──────►  SQLite + 文件存储  ◄─────┘
```

- **api** 处理上传、鉴权、任务查询，以及同步的 v1 接口
- **worker** 跑异步转换；长 deck 会被切成多片并行转，再合并
- **api 与 worker 共用同一个基础镜像**（含 LibreOffice 与字体）——v1 的转换发生在
  api 进程内，两边的字体必须完全一致，否则同一份 pptx 走网页和走 v1 会出来两份不同的 PDF

主要目录：

| 路径 | 内容 |
|---|---|
| `backend/app/api/` | HTTP 端点 |
| `backend/app/services/` | 转换流水线、引擎、OPC 重写、访问控制 |
| `frontend/src/components/` | 界面组件 |
| `frontend/src/i18n/` | 多语言，`locales/*.json` |
| `deploy/` | Dockerfile 与 nginx 配置 |
| `docs/DEVLOG.md` | 逐期的设计记录与取舍说明 |

### 多语言

`zh-CN` 与 `en` 是人工维护的，其余语言由脚本从 `en.json` 机器翻译生成：

```bash
python frontend/scripts/translate_locales.py --check   # 校验是否与 en.json 对齐
pip install argostranslate
python frontend/scripts/translate_locales.py           # 翻译缺失的条目
```

翻译用 [Argos Translate](https://github.com/argosopentech/argos-translate) **在本地跑**，
不调任何第三方 API、不需要密钥。模型首次运行时自动下载，每门约 100 MB。

译文里的 `{name}` 占位符会被校验：没能原样保留的条目自动退回英文原文，
而不是产出一条插值静默失效的文案。个别短语模型有稳定的坏输出（比如韩语
把「Login required」译成「姓名 *」），在脚本的 `OVERRIDES` 里定点纠正。

改了界面文案要同步更新 `zh-CN.json` 与 `en.json` 两份，CI 会校验 key 是否一致。

---

## 已知限制

- **Microsoft Graph** 需要自己的 Microsoft 365 租户与一个 Azure 应用注册，
  在管理面板里配置。没配的话只有 LibreOffice 可用。
- **动画展开**只处理「进入」动画。含 SmartArt 或墨迹的页、以及动画步骤超过 20 步的页
  会跳过并在任务上留下说明。
- **内嵌视频与音频不可逆地被剥离**。PDF 本来也放不了它们，所以转换结果没有损失，
  但服务器上不再保留原始文件——需要对照原件的话得自己留一份。
- **中文换行位置**与 PowerPoint 有偏差，除非自己提供字体，见上文。

---

## 许可

MIT
