"""Test-only Linux environment, no inherited provider/config credentials."""
import os
import sys
import tempfile
import time
from pathlib import Path
BASELINE = "885807cf460bec47af09812a523677d9dbb33eba"
SOURCE = Path(__file__).resolve().parents[2]
RM_URL = "https://github.com/peanutsuee/Remember-Me/releases/download/v0.1.0/remember_me-0.1.0.tar.gz"
RM_SHA = "93d1514f940bde00a43b34b61681fe7f64da130313840f247869157d6e250485"

def configure(token, root, port):
    if not root.is_absolute() or root.is_symlink():
        raise RuntimeError("invalid_test_root")
    root.mkdir(parents=True, exist_ok=True)
    # Reject pre-existing path redirection anywhere in this dedicated volume.
    if root.resolve() != root or any(p.is_symlink() for p in root.rglob("*")):
        raise RuntimeError("test_volume_symlink")
    os.environ.clear()
    os.environ.update({
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TZ": "Asia/Shanghai",
        "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(root / "home"),
        "XDG_CACHE_HOME": str(root / "cache"), "TMPDIR": str(root / "tmp"),
        "OMBRE_TRANSPORT": "streamable-http", "OMBRE_PORT": str(port),
        "OMBRE_BUCKETS_DIR": str(root / "buckets"),
        "OMBRE_RM_DATA_ROOT": str(root / "remember-me"),
        "OMBRE_RAW_EVIDENCE_ROOT": str(root / "raw-evidence"),
        "OMBRE_RM_RUNTIME_ENABLED": "false", "OMBRE_ASSET_AUTHORITY": "legacy",
        "OMBRE_MCP_STATELESS_HTTP": "false", "OMBRE_MCP_ALLOW_ANONYMOUS_HTTP": "false",
        "OMBRE_MCP_ALLOW_QUERY_TOKEN": "true", "OMBRE_MCP_QUERY_TOKEN": token,
        "OMBRE_HOOK_SKIP": "true", "OMBRE_DIAG_TOOLS": "false",
        "OMBRE_API_KEY": "synthetic-no-external-permission",
        "OMBRE_EMBEDDING_API_KEY": "synthetic-no-external-permission",
        "OMBRE_DIGEST_API_KEY": "synthetic-no-external-permission",
        "OMBRE_BASE_URL": "http://127.0.0.1:18995/v1",
        "OMBRE_DEHYDRATION_BASE_URL": "http://127.0.0.1:18995/v1",
        "OMBRE_EMBEDDING_BASE_URL": "http://127.0.0.1:18995/v1",
        "OMBRE_DIGEST_BASE_URL": "http://127.0.0.1:18995/v1",
        "OMBRE_DEHYDRATION_MODEL": "synthetic-chat-v1",
        "OMBRE_EMBEDDING_MODEL": "synthetic-embedding-4-v1",
    })
    time.tzset()
    for name in ("home", "cache", "tmp", "remember-me", "raw-evidence"):
        (root / name).mkdir(exist_ok=True)
    tempfile.tempdir = str(root / "tmp")
    os.chdir(root)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SOURCE))
    import utils
    original_load = utils.load_config
    isolated = root / "no-config.yaml"
    if isolated.exists():
        raise RuntimeError("unexpected_test_config")
    utils.load_config = lambda config_path=None: original_load(str(isolated))
