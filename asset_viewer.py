ASSET_VIEWER_URI = "ui://remember-me/asset-viewer.html"
ASSET_VIEWER_MIME_TYPE = "text/html;profile=mcp-app"

ASSET_VIEWER_TOOL_META = {
    "ui": {"resourceUri": ASSET_VIEWER_URI},
    "ui/resourceUri": ASSET_VIEWER_URI,
}

ASSET_VIEWER_RESOURCE_META = {
    "ui": {
        "csp": {
            "connectDomains": [],
            "resourceDomains": [],
        },
        "prefersBorder": True,
    }
}

ASSET_VIEWER_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Remember-Me asset viewer</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: Canvas;
      color: CanvasText;
    }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 12px; }
    main { display: grid; gap: 10px; max-width: 100%; }
    figure { margin: 0; display: grid; gap: 10px; }
    img {
      display: none;
      width: auto;
      height: auto;
      max-width: 100%;
      max-height: min(70vh, 760px);
      margin: 0 auto;
      object-fit: contain;
    }
    h1 {
      margin: 0;
      font-size: 1rem;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    dl {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 4px 10px;
      margin: 0;
      font-size: .82rem;
      line-height: 1.4;
    }
    dt { color: color-mix(in srgb, CanvasText 65%, transparent); }
    dd { margin: 0; overflow-wrap: anywhere; }
    #tags { display: flex; flex-wrap: wrap; gap: 5px; }
    .tag {
      border: 1px solid color-mix(in srgb, CanvasText 28%, transparent);
      border-radius: 4px;
      padding: 1px 5px;
    }
    #error {
      display: none;
      margin: 0;
      color: #b42318;
      font-size: .9rem;
    }
    @media (prefers-color-scheme: dark) {
      #error { color: #ffb4ab; }
    }
  </style>
</head>
<body>
  <main>
    <p id="error" role="alert"></p>
    <figure id="viewer" hidden>
      <img id="image" alt="">
      <figcaption>
        <h1 id="title"></h1>
        <dl>
          <dt>Filename</dt><dd id="filename"></dd>
          <dt>Dimensions</dt><dd id="dimensions"></dd>
          <dt>MIME type</dt><dd id="mime"></dd>
          <dt>Tags</dt><dd id="tags"></dd>
        </dl>
      </figcaption>
    </figure>
  </main>
  <script>
    (() => {
      "use strict";
      const host = window.parent;
      const pending = new Map();
      let nextRequestId = 1;

      const errorNode = document.getElementById("error");
      const viewerNode = document.getElementById("viewer");
      const imageNode = document.getElementById("image");
      const titleNode = document.getElementById("title");
      const filenameNode = document.getElementById("filename");
      const dimensionsNode = document.getElementById("dimensions");
      const mimeNode = document.getElementById("mime");
      const tagsNode = document.getElementById("tags");

      function post(message) {
        host.postMessage(message, "*");
      }

      function request(method, params) {
        const id = nextRequestId++;
        post({ jsonrpc: "2.0", id, method, params });
        return new Promise((resolve, reject) => {
          pending.set(id, { resolve, reject });
        });
      }

      function notify(method, params) {
        post({ jsonrpc: "2.0", method, params });
      }

      function showError() {
        imageNode.removeAttribute("src");
        imageNode.style.display = "none";
        viewerNode.hidden = true;
        errorNode.textContent = "This image could not be displayed.";
        errorNode.style.display = "block";
      }

      function updateSize() {
        notify("ui/notifications/size-changed", {
          width: Math.ceil(document.documentElement.scrollWidth),
          height: Math.ceil(document.documentElement.scrollHeight)
        });
      }

      function setText(node, value) {
        node.textContent = typeof value === "string" ? value : "";
      }

      function renderToolResult(result) {
        try {
          const structured = result && result.structuredContent;
          const rememberMe = result && result._meta && result._meta.rememberMe;
          if (!structured || !rememberMe || rememberMe.schemaVersion !== 1) {
            showError();
            return;
          }
          if (!["image/jpeg", "image/png"].includes(rememberMe.mimeType)) {
            showError();
            return;
          }
          if (typeof rememberMe.imageBase64 !== "string" || !rememberMe.imageBase64) {
            showError();
            return;
          }

          const filename = typeof structured.filename === "string"
            ? structured.filename
            : "";
          const title = typeof structured.title === "string" && structured.title
            ? structured.title
            : filename;
          setText(titleNode, title);
          setText(filenameNode, filename);
          setText(
            dimensionsNode,
            Number.isInteger(structured.width) && Number.isInteger(structured.height)
              ? `${structured.width} × ${structured.height}`
              : ""
          );
          setText(mimeNode, structured.mime_type);

          tagsNode.replaceChildren();
          const tags = Array.isArray(structured.tags) ? structured.tags : [];
          for (const value of tags) {
            if (typeof value !== "string") continue;
            const item = document.createElement("span");
            item.className = "tag";
            item.textContent = value;
            tagsNode.appendChild(item);
          }

          imageNode.alt = title;
          imageNode.onload = () => {
            errorNode.style.display = "none";
            viewerNode.hidden = false;
            imageNode.style.display = "block";
            updateSize();
          };
          imageNode.onerror = showError;
          imageNode.src = `data:${rememberMe.mimeType};base64,${rememberMe.imageBase64}`;
        } catch (_error) {
          showError();
        }
      }

      window.addEventListener("message", (event) => {
        if (event.source !== host) return;
        const message = event.data;
        if (!message || message.jsonrpc !== "2.0") return;
        if (Object.prototype.hasOwnProperty.call(message, "id") && pending.has(message.id)) {
          const callback = pending.get(message.id);
          pending.delete(message.id);
          if (message.error) callback.reject(new Error("Host request failed"));
          else callback.resolve(message.result);
          return;
        }
        if (message.method === "ui/notifications/tool-result") {
          renderToolResult(message.params);
        }
      });

      request("ui/initialize", {
        protocolVersion: "2026-01-26",
        clientInfo: {
          name: "remember-me-asset-viewer",
          version: "1.0.0"
        },
        appCapabilities: {}
      }).then(() => {
        notify("ui/notifications/initialized", {});
      }).catch(showError);
    })();
  </script>
</body>
</html>
"""
