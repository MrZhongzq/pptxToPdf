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
