FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# libreoffice-impress 带起 Impress 的 PDF 导出过滤器。
# 字体是保真度的 80%：Carlito/Caladea/Liberation 与微软字体 metric 兼容，
# 换行位置不变；Noto CJK 保证中文不渲染成豆腐块；Noto core 覆盖希腊语
# （公式里的希腊字母）；STIX 顶替 Cambria Math。
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress \
      libreoffice-core \
      fontconfig \
      fonts-crosextra-carlito \
      fonts-crosextra-caladea \
      fonts-liberation \
      fonts-liberation2 \
      fonts-noto-cjk \
      fonts-noto-core \
      fonts-dejavu-core \
      fonts-stix \
 && rm -rf /var/lib/apt/lists/*

COPY deploy/fontconfig-local.conf /etc/fonts/local.conf
RUN mkdir -p /usr/share/fonts/truetype/extra

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY deploy/worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh
RUN chmod +x /usr/local/bin/worker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/worker-entrypoint.sh"]
CMD ["python", "worker.py"]
