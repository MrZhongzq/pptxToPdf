#!/bin/sh
set -e

# 用户挂载到 /usr/share/fonts/truetype/extra 的字体是在镜像构建**之后**
# 才出现的，fontconfig 的缓存是构建时生成的，不重建就等于没挂。
#
# api 与 worker 共用这一个脚本：v1 的转换在 api 进程内跑，两边看到的
# 字体必须完全一致，否则同一份 pptx 走网页和走 v1 会出来两份不同的 PDF。
fc-cache -f >/dev/null 2>&1 || true

exec "$@"
