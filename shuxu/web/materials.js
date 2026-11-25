"use strict";

const MATERIAL_PAGES = {
  inspirations: {
    title: "灵感收件箱", description: "随手保存想法、摘句、链接和截图",
    actions: ["add_inspiration", "search_inspirations", "update_inspiration", "remove_inspiration"],
    heading: "收一条灵感", label: "灵感", empty: "还没有灵感",
    fields: [
      ["kind", "类型", "kind", true], ["text", "内容", "textarea", false],
      ["source", "来源", "text", false], ["url", "原始链接", "url", false],
    ],
  },
  clippings: {
    title: "社会话题剪藏", description: "把原始来源与自己的批注放在一起",
    actions: ["save_clipping", "list_clippings", "update_clipping", "remove_clipping"],
    heading: "保存一条剪藏", label: "剪藏", empty: "还没有剪藏",
    fields: [
      ["title", "标题", "text", true], ["url", "原始链接", "url", true],
      ["published_on", "发布日期", "date", true], ["source", "出处", "text", true],
      ["note", "我的批注", "textarea", false],
    ],
  },
  resume: {
    title: "思考续接", description: "从上次留下的一句话和待想问题接着想",
    actions: ["save_resume_card", "list_resume_cards", "update_resume_card", "clear_resume_card"],
    heading: "留一张续接卡", label: "续接卡", empty: "还没有续接卡",
    fields: [
      ["title", "标题", "text", true], ["note", "留给自己的一句话", "textarea", true],
      ["question", "下次想继续的问题", "textarea", true],
    ],
  },
};
const MATERIAL_ROUTES = Object.fromEntries(Object.entries(MATERIAL_PAGES).map(([key, page]) => [key, { title: page.title, description: page.description }]));
const MATERIAL_KINDS = { thought: "想法", quote: "摘句", link: "链接", screenshot: "截图" };
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const MATERIAL_PAGE_SIZE = 20;
let captureFocus = false;

document.addEventListener("keydown", function (event) {
  if (!event.altKey || event.ctrlKey || event.shiftKey || event.key.toLowerCase() !== "i") return;
  event.preventDefault();
  captureFocus = true;
  if (location.hash === "#inspirations") {
    document.getElementById("material-text")?.focus();
    captureFocus = false;
  } else {
    location.hash = "inspirations";
  }
});

function materialField([key, label, type, required], value) {
  const attr = ' id="material-' + key + '" name="' + key + '"' + (required ? " required" : "");
  const escaped = escapeHTML(value || "");
  let control;
  if (type === "textarea") {
    control = "<textarea" + attr + ' rows="3" maxlength="500">' + escaped + "</textarea>";
  } else if (type === "kind") {
    control = "<select" + attr + ">" + Object.entries(MATERIAL_KINDS).map(([kind, text]) =>
      '<option value="' + kind + '"' + (kind === value ? " selected" : "") + ">" + escapeHTML(text) + "</option>"
    ).join("") + "</select>";
  } else {
    control = "<input" + attr + ' type="' + type + '" value="' + escaped + '"' +
      (type === "url" ? ' maxlength="2048"' : type === "text" ? ' maxlength="160"' : "") + ">";
  }
  return '<div class="field"><label for="material-' + key + '">' + label + "</label>" + control + "</div>";
}

