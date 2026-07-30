# ---- 共同基座 ----
#
# 七期之前只有 worker 装 LibreOffice：转换都在后台任务里跑。v1 是同步
# 接口，转换发生在 api 进程内，所以 api 也需要这套二进制与字体——真机
# 验证时 v1 的 libreoffice 路径直接 503，就是因为 api 容器里没有 soffice。
#
# 用多阶段的第一段而不是两个文件各写一遍：字体列表是保真度的关键，两份
# 拷贝迟早会漂移，而漂移的表现是「同一份 pptx 走 webui 和走 v1 出来的
# PDF 不一样」，极难排查。docker 会自动复用这一层的缓存。
FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# libreoffice-impress 带起 Impress 的 PDF 导出过滤器。
# 字体是保真度的 80%：Carlito/Caladea/Liberation 与微软字体 metric 兼容，
# 换行位置不变；Noto CJK 保证中文不渲染成豆腐块；Noto core 覆盖希腊语
# （公式里的希腊字母）；STIX 顶替 Cambria Math。
RUN apt-get update && apt-get install -y --no-install-recommends       libreoffice-impress       libreoffice-core       fontconfig       fonts-crosextra-carlito       fonts-crosextra-caladea       fonts-liberation       fonts-liberation2       fonts-noto-cjk       fonts-noto-core       fonts-dejavu-core       fonts-stix  && rm -rf /var/lib/apt/lists/*

COPY deploy/fontconfig-local.conf /etc/fonts/local.conf
RUN mkdir -p /usr/share/fonts/truetype/extra

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ---- api ----
FROM base AS api

COPY backend/ ./

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
