# 前端多阶段镜像：node 里构建，nginx 里托管。
#
# 这样部署机不需要装 Node——只要有 Docker 就能起全栈。
# 构建阶段的 node_modules 不会进最终镜像，产物只有 dist 加 nginx。

FROM node:22-slim AS build

WORKDIR /app

# 先只拷依赖清单，让 npm ci 这一层能被缓存——改源码不会重装依赖
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine

COPY deploy/nginx-frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
