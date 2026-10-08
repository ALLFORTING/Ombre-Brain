FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
WORKDIR /app
COPY requirements.txt constraints-py312-linux.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints-py312-linux.txt
COPY *.py ./
COPY asset_viewer.html ./
COPY deploy/backup-synthetic/run.py ./deploy/backup-synthetic/run.py
# Provider-issued build provenance only; this is not a credential argument.
ARG ZEABUR_GIT_COMMIT_SHA
RUN python -B -c 'import os,re,json,pathlib; v=os.environ.get("ZEABUR_GIT_COMMIT_SHA",""); assert re.fullmatch("[0-9a-f]{40}",v), "provider build SHA required"; pathlib.Path("/app/.backup-v2-build.json").write_text(json.dumps({"source":"zeabur-build","status":"valid","commit":v}),encoding="ascii")' \
    && groupadd --gid 10002 synthetic \
    && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin synthetic
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OB_BACKUP_SYNTHETIC_ENABLED=false
USER 10002:10002
ENTRYPOINT ["python", "-B", "/app/deploy/backup-synthetic/run.py"]
