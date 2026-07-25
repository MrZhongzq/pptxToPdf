# pptx → PDF

把课程 pptx 转成能直接导入 GoodNotes / OneNote 的 PDF。

**当前进度：一期（前端 + 上传骨架）。转换引擎尚未接入，输出为占位 PDF——页数与页面尺寸真实，内容是占位文字。**

## 开发

后端：
```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
.venv/Scripts/uvicorn app.main:app --reload
```

前端：
```bash
cd frontend
npm install
npm test
npm run dev
```

前端开发服务器在 5173，`/api` 已代理到 8000。

## 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 一 | 前端三端 UI + 分片上传全链路 + 元信息解析 + 占位 PDF | 进行中 |
| 二 | LibreOffice 引擎（主力通道） | 未开始 |
| 三 | Microsoft Graph 引擎（小文件高保真）+ 转换切片合并 | 未开始 |
| 四 | 账号、配额、风控、管理面板 | 未开始 |

设计文档见 `docs/superpowers/specs/`，实施计划见 `docs/superpowers/plans/`。

## 已知限制 / 一期技术债

- **`originals/` 与 `outputs/` 无保留策略**：`_purge_expired` 只回收 `uploads/`
  下过期会话的块目录，原始 pptx 与转换出的 PDF 目前没有任何删除路径，磁盘
  会随真实使用无限增长（单节课约 80MB、半学期约 500MB，每次转换永久留下
  两份文件）。
- **无鉴权、无配额**：任何人都可以上传最大 600MB 的文件，账号/配额/风控留
  到四期。
- **`BackgroundTasks` 是单进程内存队列**：进程重启会丢失所有进行中的任务，
  这些任务会永久停在非终态（如 `parsing`），前端会无限轮询一个不再变化
  的状态。
- **UI 没有断点续传入口**：客户端库（`chunking.ts`）和后端协议都支持从中
  途续传，但目前没有任何组件把 `resumeUploadId` 传进去，`upload_id` 也没
  有持久化到本地存储——用户刷新页面后只能从 0 重传。
- **任务列表只存在于 React state**：刷新页面即丢失，也没有列表端点可以
  用来恢复历史任务。
- **业务错误码未进入 `openapi.json` 契约**：`openapi.json` 里的 422 响应
  仍然沿用 FastAPI 默认的 `HTTPValidationError` schema 声明；实际请求校验
  失败时返回的 `{"code": "VALIDATION_ERROR", ...}` 与其它业务错误码（如
  `UPLOAD_SESSION_EXPIRED`）都不会出现在这份契约文件里。
