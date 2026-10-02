import os
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open("http://127.0.0.1:" + os.environ.get("PORT", "8080") + "/health", timeout=4) as response:
    assert response.status == 200
