# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build: docker build -t ombre-brain .
# Run:   docker run -e OMBRE_API_KEY=your-key -p 8000:8000 ombre-brain
# ============================================================

FROM python:3.12.14-slim

WORKDIR /app

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt constraints-py312-linux.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints-py312-linux.txt

# Copy project files / 复制项目文件
COPY *.py .
COPY dashboard.html .
COPY dashboard_assets.js .
COPY dashboard_assets.css .
COPY asset_viewer.html .
COPY assets ./assets
COPY config.example.yaml ./config.yaml

# Build-only provider metadata; invalid/missing input never becomes a claimed SHA.
# The record lives in the image, outside /app/buckets. No runtime SHA override.
ARG ZEABUR_GIT_COMMIT_SHA
RUN python -c 'import os,re,json,pathlib; v=os.environ.get("ZEABUR_GIT_COMMIT_SHA",""); valid=re.fullmatch("[0-9a-f]{40}",v) is not None; pathlib.Path("/app/.backup-v2-build.json").write_text(json.dumps({"source":"zeabur-build","status":"valid" if valid else ("missing" if not v else "invalid"),"commit":v if valid else None}),encoding="ascii")'

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets

EXPOSE 8000

CMD ["python", "server.py"]
