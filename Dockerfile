# --- Stage 1: Build Frontend ---
FROM node:20-alpine AS builder

WORKDIR /app/web

# 仅复制 package.json 和 package-lock.json 以利用Docker缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci

# 复制前端源代码
COPY web/ ./

# 执行构建
RUN npm run build


# --- Stage 2:backend-builder ---
# 使用官方 Python 镜像专门编译 Nuitka 模块
FROM l429609201/su-exec:su-exec AS backend-builder

# 安装编译所需的依赖
RUN apt-get update && apt-get install -y --no-install-recommends build-essential python3-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /backend-build

# --- 精确复制编译所需的文件 ---
# 创建 src 目录结构
RUN mkdir -p src
# 只复制 rate_limiter.py 及其直接和间接依赖的本地模块
COPY src/rate_limiter.py ./src/
COPY src/crud.py ./src/
COPY src/scraper_manager.py ./src/
COPY src/timezone.py ./src/
COPY src/orm_models.py ./src/
COPY src/models.py ./src/
COPY src/config.py ./src/

# --- 最小化安装编译所需的 Python 包 ---
RUN pip install --no-cache-dir nuitka sqlalchemy gmssl

# 使用 --mount=type=secret 安全地挂载密钥，并替换占位符
RUN --mount=type=secret,id=XOR_KEY_SECRET \
    sh -c 'XOR_KEY_VALUE=$(cat /run/secrets/XOR_KEY_SECRET) && sed -i "s|__XOR_KEY_PLACEHOLDER__|${XOR_KEY_VALUE}|g" src/rate_limiter.py'

# 编译 rate_limiter.py。移除 --include-package=src 以避免将整个应用打包进去。
RUN python3 -m nuitka --module src/rate_limiter.py --output-dir=.

# --- Stage 3: Python Dependency Builder ---
FROM l429609201/su-exec:su-exec AS python-builder

# 安装编译Python包所需的构建时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libpq-dev \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# 创建一个目录用于存放安装的包
WORKDIR /install
COPY requirements.txt .
# 将所有包安装到当前目录 (/install)
RUN pip install --no-cache-dir -r requirements.txt --target .


# --- Stage 4: Final Python Application ---
FROM l429609201/su-exec:su-exec


# 设置环境变量，防止生成 .pyc 文件并启用无缓冲输出
# 设置时区为亚洲/上海，以确保日志等时间正确显示
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV TZ=Asia/Shanghai
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

# 设置工作目录
WORKDIR /app

# 仅安装运行时的系统依赖，不再需要 build-essential, python3-dev 等
RUN set -ex \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata \
        iputils-ping \
        libmariadb3 \
        libpq5 \
    && addgroup --gid 1000 appgroup \
    && adduser --shell /bin/sh --disabled-password --uid 1000 --gid 1000 appuser \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 从 python-builder 阶段将安装好的包复制到系统 site-packages 目录
# 注意：路径中的 python3.11 需要与基础镜像的Python版本匹配
COPY --from=python-builder /install /usr/local/lib/python3.11/site-packages

# 复制应用代码
COPY src/ ./src/
COPY static/ ./static/
COPY config/ ./config/
COPY exec.sh /exec.sh
COPY run.sh /run.sh
RUN chmod +x /exec.sh /run.sh

# 从 backend-builder 阶段复制编译好的 .so 文件
COPY --from=backend-builder /backend-build/rate_limiter.*.so ./src/rate_limiter.so

# 移除 rate_limiter.py 源码
RUN rm -f src/rate_limiter.py

# 从 'builder' 阶段复制构建好的前端静态文件
COPY --from=builder /app/web/dist ./web/dist/

# 更改工作目录所有权为新创建的用户
RUN chown -R appuser:appgroup /app

# 暴露应用运行的端口
EXPOSE 7768

# 运行应用的默认命令

CMD ["/exec.sh"]
