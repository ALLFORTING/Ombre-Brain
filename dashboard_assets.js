(function () {
  "use strict";

  const DEFAULT_LIMIT = 12;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function formatDate(value) {
    if (!value) return "未记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function addField(container, label, value) {
    const field = element("div", "rm-asset-field");
    field.append(element("span", "rm-asset-field-label", label));
    field.append(element("span", "rm-asset-field-value", value));
    container.append(field);
  }

  function renderTags(tags) {
    const container = element("div", "rm-asset-tags");
    (tags || []).forEach(function (tag) {
      container.append(element("span", "rm-asset-tag", tag));
    });
    if (!container.childNodes.length) {
      container.append(element("span", "rm-asset-tag-empty", "暂无标签"));
    }
    return container;
  }

  function createImage(asset, className, fallbackText) {
    const frame = element("div", className);
    const image = document.createElement("img");
    image.alt = asset.title || asset.filename || "图片资产";
    image.loading = "lazy";
    image.src = asset.thumbnail_url;
    const fallback = element("div", "rm-asset-image-fallback", fallbackText);
    fallback.hidden = true;
    image.addEventListener("error", function () {
      image.hidden = true;
      fallback.hidden = false;
    });
    frame.append(image, fallback);
    return frame;
  }

  function createBrowser(options) {
    const root = options.root;
    const fetcher = options.fetcher || window.fetch.bind(window);
    const apiBase = options.apiBase || "/api/assets";
    const state = {
      query: "",
      tag: "",
      offset: 0,
      limit: options.limit || DEFAULT_LIMIT,
      total: 0,
      loading: false,
    };

    const toolbar = element("form", "rm-asset-toolbar");
    const queryInput = document.createElement("input");
    queryInput.type = "search";
    queryInput.placeholder = "搜索标题、描述、标签或文件名";
    queryInput.setAttribute("aria-label", "搜索图片资产");
    const tagInput = document.createElement("input");
    tagInput.type = "search";
    tagInput.placeholder = "标签筛选";
    tagInput.setAttribute("aria-label", "按标签筛选");
    const searchButton = element("button", "rm-asset-search-button", "搜索");
    searchButton.type = "submit";
    const clearButton = element("button", "rm-asset-clear-button", "清除");
    clearButton.type = "button";
    toolbar.append(queryInput, tagInput, searchButton, clearButton);

    const summary = element("div", "rm-asset-summary");
    const body = element("div", "rm-asset-body");
    const pager = element("div", "rm-asset-pager");
    const previous = element("button", "rm-asset-page-button", "上一页");
    previous.type = "button";
    const pageText = element("span", "rm-asset-page-text", "");
    const next = element("button", "rm-asset-page-button", "下一页");
    next.type = "button";
    pager.append(previous, pageText, next);

    const overlay = element("div", "rm-asset-detail-overlay");
    overlay.hidden = true;
    const detail = element("section", "rm-asset-detail");
    detail.setAttribute("role", "dialog");
    detail.setAttribute("aria-modal", "true");
    detail.setAttribute("aria-label", "图片资产详情");
    const closeButton = element("button", "rm-asset-detail-close", "关闭");
    closeButton.type = "button";
    const detailBody = element("div", "rm-asset-detail-body");
    detail.append(closeButton, detailBody);
    overlay.append(detail);

    root.replaceChildren(toolbar, summary, body, pager, overlay);

    function renderState(message, kind) {
      body.replaceChildren(element("div", "rm-asset-state " + kind, message));
    }

    function updatePager() {
      const first = state.total === 0 ? 0 : state.offset + 1;
      const last = Math.min(state.offset + state.limit, state.total);
      pageText.textContent = first + "–" + last + " / " + state.total;
      previous.disabled = state.offset === 0 || state.loading;
      next.disabled = state.offset + state.limit >= state.total || state.loading;
      pager.hidden = state.total === 0;
    }

    function renderCard(asset) {
      const card = element("article", "rm-asset-card");
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", "查看 " + (asset.title || asset.filename));
      card.append(createImage(asset, "rm-asset-thumbnail", "缩略图无法加载"));

      const copy = element("div", "rm-asset-card-copy");
      copy.append(element("h3", "rm-asset-title", asset.title || asset.filename));
      copy.append(element(
        "p",
        "rm-asset-description",
        asset.description || "暂无描述"
      ));
      copy.append(renderTags(asset.tags));
      copy.append(element(
        "p",
        "rm-asset-facts",
        asset.mime_type + " · " + asset.width + " × " + asset.height +
          " · " + formatBytes(asset.stored_bytes)
      ));
      copy.append(element(
        "p",
        "rm-asset-date",
        "更新于 " + formatDate(asset.updated_at)
      ));
      card.append(copy);

      function open() {
        openDetail(asset.asset_id);
      }
      card.addEventListener("click", open);
      card.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
      return card;
    }

    function renderResults(payload) {
      state.total = payload.total;
      state.offset = payload.offset;
      summary.textContent = payload.total
        ? "共 " + payload.total + " 张已清理并持久保存的图片"
        : "";
      if (!payload.results.length) {
        const filtered = Boolean(state.query || state.tag);
        renderState(
          filtered
            ? "没有符合当前搜索条件的图片。"
            : "图片库还是空的。通过 Remember-Me 保存的图片会出现在这里。",
          filtered ? "no-results" : "empty"
        );
      } else {
        const grid = element("div", "rm-asset-grid");
        payload.results.forEach(function (asset) {
          grid.append(renderCard(asset));
        });
        body.replaceChildren(grid);
      }
      updatePager();
    }

    async function load() {
      if (state.loading) return;
      state.loading = true;
      updatePager();
      renderState("正在加载图片资产…", "loading");
      const params = new URLSearchParams({
        limit: String(state.limit),
        offset: String(state.offset),
      });
      if (state.query) params.set("q", state.query);
      if (state.tag) params.set("tag", state.tag);
      try {
        const response = await fetcher(apiBase + "?" + params.toString());
        if (!response) return;
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "request_failed");
        renderResults(payload);
      } catch (error) {
        state.total = 0;
        summary.textContent = "";
        renderState("图片资产加载失败，请稍后重试。", "error");
        updatePager();
      } finally {
        state.loading = false;
        updatePager();
      }
    }

    async function openDetail(assetId) {
      overlay.hidden = false;
      detailBody.replaceChildren(element("div", "rm-asset-state loading", "正在加载详情…"));
      try {
        const response = await fetcher(apiBase + "/" + encodeURIComponent(assetId));
        if (!response) return;
        const asset = await response.json();
        if (!response.ok) throw new Error(asset.error || "request_failed");

        const imageFrame = element("div", "rm-asset-detail-image");
        const image = document.createElement("img");
        image.alt = asset.title || asset.filename;
        image.src = asset.image_url;
        const fallback = element("div", "rm-asset-image-fallback", "图片无法加载");
        fallback.hidden = true;
        image.addEventListener("error", function () {
          image.hidden = true;
          fallback.hidden = false;
        });
        imageFrame.append(image, fallback);

        const title = element("h2", "rm-asset-detail-title", asset.title || asset.filename);
        const description = element(
          "p",
          "rm-asset-detail-description",
          asset.description || "暂无描述"
        );
        const fields = element("div", "rm-asset-detail-fields");
        addField(fields, "文件名", asset.filename);
        addField(fields, "格式", asset.mime_type);
        addField(fields, "尺寸", asset.width + " × " + asset.height);
        addField(fields, "存储大小", formatBytes(asset.stored_bytes));
        addField(fields, "创建时间", formatDate(asset.created_at));
        addField(fields, "更新时间", formatDate(asset.updated_at));
        detailBody.replaceChildren(
          imageFrame,
          title,
          description,
          renderTags(asset.tags),
          fields
        );
      } catch (error) {
        detailBody.replaceChildren(
          element("div", "rm-asset-state error", "资产详情加载失败。")
        );
      }
    }

    toolbar.addEventListener("submit", function (event) {
      event.preventDefault();
      state.query = queryInput.value.trim();
      state.tag = tagInput.value.trim();
      state.offset = 0;
      load();
    });
    clearButton.addEventListener("click", function () {
      queryInput.value = "";
      tagInput.value = "";
      state.query = "";
      state.tag = "";
      state.offset = 0;
      load();
    });
    previous.addEventListener("click", function () {
      state.offset = Math.max(0, state.offset - state.limit);
      load();
    });
    next.addEventListener("click", function () {
      state.offset += state.limit;
      load();
    });
    closeButton.addEventListener("click", function () {
      overlay.hidden = true;
    });
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) overlay.hidden = true;
    });

    return {
      load: load,
      getState: function () {
        return Object.assign({}, state);
      },
    };
  }

  window.RememberMeAssetBrowser = {
    create: createBrowser,
  };
})();
