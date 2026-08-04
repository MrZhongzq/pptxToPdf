#!/usr/bin/env bash
#
# 一键安装 pptx2pdf。
#
#   curl -fsSL https://raw.githubusercontent.com/MrZhongzq/pptxToPdf/master/install.sh | bash
#
# 做四件事：检查 docker、生成 .env（含随机密钥与管理员口令）、拉起服务、
# 把管理员口令打印出来。全程不需要事先克隆仓库。
#
# 默认本地构建镜像。想跳过那 3-5 分钟的 LibreOffice 安装，用 ghcr 上的
# 预构建镜像（amd64 与 arm64 都有）：
#
#   curl -fsSL .../install.sh | PPTX2PDF_PREBUILT=1 bash
set -euo pipefail

REPO="${PPTX2PDF_REPO:-https://github.com/MrZhongzq/pptxToPdf.git}"
DIR="${PPTX2PDF_DIR:-pptx2pdf}"
PORT="${WEB_PORT:-18993}"
PREBUILT="${PPTX2PDF_PREBUILT:-}"
# override 文件把 build 换成 image；空串时下面的 compose 调用退化成只用主文件
COMPOSE_FILES=(-f docker-compose.yml)
if [ -n "$PREBUILT" ]; then
  COMPOSE_FILES+=(-f docker-compose.ghcr.yml)
fi

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
green(){ printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[36m%s\033[0m\n' "$*"; }

# ---- 1. 前置检查 ----

if ! command -v docker >/dev/null 2>&1; then
  red "没有找到 docker。先装 Docker Engine：https://docs.docker.com/engine/install/"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  red "没有找到 docker compose（v2）。老版本的 docker-compose 不行——"
  red "本项目用了 compose v2 的多阶段构建目标语法。"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  red "docker 守护进程没在跑，或当前用户没有权限。"
  red "试试：sudo systemctl start docker，或把自己加进 docker 组后重新登录。"
  exit 1
fi

# ---- 2. 取代码 ----

if [ -d "$DIR/.git" ]; then
  info "已存在 $DIR，拉取最新代码…"
  git -C "$DIR" pull --ff-only
else
  info "克隆到 $DIR …"
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

# ---- 3. 生成 .env ----

if [ -f .env ]; then
  info ".env 已存在，保留不动。"
else
  info "生成 .env …"
  cp .env.example .env

  # Fernet 主密钥：32 字节 urlsafe base64。client_secret 与会话签名都靠它，
  # 丢了等于所有已存的 Azure 凭证都解不开。
  SECRET_KEY=$(docker run --rm python:3.12-slim python -c \
    "from base64 import urlsafe_b64encode; import os; print(urlsafe_b64encode(os.urandom(32)).decode())")

  # 管理员口令：随机 20 位。脚本只打印一次，不写进任何文件。
  ADMIN_PASSWORD=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
  # 纯标准库，既不用挂载 backend 也不用装 cryptography——scrypt 就在 hashlib 里。
  # 参数必须与 backend/app/services/auth.py 的 _SCRYPT_* 常量保持一致。
  ADMIN_HASH=$(docker run --rm python:3.12-slim python -c "
import binascii, hashlib, os
salt = os.urandom(16)
digest = hashlib.scrypt('$ADMIN_PASSWORD'.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
print(f'scrypt:{binascii.hexlify(salt).decode()}:{binascii.hexlify(digest).decode()}')
")

  # 用 : 而不是 $ 做分隔符——Compose 会把 \$xxx 当变量插值吃掉哈希的一段
  sed -i.bak "s|^PPTX2PDF_SECRET_KEY=.*|PPTX2PDF_SECRET_KEY=$SECRET_KEY|" .env
  sed -i.bak "s|^PPTX2PDF_ADMIN_PASSWORD_HASH=.*|PPTX2PDF_ADMIN_PASSWORD_HASH=$ADMIN_HASH|" .env
  rm -f .env.bak
fi

# ---- 4. 起服务 ----

if [ -n "$PREBUILT" ]; then
  info "拉取预构建镜像…"
  docker compose "${COMPOSE_FILES[@]}" pull
else
  info "构建镜像（首次要装 LibreOffice 与字体，大约 3-5 分钟）…"
  docker compose build
fi

info "启动…"
docker compose "${COMPOSE_FILES[@]}" up -d

echo
green "======================================================"
green " 装好了"
green "======================================================"
echo
echo "  打开：  http://localhost:$PORT"
echo
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  echo "  管理员账号：admin"
  echo "  管理员口令：$ADMIN_PASSWORD"
  echo
  red  "  ↑ 这串口令只显示这一次，现在就存进密码管理器。"
  echo "    登录后可以在 admin 面板里改成自己顺手的。"
else
  echo "  沿用了已有的 .env，管理员口令没有变。"
fi
echo
echo "  常用命令（在 $DIR 目录下）："
echo "    docker compose logs -f api      # 看日志"
echo "    docker compose down             # 停"
if [ -n "$PREBUILT" ]; then
  # 预构建模式下 --build 会去本地构建，把拉下来的镜像盖掉，所以这里给的
  # 更新命令必须带上 override 文件
  echo "    docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull && \\"
  echo "      docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d   # 更新"
else
  echo "    docker compose up -d --build    # 更新后重启"
fi
echo
