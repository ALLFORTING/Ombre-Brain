FROM python:3.12.14-slim
WORKDIR /app
COPY requirements.txt constraints-py312-linux.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt -c constraints-py312-linux.txt \
    && python -c "import sys, importlib.metadata as m; assert sys.version.split()[0] == '3.12.14'; assert m.version('mcp') == '1.29.1'; assert m.version('remember-me') == '0.1.0'"
COPY . /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 CMD python /app/deploy/lsf-zeabur/healthcheck.py
CMD ["python", "-B", "/app/deploy/lsf-zeabur/c3_run.py"]
