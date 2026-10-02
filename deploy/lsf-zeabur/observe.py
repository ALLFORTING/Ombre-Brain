"""Metadata-only observers. Render log records in memory, scan, then discard."""
import hashlib
import logging
from collections import Counter
class SafeLogs(logging.Handler):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = list(secrets)
        self.counts = Counter()
        self.leaks = 0
        self.access_records = 0
        self.redacted_access_records = 0
    def emit(self, record):
        try:
            rendered = self.format(record)
            self.leaks += int(any(value and value in rendered for value in self.secrets))
            self.counts[record.name] += 1
            if record.name == "uvicorn.access":
                self.access_records += 1
                self.redacted_access_records += int("[redacted]" in rendered)
        except Exception:
            self.leaks += 1
    def projection(self):
        return dict(raw_value_leaked=bool(self.leaks), sentinel_checked_before_archive=True,
                    access_records=self.access_records,
                    redacted_access_records=self.redacted_access_records,
                    record_counts=dict(self.counts), raw_logs_archived=False)
def install_logging(secrets):
    handler = SafeLogs(secrets)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    return handler
class Observe:
    def __init__(self, app, rows):
        self.app, self.rows = app, rows
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from urllib.parse import parse_qs
        query = parse_qs(scope.get("query_string", b"").decode("ascii", "ignore"))
        has_query_auth = bool(query.get("token"))
        headers = dict(scope.get("headers", []))
        session = headers.get(b"mcp-session-id", b"")
        row = dict(method=scope["method"], path=scope["path"], has_query_auth=has_query_auth,
                   session_hash=hashlib.sha256(session).hexdigest()[:16] if session else None,
                   body_chunks=0)
        async def safe_send(message):
            if message["type"] == "http.response.start":
                row["status"] = message["status"]
                row["auth_result"] = "rejected" if message["status"] == 401 else "passed_middleware"
                row["event_stream"] = any(k.lower() == b"content-type" and b"text/event-stream" in v
                                          for k, v in message.get("headers", []))
            elif message["type"] == "http.response.body":
                row["body_chunks"] += 1
            await send(message)
        try:
            await self.app(scope, receive, safe_send)
        finally:
            self.rows.append(row)