function materialRow(item, route, preferences) {
  const title = item.title || (item.text ? item.text.split(/\r?\n/, 1)[0] : "") || item.source || MATERIAL_KINDS[item.kind];
  const topic = preferences.topics.find(value => value.id === item.topic_id);
  const origin = [item.source, item.published_on, topic?.name || (item.direction && directionName(item.direction))].filter(Boolean).join(" · ");
  const focus = item.title || item.text || item.source || item.url;
  const isLink = /^https?:\/\//i.test(item.url || "");
  return '<article class="material-row" data-material-id="' + escapeHTML(item.id) + '">' +
    '<div class="material-row-main">' +
      (route === "inspirations" ? '<span class="material-type">' + escapeHTML(MATERIAL_KINDS[item.kind]) + "</span>" : "") +
      '<h3>' + escapeHTML(title) + "</h3>" +
      (origin ? '<p class="material-source">' + escapeHTML(origin) + "</p>" : "") +
      (route === "inspirations" && item.text && item.text !== title ? '<p class="material-copy">' + escapeHTML(item.text) + "</p>" : "") +
      (item.note ? '<p class="material-copy">' + escapeHTML(item.note) + "</p>" : "") +
      (item.question ? '<p class="material-question"><span>待想</span>' + escapeHTML(item.question) + "</p>" : "") +
      (item.prompt_id ? '<p class="material-source">关联命题 ' + escapeHTML(item.prompt_id) + "</p>" : "") +
      (isLink ? '<a class="material-link" href="' + escapeHTML(item.url) + '" target="_blank" rel="noopener noreferrer">打开来源</a>' : "") +
      (item.has_image ? '<figure class="material-image" hidden><img alt="灵感截图"></figure>' : "") +
    "</div>" +
    '<div class="material-row-actions">' +
      (item.has_image ? '<button class="button text-button" type="button" data-material-image>查看截图</button>' : "") +
      (focus ? '<button class="button text-button" type="button" data-material-draft>用这条拟题</button>' : "") +
      '<button class="button text-button" type="button" data-material-edit>编辑</button>' +
      '<button class="button text-button" type="button" data-material-remove>删除</button>' +
    "</div>" +
  "</article>";
}

function readImage(file) {
  return new Promise(function (resolve, reject) {
    const reader = new FileReader();
    reader.addEventListener("load", function () {
      resolve({ image_type: file.type, image_data: reader.result.split(",", 2)[1] });
    });
    reader.addEventListener("error", reject);
    reader.readAsDataURL(file);
  });
}

