#!/bin/sh
set -e

# 用户可能把自己的 Office 字体挂载到 /usr/share/fonts/truetype/extra，
# 挂载发生在镜像构建之后，所以字体缓存必须在容器启动时重建。
fc-cache -f >/dev/null 2>&1 || true

exec "$@"