async function renderMaterialPage(route, generation) {
  const page = MATERIAL_PAGES[route];
  const preferences = await getPreferences(true, generation);
  if (generation !== state.pageGeneration) return;
  const [create, search, update, remove] = page.actions;
  let offset = 0;
  let query = "";
  let chosenDirection = "";
  const link = state.materialLink?.route === route ? state.materialLink.prompt : null;
  let rowsGeneration = 0;
  const initialRowsGeneration = ++rowsGeneration;
  const initialParams = { limit: MATERIAL_PAGE_SIZE, offset };
  const initialData = await postAction(search, initialParams);
  if (generation !== state.pageGeneration || initialRowsGeneration !== rowsGeneration) return;
  if (link) state.materialLink = null;

  pageContent.innerHTML =
    '<div class="material-layout page-section">' +
      '<section class="panel material-editor" aria-label="录入"></section>' +
      '<section class="panel material-library">' +
        '<div class="panel-heading"><h2>' + page.label + '记录</h2></div>' +
        '<form id="material-search" class="material-search">' +
          '<div class="field"><label for="material-query">搜索</label><input id="material-query" name="query" type="search" maxlength="160"></div>' +
          '<div class="field"><label for="material-direction-filter">方向</label><select id="material-direction-filter" name="direction">' + directionOptions("", true) + '</select></div>' +
          '<button class="button" type="submit">查找</button>' +
        '</form>' +
        '<div id="material-list" class="material-list"></div>' +
        '<div id="material-pages" class="pagination" hidden><button id="material-prev" type="button" class="button">上一页</button><button id="material-next" type="button" class="button">下一页</button></div>' +
      '</section>' +
    '</div>';
  const root = $(".material-layout");
  const editor = $(".material-editor", root);
  const list = $("#material-list", root);
  const searchForm = $("#material-search", root);
  const prev = $("#material-prev", root);
  const next = $("#material-next", root);

  function showEditor(item) {
    const editing = Boolean(item?.id);
    const current = item || {};
    const direction = current.direction || (route === "clippings" ? "social_topics" : "");
    const topic = current.topic_id || "";
    editor.innerHTML = '<div class="panel-heading"><h2>' + (editing ? "编辑" + page.label : page.heading) + '</h2></div>' +
      '<form id="material-form" class="material-form">' +
        page.fields.map(field => materialField(field, current[field[0]])).join("") +
        '<div class="field-grid">' +
          '<div class="field"><label for="material-direction">方向</label><select id="material-direction" name="direction">' + directionOptions(direction, true).replace("全部方向", "不指定方向") + '</select></div>' +
          '<div class="field"><label for="material-topic">主题</label><select id="material-topic" name="topic">' + topicOptions(preferences, direction, topic, false) + '</select></div>' +
        '</div>' +
        '<div class="field"><label for="material-prompt">关联命题编号</label><input id="material-prompt" name="prompt_id" maxlength="200" value="' + escapeHTML(current.prompt_id || "") + '"></div>' +
        (route === "inspirations" ? '<div class="field image-field"><label for="material-image-input">截图</label><input id="material-image-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp"><span id="material-image-selection" class="material-source">' + (current.has_image ? "已有截图，可替换" : "也可在表单内粘贴截图，最大 5 MB") + '</span></div>' : "") +
        '<div class="button-row"><button id="material-save" type="submit" class="button primary">' + (editing ? "保存更改" : "保存" + page.label) + '</button>' +
          (editing ? '<button id="material-cancel" class="button text-button" type="button">取消编辑</button>' : "") +
          (current.has_image ? '<button id="material-remove-image" class="button text-button" type="button">移除截图</button>' : "") +
        '</div>' +
      '</form>';
    const form = $("#material-form", editor);
    const save = $("#material-save", editor);
    let image = null;
    resetRetryOnChange(form, save);
    form.elements.direction.addEventListener("change", function () {
      form.elements.topic.innerHTML = topicOptions(preferences, this.value, "", false);
    });

    function selectImage(file) {
      if (!file) return;
      if (!IMAGE_TYPES.includes(file.type) || file.size > 5 * 1024 * 1024) {
        showNotice("请选择不超过 5 MB 的 PNG、JPEG、GIF 或 WebP 图片。", "error");
        return;
      }
      image = file;
      form.elements.kind.value = "screenshot";
      delete save.dataset.requestId;
      $("#material-image-selection", editor).textContent = file.name || "已粘贴截图";
      clearNotice();
    }

    if (route === "inspirations") {
      $("#material-image-input", editor).addEventListener("change", function () { selectImage(this.files[0]); });
      form.addEventListener("paste", function (event) {
        const pastedImage = Array.from(event.clipboardData?.files || []).find(file => IMAGE_TYPES.includes(file.type));
        if (pastedImage) {
          event.preventDefault();
          selectImage(pastedImage);
        }
      });
    }
    $("#material-cancel", editor)?.addEventListener("click", function () { showEditor(); });
    $("#material-remove-image", editor)?.addEventListener("click", function () {
      if (!window.confirm("移除这条灵感的截图？")) return;
      runButton(this, async function () {
        await postAction("remove_inspiration_image", { id: current.id, expected_version: current.version }, $("#material-remove-image", editor));
        showEditor();
        await loadRows();
      }, "已移除截图");
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      runButton(save, async function () {
        const fields = Object.fromEntries(page.fields.map(([key]) => [key, form.elements[key].value.trim()]));
        const params = {
          ...fields,
          direction: form.elements.direction.value || null,
          topic: form.elements.topic.value || null,
          prompt_id: form.elements.prompt_id.value.trim() || null,
          ...(editing ? { id: current.id, expected_version: current.version } : {}),
        };
        if (image) Object.assign(params, await readImage(image));
        await postAction(image ? "save_inspiration_image" : editing ? update : create, params, save);
        showEditor();
        offset = 0;
        await loadRows();
      }, editing ? "已保存更改" : "已保存" + page.label);
    });
    if (captureFocus && route === "inspirations") {
      $("#material-text", editor).focus();
      captureFocus = false;
    }
  }

  function renderRows(data, requestGeneration) {
    if (generation !== state.pageGeneration || requestGeneration !== rowsGeneration || !root.isConnected) return;
    const related = new Map(data.items.map(item => [item.id, item]));
    list.innerHTML = data.items.length ? data.items.map(item => materialRow(item, route, preferences)).join("") :
      '<div class="empty-state"><strong>' + (query || chosenDirection ? "没有匹配的" + page.label : page.empty) + "</strong></div>";
    $("#material-pages", root).hidden = !offset && !data.has_more;
    prev.disabled = !offset;
    next.disabled = !data.has_more;
    $$(".material-row", list).forEach(function (row) {
      const item = related.get(row.dataset.materialId);
      $("[data-material-edit]", row).addEventListener("click", function () {
        showEditor(item);
        editor.scrollIntoView({ block: "nearest" });
        $("input, textarea, select", editor).focus();
      });
      $("[data-material-remove]", row).addEventListener("click", function () {
        if (!window.confirm("删除这条" + page.label + "？")) return;
        const button = this;
        runButton(button, async function () {
          await postAction(remove, { id: item.id, expected_version: item.version }, button);
          if (data.items.length === 1 && offset) offset -= MATERIAL_PAGE_SIZE;
          showEditor();
          await loadRows();
        }, "已删除" + page.label);
      });
      $("[data-material-image]", row)?.addEventListener("click", function () {
        const button = this;
        const figure = $(".material-image", row);
        runButton(button, async function () {
          const img = $("img", figure);
          if (!img.getAttribute("src")) {
            const payload = await postAction("get_inspiration_image", { id: item.id });
            if (!IMAGE_TYPES.includes(payload.image_type)) throw new Error("Invalid image.");
            img.src = "data:" + payload.image_type + ";base64," + payload.image_data;
          }
          figure.hidden = !figure.hidden;
          button.textContent = figure.hidden ? "查看截图" : "收起截图";
        });
      });
      $("[data-material-draft]", row)?.addEventListener("click", function () {
        const button = this;
        runButton(button, async function () {
          const focus = (item.title || item.text || item.source || item.url).slice(0, 160);
          const direction = item.direction || (route === "clippings" ? "social_topics" : "self_exploration");
          const params = { direction, focus };
          if (item.topic_id) params.topic = item.topic_id;
          const result = await postAction("draft_prompt", params);
          if (generation !== state.pageGeneration) return;
          state.currentPrompt = result.prompt;
          state.angles = [];
          state.draftFocus = focus;
          state.promptFilters = { direction, topic: item.topic_id || "", depth: "" };
          location.hash = "prompts";
        });
      });
    });
  }

  async function loadRows() {
    const requestGeneration = ++rowsGeneration;
    const params = { limit: MATERIAL_PAGE_SIZE, offset };
    if (query) params.query = query;
    if (chosenDirection) params.direction = chosenDirection;
    let data;
    try {
      data = await postAction(search, params);
    } catch (error) {
      if (generation !== state.pageGeneration || requestGeneration !== rowsGeneration || !root.isConnected) return;
      throw error;
    }
    if (generation !== state.pageGeneration || requestGeneration !== rowsGeneration || !root.isConnected) return;
    renderRows(data, requestGeneration);
  }

  searchForm.addEventListener("submit", function (event) {
    event.preventDefault();
    query = searchForm.elements.query.value.trim();
    chosenDirection = searchForm.elements.direction.value;
    offset = 0;
    runButton($("button", searchForm), loadRows);
  });
  prev.addEventListener("click", function () {
    offset = Math.max(0, offset - MATERIAL_PAGE_SIZE);
    runButton(this, loadRows);
  });
  next.addEventListener("click", function () {
    offset += MATERIAL_PAGE_SIZE;
    runButton(this, loadRows);
  });

  showEditor(link ? { direction: link.direction, topic_id: link.topic_id, prompt_id: link.id, title: link.text.slice(0, 160) } : null);
  renderRows(initialData, initialRowsGeneration);
}

