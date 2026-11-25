"use strict";

const $ = (selector, root) => (root || document).querySelector(selector);
const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));
const pageContent = $("#page-content");
const notice = $("#notice");
const pageTitle = $("#page-title");
const pageDescription = $("#page-description");

const DIRECTIONS = [
  { id: "self_exploration", name: "自我探索" },
  { id: "consultation_reflection", name: "咨询反思" },
  { id: "social_topics", name: "社会话题" }
];
const DEPTHS = [
  { id: "light", name: "轻" },
  { id: "medium", name: "中" },
  { id: "deep", name: "深" }
];
const STATES = [
  { id: "want_to_write", name: "想写" },
  { id: "in_progress", name: "写到一半" },
  { id: "written", name: "写过" }
];
const FEEDBACK = [
  { id: "helpful", name: "有帮助" },
  { id: "not_helpful", name: "没帮助" },
  { id: "too_broad", name: "太宽" },
  { id: "too_narrow", name: "太窄" },
  { id: "not_relevant", name: "不相关" },
  { id: "too_heavy", name: "太重" },
  { id: "too_academic", name: "太学术" }
];
const ROUTES = {
  prompts: { title: "命题", description: "按需取题，按你的判断记录写作状态" },
  records: { title: "记录", description: "查阅命题，更新写作状态或暂缓期限" },
  preferences: { title: "偏好", description: "分别调整长期方向和主题权重" },
  ...MATERIAL_ROUTES
};
const STATUS_DEFAULT_KEY = "shuxu.status.default";

const state = {
  sessionToken: null,
  route: "prompts",
  pageGeneration: 0,
  recordLoadGeneration: 0,
  preferences: null,
  currentPrompt: null,
  draftFocus: "",
  materialLink: null,
  angles: [],
  promptFilters: { direction: "", topic: "", depth: "" },
  recordFilters: { direction: "", topic: "", state: "", query: "", offset: 0 },
  records: [],
  recordTotal: 0,
  statusLoaded: false
};

class ApiError extends Error {
  constructor(status, payload) {
    const issue = payload && payload.error ? payload.error : {};
    super(issue.reason || issue.message || issue.code || "请求未完成");
    this.name = "ApiError";
    this.status = status || (payload && payload.status) || "error";
    this.code = issue.code || "";
    this.details = issue.details || null;
  }
}

function escapeHTML(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char];
  });
}

function labelFor(items, value, fallback) {
  const found = items.find(function (item) { return item.id === value; });
  return found ? found.name : (fallback || "—");
}

function directionName(id) { return labelFor(DIRECTIONS, id, id || "未分类"); }
function depthName(id) { return labelFor(DEPTHS, id, "未标深浅"); }
function stateName(id) { return labelFor(STATES, id, "未标记"); }

function formatDate(value, withTime) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", withTime
    ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function formatShortTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function toLocalDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function fromLocalDateTime(value) {
  return value ? new Date(value).toISOString() : "";
}

function requestId(button) {
  if (!button.dataset.requestId) button.dataset.requestId = crypto.randomUUID();
  return button.dataset.requestId;
}

function resetRetryOnChange(form, button) {
  ["input", "change"].forEach(function (eventName) {
    form.addEventListener(eventName, function () { delete button.dataset.requestId; });
  });
}

function showNotice(message, kind) {
  notice.textContent = message;
  notice.dataset.kind = kind || "info";
  notice.hidden = !message;
}

function clearNotice() {
  notice.textContent = "";
  notice.hidden = true;
  delete notice.dataset.kind;
}

function userError(error) {
  if (error instanceof ApiError) return error.message;
  return "暂时无法连接本机服务";
}

async function requestJSON(path, options) {
  const response = await fetch(path, Object.assign({ credentials: "same-origin" }, options || {}));
  const payload = await response.json();
  if (!response.ok || payload.status !== "ok") throw new ApiError(payload.status, payload);
  return payload.data;
}

async function establishSession() {
  const data = await requestJSON("/api/session", { method: "GET" });
  state.sessionToken = data.token;
}

async function postAction(action, params, button) {
  const body = Object.assign({}, params || {});
  if (button) body.request_id = requestId(button);
  const data = await requestJSON("/api/" + encodeURIComponent(action), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Book-Session": state.sessionToken
    },
    body: JSON.stringify(body)
  });
  if (button) delete button.dataset.requestId;
  return data;
}

async function getPreferences(force, generation = state.pageGeneration) {
  if (!state.preferences || force) {
    const preferences = await postAction("get_preferences", {});
    if (generation === state.pageGeneration) state.preferences = preferences;
    return preferences;
  }
  return state.preferences;
}

function directionOptions(selected, includeAll) {
  let html = includeAll ? '<option value="">全部方向</option>' : "";
  DIRECTIONS.forEach(function (direction) {
    html += '<option value="' + escapeHTML(direction.id) + '"' + (direction.id === selected ? " selected" : "") + ">" + escapeHTML(direction.name) + "</option>";
  });
  return html;
}

function topicOptions(preferences, direction, selected, includeAll) {
  let html = includeAll ? '<option value="">全部主题</option>' : '<option value="">不指定主题</option>';
  const topics = preferences.topics.filter(function (topic) {
    return topic.active && (!direction || topic.direction === direction);
  });
  topics.forEach(function (topic) {
    const indent = topic.parent_id ? "　· " : "";
    html += '<option value="' + escapeHTML(topic.id) + '"' + (String(topic.id) === String(selected || "") ? " selected" : "") + ">" +
      escapeHTML(indent + topic.name) + "</option>";
  });
  return html;
}

function stateOptions(selected, includeAll) {
  let html = includeAll ? '<option value="">全部状态</option>' : '<option value="">未标记</option>';
  STATES.forEach(function (item) {
    html += '<option value="' + escapeHTML(item.id) + '"' + (item.id === selected ? " selected" : "") + ">" + escapeHTML(item.name) + "</option>";
  });
  return html;
}

function editableStateOptions(selected) {
  let html = '<option value=""' + (selected ? "" : " selected") + '>未记录</option>';
  STATES.forEach(function (item) {
    html += '<option value="' + escapeHTML(item.id) + '"' + (item.id === selected ? " selected" : "") + ">" + escapeHTML(item.name) + "</option>";
  });
  return html;
}

function depthOptions(selected, includeAll) {
  let html = includeAll ? '<option value="">不限深浅</option>' : '<option value="">未标深浅</option>';
  DEPTHS.forEach(function (item) {
    html += '<option value="' + escapeHTML(item.id) + '"' + (item.id === selected ? " selected" : "") + ">" + escapeHTML(item.name) + "</option>";
  });
  return html;
}

function tag(text, tone) {
  return '<span class="tag' + (tone ? " " + tone : "") + '">' + escapeHTML(text) + "</span>";
}

function promptTopicName(prompt, preferences) {
  const topic = preferences.topics.find(function (item) { return String(item.id) === String(prompt.topic_id); });
  return topic ? topic.name : (prompt.topic_id ? String(prompt.topic_id) : "未指定主题");
}

function setRoute(route) {
  state.route = Object.prototype.hasOwnProperty.call(ROUTES, route) ? route : "prompts";
  $$(".navigation-link").forEach(function (link) {
    if (link.dataset.route === state.route) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  pageTitle.textContent = ROUTES[state.route].title;
  pageDescription.textContent = ROUTES[state.route].description;
}

async function runButton(button, operation, successMessage) {
  if (button.disabled) return;
  const generation = state.pageGeneration;
  button.disabled = true;
  clearNotice();
  try {
    await operation();
    if (generation === state.pageGeneration && successMessage) showNotice(successMessage, "success");
  } catch (error) {
    if (generation !== state.pageGeneration) return;
    showNotice(userError(error), "error");
    if (error.status === "version_conflict" && state.route === "records") {
      try { await loadRecords(generation); } catch (refreshError) { if (generation === state.pageGeneration) showNotice(userError(refreshError), "error"); }
    }
    if (error.status === "version_conflict" && state.route === "preferences") {
      state.preferences = null;
      try { await renderPreferences(generation); } catch (refreshError) { if (generation === state.pageGeneration) showNotice(userError(refreshError), "error"); }
    }
  } finally {
    button.disabled = false;
  }
}

function promptFiltersFromForm(form) {
  state.promptFilters = {
    direction: form.elements.direction.value,
    topic: form.elements.topic.value,
    depth: form.elements.depth.value
  };
  return state.promptFilters;
}

function promptCardHTML(prompt, preferences) {
  if (!prompt) {
    return '<div class="empty-state"><strong>还没有取出命题</strong><p>选择范围后取一题，或直接开始记录自己的命题</p></div>';
  }
  return '<article class="prompt-card has-prompt">' +
    '<div class="prompt-card-meta">' +
      tag(directionName(prompt.direction)) +
      tag(promptTopicName(prompt, preferences)) +
      tag(depthName(prompt.depth)) +
    '</div>' +
    '<p class="prompt-card-text">' + escapeHTML(prompt.text) + '</p>' +
    (prompt.draft ? '<div class="inline-note">草题 · 尚未保存</div>' : '<div class="inline-note">命题编号 ' + escapeHTML(prompt.id) + '</div>') +
    '<div class="prompt-actions">' +
      (prompt.draft ? '<button class="button primary" type="button" data-prompt-action="save-draft">保存草题</button>' : '<button class="button" type="button" data-prompt-action="narrow">收窄这道题</button>') +
      '<button class="button" type="button" data-prompt-action="angles">查看切入点</button>' +
      (prompt.draft ? "" : '<button class="button text-button" type="button" data-prompt-action="resume">留一张续接卡</button>') +
      '<button class="button text-button" type="button" data-prompt-action="pick-again">换一题</button>' +
    '</div>' +
  '</article>';
}

function renderPromptPage(preferences, generation) {
  if (generation !== state.pageGeneration) return;
  const filters = state.promptFilters;
  const prompt = state.currentPrompt;
  const angles = state.angles.slice(0, 2);
  pageContent.innerHTML =
    '<section class="columns page-section">' +
      '<div class="stack">' +
        '<section class="panel">' +
          '<div class="panel-heading"><div><h2>取一题</h2><p>方向、主题和深浅按当前请求优先</p></div></div>' +
          '<form id="pick-form" class="manual-form">' +
            '<div class="field-grid">' +
              '<div class="field"><label for="pick-direction">方向</label><select id="pick-direction" name="direction">' + directionOptions(filters.direction, true) + '</select></div>' +
              '<div class="field"><label for="pick-topic">主题</label><select id="pick-topic" name="topic">' + topicOptions(preferences, filters.direction, filters.topic, true) + '</select></div>' +
              '<div class="field"><label for="pick-depth">深浅</label><select id="pick-depth" name="depth">' + depthOptions(filters.depth, true) + '</select></div>' +
            '</div>' +
            '<div class="button-row"><button class="button primary" type="submit">' + (prompt ? "换一题" : "取一题") + '</button></div>' +
          '</form>' +
          '<form id="draft-form" class="draft-form"><div class="field"><label for="draft-focus">用线索拟题</label><input id="draft-focus" name="focus" maxlength="160" value="' + escapeHTML(state.draftFocus) + '" placeholder="输入一个经历、材料或想讨论的现象"></div><button class="button" type="submit">拟一题</button></form>' +
        '</section>' +
        '<section class="panel">' +
          '<div class="panel-heading"><div><h2>当前命题</h2><p>命题本身不会改变写作状态</p></div></div>' +
          promptCardHTML(prompt, preferences) +
        '</section>' +
        (angles.length ? '<section class="panel"><div class="panel-heading"><div><h2>切入点</h2><p>按需展开的一到两个具体方向</p></div></div><ol class="angles-list">' +
          angles.map(function (angle) { return "<li>" + escapeHTML(angle) + "</li>"; }).join("") + '</ol></section>' : "") +
      '</div>' +
      '<aside class="stack">' +
        '<section class="panel">' +
          '<div class="panel-heading"><div><h2>手工补记</h2><p>保存一条命题和可选状态</p></div></div>' +
          '<form id="manual-form" class="manual-form">' +
            '<div class="field"><label for="manual-text">命题</label><textarea id="manual-text" name="text" required maxlength="1000" placeholder="写下要保存的命题"></textarea></div>' +
            '<div class="field-grid">' +
              '<div class="field"><label for="manual-direction">方向</label><select id="manual-direction" name="direction">' + directionOptions("self_exploration", false) + '</select></div>' +
              '<div class="field"><label for="manual-topic">主题</label><select id="manual-topic" name="topic">' + topicOptions(preferences, "self_exploration", "", false) + '</select></div>' +
              '<div class="field"><label for="manual-depth">深浅</label><select id="manual-depth" name="depth">' + depthOptions("", false) + '</select></div>' +
            '</div>' +
            '<div class="field-grid">' +
              '<div class="field"><label for="manual-state">写作状态</label><select id="manual-state" name="state">' + stateOptions("", false) + '</select></div>' +
              '<div class="field"><label for="manual-snooze">暂缓到</label><input id="manual-snooze" name="snoozed_until" type="datetime-local"></div>' +
            '</div>' +
            '<div class="button-row"><button class="button primary" type="submit">保存命题</button></div>' +
          '</form>' +
        '</section>' +
        '<section class="panel">' +
          '<div class="panel-heading"><div><h2>反馈</h2><p>作用于所选范围内近 7 天的选题顺序</p></div></div>' +
          '<form id="feedback-form" class="feedback-form">' +
            '<div class="field"><label for="feedback-scope">作用范围</label><select id="feedback-scope" name="scope">' +
              '<option value="prompt">当前命题</option><option value="direction">当前方向</option>' +
              '<option value="topic"' + (prompt && prompt.topic_id ? "" : " disabled") + '>当前主题</option>' +
            '</select></div>' +
            '<div class="field"><label for="feedback-value">反馈</label><select id="feedback-value" name="feedback">' +
              FEEDBACK.map(function (item) { return '<option value="' + item.id + '">' + item.name + '</option>'; }).join("") +
            '</select></div>' +
            '<div class="feedback-submit"><button class="button" type="submit"' + (prompt && !prompt.draft ? "" : " disabled") + '>保存反馈</button><button id="feedback-history" class="button text-button" type="button"' + (prompt && !prompt.draft ? "" : " disabled") + '>查看反馈</button></div>' +
          '</form>' +
          '<ul id="feedback-items" class="feedback-items" hidden></ul>' +
        '</section>' +
      '</aside>' +
    '</section>';

  const pickForm = $("#pick-form");
  const pickButton = $('button[type="submit"]', pickForm);
  pickForm.addEventListener("change", function (event) {
    const filtersNow = promptFiltersFromForm(pickForm);
    if (event.target.name === "direction") $("#pick-topic").innerHTML = topicOptions(preferences, filtersNow.direction, "", true);
  });
  pickForm.addEventListener("submit", function (event) {
    event.preventDefault();
    const filtersNow = promptFiltersFromForm(pickForm);
    const params = {};
    if (filtersNow.direction) params.direction = filtersNow.direction;
    if (filtersNow.topic) params.topic = filtersNow.topic;
    if (filtersNow.depth) params.depth = filtersNow.depth;
    if (state.currentPrompt?.id) params.current_id = state.currentPrompt.id;
    runButton(pickButton, async function () {
      const data = await postAction("pick_prompt", params);
      state.currentPrompt = data.prompt;
      state.angles = [];
      renderPromptPage(await getPreferences(false, generation), generation);
    });
  });

  const draftForm = $("#draft-form");
  draftForm.addEventListener("submit", function (event) {
    event.preventDefault();
    const filters = promptFiltersFromForm(pickForm);
    const focus = draftForm.elements.focus.value.trim();
    const topic = preferences.topics.find(item => item.id === filters.topic);
    const params = { direction: filters.direction || topic?.direction || "self_exploration" };
    if (filters.topic) params.topic = filters.topic;
    if (filters.depth) params.depth = filters.depth;
    if (focus) params.focus = focus;
    runButton($("button", draftForm), async function () {
      const data = await postAction("draft_prompt", params);
      state.draftFocus = focus;
      state.currentPrompt = data.prompt;
      state.angles = [];
      renderPromptPage(preferences, generation);
    });
  });

  $$(".prompt-actions [data-prompt-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      const current = state.currentPrompt;
      if (!current) return;
      const action = button.dataset.promptAction;
      if (action === "pick-again") {
        pickForm.requestSubmit();
        return;
      }
      if (action === "angles") {
        runButton(button, async function () {
          const angleParams = current.id ? { prompt_id: current.id } : { direction: current.direction };
          if (!current.id && current.topic_id) angleParams.topic = current.topic_id;
          const data = await postAction("get_angles", angleParams);
          state.angles = data.angles.slice(0, 2);
          renderPromptPage(await getPreferences(false, generation), generation);
        });
        return;
      }
      if (action === "save-draft") {
        runButton(button, async function () {
          const params = { text: current.text, direction: current.direction, depth: current.depth };
          if (current.topic_id) params.topic = current.topic_id;
          if (current.parent_id) params.parent_id = current.parent_id;
          const receipt = await postAction("save_prompt", params, button);
          const data = await postAction("get_prompt", { prompt_id: receipt.id });
          state.currentPrompt = data.prompt;
          renderPromptPage(preferences, generation);
        }, "已保存草题");
        return;
      }
      if (action === "resume") {
        state.materialLink = { route: "resume", prompt: current };
        location.hash = "resume";
        return;
      }
      if (action === "narrow") {
        runButton(button, async function () {
          const data = await postAction("narrow_prompt", { prompt_id: current.id });
          state.currentPrompt = data.prompt;
          state.angles = [];
          renderPromptPage(await getPreferences(false, generation), generation);
        }, "已收窄命题");
      }
    });
  });

  const manualForm = $("#manual-form");
  const manualButton = $('button[type="submit"]', manualForm);
  resetRetryOnChange(manualForm, manualButton);
  $("#manual-direction").addEventListener("change", function () {
    $("#manual-topic").innerHTML = topicOptions(preferences, this.value, "", false);
  });
  manualForm.addEventListener("submit", function (event) {
    event.preventDefault();
    const form = manualForm.elements;
    const params = { text: form.text.value.trim(), direction: form.direction.value };
    if (form.topic.value) params.topic = form.topic.value;
    if (form.depth.value) params.depth = form.depth.value;
    if (form.state.value) params.state = form.state.value;
    if (form.snoozed_until.value) params.snoozed_until = fromLocalDateTime(form.snoozed_until.value);
    runButton(manualButton, async function () {
      const receipt = await postAction("save_prompt", params, manualButton);
      if (generation !== state.pageGeneration) return;
      manualForm.reset();
      showNotice("已保存命题，记录版本 " + receipt.version, "success");
    });
  });

  const feedbackForm = $("#feedback-form");
  const feedbackButton = $('button[type="submit"]', feedbackForm);
  resetRetryOnChange(feedbackForm, feedbackButton);
  function feedbackTarget() {
    const current = state.currentPrompt;
    const scope = feedbackForm.elements.scope.value;
    return scope === "prompt" ? { prompt_id: current.id } : scope === "direction" ? { direction: current.direction } : { topic: current.topic_id };
  }
  $("#feedback-history").addEventListener("click", function () {
    runButton(this, async function () {
      const data = await postAction("list_feedback", { ...feedbackTarget(), limit: 5 });
      if (generation !== state.pageGeneration) return;
      const list = $("#feedback-items");
      list.innerHTML = data.items.length ? data.items.map(item => '<li>' + escapeHTML(labelFor(FEEDBACK, item.feedback, item.feedback)) + ' · ' + escapeHTML(formatDate(item.created_at, true)) + '</li>').join("") : '<li>这项范围暂无反馈</li>';
      list.hidden = false;
    });
  });
  feedbackForm.addEventListener("submit", function (event) {
    event.preventDefault();
    const current = state.currentPrompt;
    if (!current) return;
    const scope = feedbackForm.elements.scope.value;
    const params = { scope, feedback: feedbackForm.elements.feedback.value, ...feedbackTarget() };
    runButton(feedbackButton, async function () {
      await postAction("record_feedback", params, feedbackButton);
    }, "已记录反馈");
  });
}

function recordStateTone(record) {
  if (record.is_snoozed) return "amber";
  if (record.state === "written") return "green";
  if (record.state === "in_progress") return "blue";
  return "";
}

function recordHTML(record, index, preferences) {
  const statusText = record.is_snoozed ? "暂缓到 " + formatDate(record.snoozed_until, true) : stateName(record.state);
  return '<article class="record" data-index="' + index + '">' +
    '<div class="record-main">' +
      '<p class="record-text">' + escapeHTML(record.text) + '</p>' +
      '<div class="record-meta">' +
        tag(directionName(record.direction)) +
        tag(promptTopicName(record, preferences)) +
        tag(depthName(record.depth)) +
        tag(statusText, recordStateTone(record)) +
        '<span class="date">更新于 ' + escapeHTML(formatDate(record.updated_at || record.created_at, true)) + '</span>' +
      '</div>' +
    '</div>' +
    '<div class="record-controls">' +
      '<div class="field"><label for="record-state-' + index + '">状态</label><select id="record-state-' + index + '" data-record-state>' + editableStateOptions(record.state || "") + '</select></div>' +
      '<div class="button-row"><button class="button" type="button" data-save-state>更新状态</button>' +
        (record.snoozed_until ? '<button class="button text-button" type="button" data-clear-snooze>取消暂缓</button>' : "") +
      '</div>' +
      '<details class="snooze-details"><summary>设置暂缓</summary>' +
        '<form class="snooze-form"><div class="field"><label for="snooze-' + index + '">暂缓到</label><input id="snooze-' + index + '" type="datetime-local" name="snoozed_until" required value="' + escapeHTML(toLocalDateTime(record.snoozed_until)) + '"></div>' +
        '<button class="button" type="submit">保存暂缓</button></form>' +
      '</details>' +
    '</div>' +
  '</article>';
}

async function loadRecords(generation, renderList = true, preferences) {
  const requestGeneration = ++state.recordLoadGeneration;
  const filters = state.recordFilters;
  const params = { include_private: true, limit: 50, offset: filters.offset };
  if (filters.direction) params.direction = filters.direction;
  if (filters.topic) params.topic = filters.topic;
  if (filters.state) params.state = filters.state;
  let data;
  try {
    data = await postAction("list_prompts", params);
  } catch (error) {
    if (generation !== state.pageGeneration || requestGeneration !== state.recordLoadGeneration) return;
    throw error;
  }
  if (generation !== state.pageGeneration || requestGeneration !== state.recordLoadGeneration) return;
  try {
    preferences = preferences || await getPreferences(false, generation);
  } catch (error) {
    if (generation !== state.pageGeneration || requestGeneration !== state.recordLoadGeneration) return;
    throw error;
  }
  if (generation !== state.pageGeneration || requestGeneration !== state.recordLoadGeneration) return;
  state.records = data.prompts;
  state.recordTotal = data.total;
  if (renderList) renderRecordRows(preferences, generation);
  return true;
}

function renderRecordRows(preferences, generation) {
  if (generation !== state.pageGeneration) return;
  const query = state.recordFilters.query.trim().toLocaleLowerCase();
  const filtered = state.records.map(function (record, index) {
    return { record: record, index: index };
  }).filter(function (row) {
    return !query || row.record.text.toLocaleLowerCase().includes(query);
  });
  const first = state.recordTotal ? state.recordFilters.offset + 1 : 0;
  const last = Math.min(state.recordFilters.offset + state.records.length, state.recordTotal);
  $("#record-summary").textContent = "显示 " + first + "–" + last + " 条，共 " + state.recordTotal + " 条";
  $("#record-list").innerHTML = filtered.length
    ? filtered.map(function (row) { return recordHTML(row.record, row.index, preferences); }).join("")
    : '<div class="empty-state"><strong>这个范围里没有命题</strong><p>调整方向、主题、状态或搜索词后再查看</p></div>';
  $("#records-prev").disabled = state.recordFilters.offset <= 0;
  $("#records-next").disabled = state.recordFilters.offset + state.records.length >= state.recordTotal;
  $("#records-page").textContent = query ? "搜索当前页" : "每页 50 条";
  bindRecordActions(generation);
}

function bindRecordActions(generation) {
  $$(".record").forEach(function (row) {
    const record = state.records[Number(row.dataset.index)];
    const stateButton = $("[data-save-state]", row);
    $("[data-record-state]", row).addEventListener("change", function () { delete stateButton.dataset.requestId; });
    stateButton.addEventListener("click", function () {
      const value = $("[data-record-state]", row).value;
      runButton(stateButton, async function () {
        await postAction("set_prompt_state", {
          prompt_id: record.id,
          state: value || null,
          expected_version: record.version
        }, stateButton);
        await loadRecords(generation);
      }, "已更新写作状态");
    });

    const snoozeForm = $(".snooze-form", row);
    const snoozeButton = $('button[type="submit"]', snoozeForm);
    resetRetryOnChange(snoozeForm, snoozeButton);
    snoozeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      const localValue = snoozeForm.elements.snoozed_until.value;
      runButton(snoozeButton, async function () {
        await postAction("snooze_prompt", {
          prompt_id: record.id,
          snoozed_until: fromLocalDateTime(localValue),
          expected_version: record.version
        }, snoozeButton);
        await loadRecords(generation);
      }, "已更新暂缓期限");
    });

    const clearButton = $("[data-clear-snooze]", row);
    if (clearButton) {
      clearButton.addEventListener("click", function () {
        runButton(clearButton, async function () {
          await postAction("clear_snooze", {
            prompt_id: record.id,
            expected_version: record.version
          }, clearButton);
          await loadRecords(generation);
        }, "已取消暂缓");
      });
    }
  });
}

async function renderRecords(generation) {
  const preferences = await getPreferences(false, generation);
  if (generation !== state.pageGeneration) return;
  if (!await loadRecords(generation, false, preferences) || generation !== state.pageGeneration) return;
  const filters = state.recordFilters;
  pageContent.innerHTML =
    '<section class="page-section">' +
      '<div class="section-heading"><div><h2>命题记录</h2><p>本机网页可以查看共享与私密记录</p></div>' +
        '<button id="records-refresh" class="button" type="button">刷新</button></div>' +
      '<form id="record-filters" class="filter-bar">' +
        '<div class="field"><label for="record-direction">方向</label><select id="record-direction" name="direction">' + directionOptions(filters.direction, true) + '</select></div>' +
        '<div class="field"><label for="record-topic">主题</label><select id="record-topic" name="topic">' + topicOptions(preferences, filters.direction, filters.topic, true) + '</select></div>' +
        '<div class="field"><label for="record-state-filter">写作状态</label><select id="record-state-filter" name="state">' + stateOptions(filters.state, true) + '</select></div>' +
        '<div class="field search-field"><label for="record-query">搜索本页命题</label><input id="record-query" name="query" type="search" value="' + escapeHTML(filters.query) + '" placeholder="在当前 50 条记录中搜索"></div>' +
        '<button class="button primary" type="submit">查找</button>' +
      '</form>' +
      '<p id="record-summary" class="record-summary">读取中</p>' +
      '<div id="record-list" class="record-list"><div class="empty-state">读取命题记录</div></div>' +
      '<div class="pagination"><button id="records-prev" class="button" type="button">上一页</button><p id="records-page">每页 50 条</p><button id="records-next" class="button" type="button">下一页</button></div>' +
    '</section>';

  const form = $("#record-filters");
  renderRecordRows(preferences, generation);
  form.addEventListener("change", function (event) {
    if (event.target.name === "direction") $("#record-topic").innerHTML = topicOptions(preferences, event.target.value, "", true);
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    state.recordFilters = {
      direction: form.elements.direction.value,
      topic: form.elements.topic.value,
      state: form.elements.state.value,
      query: form.elements.query.value,
      offset: 0
    };
    runButton($('button[type="submit"]', form), function () { return loadRecords(generation); });
  });
  $("#record-query").addEventListener("input", function () {
    state.recordFilters.query = this.value;
    renderRecordRows(preferences, generation);
  });
  $("#records-refresh").addEventListener("click", function () {
    state.recordFilters.offset = 0;
    runButton(this, function () { return loadRecords(generation); });
  });
  $("#records-prev").addEventListener("click", function () {
    state.recordFilters.offset = Math.max(0, state.recordFilters.offset - 50);
    runButton(this, function () { return loadRecords(generation); });
  });
  $("#records-next").addEventListener("click", function () {
    state.recordFilters.offset += 50;
    runButton(this, function () { return loadRecords(generation); });
  });
}

function weightText(value) {
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function recentText(weight, expiresAt) {
  if (weight == null) return "无近期调整";
  return "近期 " + weightText(weight) + (expiresAt ? " · 至 " + formatDate(expiresAt, true) : " · 未设期限");
}

function weightEditorHTML(kind, item) {
  const action = kind === "direction" ? "set_direction_weight" : "set_topic_weight";
  const fieldId = kind + "-weight-" + escapeHTML(item.id);
  const wrapper = kind === "direction" ? "weight-row" : "topic-weight-editor";
  const summary = kind === "direction"
    ? '<div class="weight-name"><strong>' + escapeHTML(item.name) + '</strong><span>方向 · ' + escapeHTML(recentText(item.recent_weight, item.expires_at)) + '</span></div>' +
      '<div class="weight-values"><div class="weight-value"><span>长期</span><strong>' + escapeHTML(weightText(item.baseline_weight)) + '</strong></div><div class="weight-value"><span>当前有效</span><strong>' + escapeHTML(weightText(item.effective_weight)) + '</strong></div></div>'
    : "";
  return '<div class="' + wrapper + '">' + summary +
    '<form class="weight-form" data-weight-kind="' + kind + '" data-scope="baseline" data-action="' + action + '" data-item="' + escapeHTML(item.id) + '" data-baseline-weight="' + escapeHTML(item.baseline_weight) + '" data-recent-weight="' + escapeHTML(item.recent_weight == null ? "" : item.recent_weight) + '" data-effective-weight="' + escapeHTML(item.effective_weight) + '" data-expires-at="' + escapeHTML(toLocalDateTime(item.expires_at)) + '">' +
      '<div class="field"><label for="' + fieldId + '">权重</label><input id="' + fieldId + '" name="weight" type="number" min="0" step="0.01" value="' + escapeHTML(item.baseline_weight) + '" required></div>' +
      '<div class="field"><label for="' + fieldId + '-scope">作用范围</label><select id="' + fieldId + '-scope" name="scope"><option value="baseline">长期</option><option value="recent">近期</option></select></div>' +
      '<div class="field expiry"><label for="' + fieldId + '-expiry">到期时间</label><input id="' + fieldId + '-expiry" name="expires_at" type="datetime-local"></div>' +
      '<div class="field weight-reason"><label for="' + fieldId + '-reason">调整理由</label><input id="' + fieldId + '-reason" name="reason" maxlength="240" value="' + escapeHTML(item.baseline_reason || "") + '" data-baseline-reason="' + escapeHTML(item.baseline_reason || "") + '" data-recent-reason="' + escapeHTML(item.recent_reason || "") + '"></div>' +
      '<div class="button-row"><button class="button" type="submit">保存</button>' +
      (kind === "direction" && item.recent_weight != null ? '<button class="button text-button" type="button" data-end-direction>结束近期调整</button>' : "") +
      (kind === "direction" ? '<button class="button text-button" type="button" data-reset-direction>恢复默认</button>' : "") +
      '</div>' +
    '</form>' +
  '</div>';
}

function topicTreeHTML(topics) {
  const sorted = topics.slice().sort(function (a, b) {
    if (a.direction !== b.direction) return DIRECTIONS.findIndex(function (d) { return d.id === a.direction; }) - DIRECTIONS.findIndex(function (d) { return d.id === b.direction; });
    if (a.parent_id && !b.parent_id) return 1;
    if (!a.parent_id && b.parent_id) return -1;
    return a.name.localeCompare(b.name, "zh-CN");
  });
  return sorted.map(function (topic) {
    const direction = directionName(topic.direction);
    const parent = topics.find(function (item) { return String(item.id) === String(topic.parent_id); });
    const parents = topics.filter(function (item) { return item.direction === topic.direction && String(item.id) !== String(topic.id); });
    const parentOptions = '<option value="">一级主题</option>' + parents.map(function (item) {
      return '<option value="' + escapeHTML(item.id) + '"' + (String(item.id) === String(topic.parent_id || "") ? " selected" : "") + '>' + escapeHTML(item.name) + '</option>';
    }).join("");
    return '<article class="topic-row' + (topic.active ? "" : " is-inactive") + '" data-depth="' + (topic.parent_id ? "child" : "root") + '">' +
      '<div class="topic-topline"><div><strong>' + escapeHTML(topic.name) + '</strong><div class="record-meta">' +
        tag(direction) + (parent ? tag("属于 " + parent.name) : "") + (topic.active ? "" : tag("已停用")) +
        tag("长期 " + weightText(topic.baseline_weight)) + tag("当前 " + weightText(topic.effective_weight)) +
        tag(recentText(topic.recent_weight, topic.expires_at)) +
      '</div></div></div>' +
      '<details class="topic-details"><summary>维护主题</summary>' +
        '<form class="topic-edit" data-topic-id="' + escapeHTML(topic.id) + '" data-topic-name="' + escapeHTML(topic.name) + '">' +
          '<div class="field"><label for="topic-name-' + escapeHTML(topic.id) + '">主题名称</label><input id="topic-name-' + escapeHTML(topic.id) + '" name="name" value="' + escapeHTML(topic.name) + '" required></div>' +
          '<div class="field"><label for="topic-parent-' + escapeHTML(topic.id) + '">上级主题</label><select id="topic-parent-' + escapeHTML(topic.id) + '" name="parent_id">' + parentOptions + '</select></div>' +
          '<label class="active-control"><input name="active" type="checkbox"' + (topic.active ? " checked" : "") + '>启用</label>' +
          '<div class="button-row"><button class="button" type="submit">保存主题</button>' +
          (topic.recent_weight != null ? '<button class="button text-button" type="button" data-end-topic>结束近期调整</button>' : "") +
          '<button class="button text-button" type="button" data-reset-topic>恢复权重</button></div>' +
        '</form>' +
        weightEditorHTML("topic", topic) +
      '</details>' +
    '</article>';
  }).join("");
}

function renderPreferencesPage(preferences, generation) {
  if (generation !== state.pageGeneration) return;
  const directions = preferences.directions;
  const topics = preferences.topics;
  async function refreshPreferencesPage() {
    state.preferences = null;
    if (generation !== state.pageGeneration) return;
    const updated = await getPreferences(true, generation);
    if (generation === state.pageGeneration) renderPreferencesPage(updated, generation);
  }
  pageContent.innerHTML =
    '<section class="page-section">' +
      '<div class="section-heading"><div><h2>方向与主题权重</h2><p>长期基准、近期调整及各自的理由</p></div>' +
        '<button id="weights-reset" class="button" type="button">恢复权重默认值</button></div>' +
      '<article class="preference-section">' +
        '<div class="preference-label"><h2>写作方向</h2><p>未点名时使用长期比例；近期调整可以指定期限，也可以手动结束</p></div>' +
        '<div class="preference-panel"><div class="weight-list">' +
          directions.map(function (item) { return weightEditorHTML("direction", item); }).join("") +
        '</div></div>' +
      '</article>' +
      '<article class="preference-section">' +
        '<div class="preference-label"><h2>小主题</h2><p>每个方向内独立加权，可以维护父子主题</p></div>' +
        '<div class="preference-panel"><div id="topic-list" class="topic-list">' + topicTreeHTML(topics) + '</div>' +
          '<hr class="divider">' +
          '<form id="add-topic-form" class="add-topic-form">' +
            '<div class="field"><label for="add-topic-direction">方向</label><select id="add-topic-direction" name="direction">' + directionOptions("self_exploration", false) + '</select></div>' +
            '<div class="field"><label for="add-topic-name">新主题</label><input id="add-topic-name" name="name" required></div>' +
            '<div class="field"><label for="add-topic-parent">上级主题</label><select id="add-topic-parent" name="parent_id">' + topicOptions(preferences, "self_exploration", "", false) + '</select></div>' +
            '<div class="field"><label for="add-topic-weight">权重</label><input id="add-topic-weight" name="weight" type="number" min="0" step="0.01" value="1"></div>' +
            '<button class="button primary" type="submit">添加主题</button>' +
          '</form>' +
        '</div>' +
      '</article>' +
    '</section>';

  $$(".weight-form").forEach(function (form) {
    const saveButton = $('button[type="submit"]', form);
    const scope = $('select[name="scope"]', form);
    scope.addEventListener("change", function () {
      form.dataset.scope = this.value;
      form.elements.weight.value = this.value === "recent" ? (form.dataset.recentWeight || form.dataset.effectiveWeight) : form.dataset.baselineWeight;
      form.elements.expires_at.value = this.value === "recent" ? form.dataset.expiresAt : "";
      const reason = form.elements.reason;
      reason.value = this.value === "recent" ? reason.dataset.recentReason : reason.dataset.baselineReason;
    });
    resetRetryOnChange(form, saveButton);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      const params = {
        weight: Number(form.elements.weight.value),
        scope: form.elements.scope.value,
        expected_version: preferences.version
      };
      if (form.dataset.weightKind === "direction") params.direction = form.dataset.item;
      else params.topic_id = form.dataset.item;
      const expiryValue = form.elements.expires_at.value;
      if (params.scope === "recent" && expiryValue) params.expires_at = fromLocalDateTime(expiryValue);
      if (form.elements.reason.value.trim()) params.reason = form.elements.reason.value.trim();
      runButton(saveButton, async function () {
        await postAction(form.dataset.action, params, saveButton);
        await refreshPreferencesPage();
      }, "已更新" + (form.dataset.weightKind === "direction" ? "方向" : "主题") + "权重");
    });
    const endDirectionButton = $("[data-end-direction]", form);
    if (endDirectionButton) {
      endDirectionButton.addEventListener("click", function () {
        runButton(endDirectionButton, async function () {
          await postAction("end_preference", {
            scope: "direction",
            direction: form.dataset.item,
            expected_version: preferences.version
          }, endDirectionButton);
          await refreshPreferencesPage();
        }, "已结束方向的近期调整");
      });
    }
    const resetDirectionButton = $("[data-reset-direction]", form);
    if (resetDirectionButton) {
      resetDirectionButton.addEventListener("click", function () {
        if (!window.confirm("恢复“" + directionName(form.dataset.item) + "”的默认权重？近期覆盖也会清除。")) return;
        runButton(resetDirectionButton, async function () {
          await postAction("reset_weights", {
            scope: "directions", direction: form.dataset.item, expected_version: preferences.version
          }, resetDirectionButton);
          await refreshPreferencesPage();
        }, "已恢复方向默认权重");
      });
    }
  });

  $$(".topic-edit").forEach(function (form) {
    const saveButton = $('button[type="submit"]', form);
    resetRetryOnChange(form, saveButton);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      const params = {
        topic_id: form.dataset.topicId,
        name: form.elements.name.value.trim(),
        parent_id: form.elements.parent_id.value || null,
        active: form.elements.active.checked,
        expected_version: preferences.version
      };
      runButton(saveButton, async function () {
        await postAction("update_topic", params, saveButton);
        await refreshPreferencesPage();
      }, "已更新小主题");
    });
    const endButton = $("[data-end-topic]", form);
    if (endButton) {
      endButton.addEventListener("click", function () {
        runButton(endButton, async function () {
          await postAction("end_preference", {
            scope: "topic",
            topic_id: form.dataset.topicId,
            expected_version: preferences.version
          }, endButton);
          await refreshPreferencesPage();
        }, "已结束小主题的近期调整");
      });
    }
    const resetTopicButton = $("[data-reset-topic]", form);
    resetTopicButton.addEventListener("click", function () {
      if (!window.confirm("恢复“" + form.dataset.topicName + "”的默认权重？近期覆盖也会清除。")) return;
      runButton(resetTopicButton, async function () {
        await postAction("reset_weights", {
          scope: "topics", topic_id: form.dataset.topicId, expected_version: preferences.version
        }, resetTopicButton);
        await refreshPreferencesPage();
      }, "已恢复小主题默认权重");
    });
  });

  const addForm = $("#add-topic-form");
  const addButton = $('button[type="submit"]', addForm);
  resetRetryOnChange(addForm, addButton);
  $("#add-topic-direction").addEventListener("change", function () {
    $("#add-topic-parent").innerHTML = topicOptions(preferences, this.value, "", false);
  });
  addForm.addEventListener("submit", function (event) {
    event.preventDefault();
    const params = {
      direction: addForm.elements.direction.value,
      name: addForm.elements.name.value.trim(),
      weight: Number(addForm.elements.weight.value),
      expected_version: preferences.version
    };
    if (addForm.elements.parent_id.value) params.parent_id = addForm.elements.parent_id.value;
    runButton(addButton, async function () {
      await postAction("add_topic", params, addButton);
      await refreshPreferencesPage();
    }, "已添加小主题");
  });

  $("#weights-reset").addEventListener("click", function () {
    const button = this;
    if (!window.confirm("恢复方向与小主题的默认权重？现有主题不会删除。")) return;
    runButton(button, async function () {
      await postAction("reset_weights", { scope: "all", expected_version: preferences.version }, button);
      await refreshPreferencesPage();
    }, "已恢复默认权重");
  });
}

async function renderPreferences(generation) {
  const preferences = await getPreferences(false, generation);
  if (generation !== state.pageGeneration) return;
  renderPreferencesPage(preferences, generation);
}

async function renderPage() {
  const generation = ++state.pageGeneration;
  clearNotice();
  const hash = location.hash.slice(1);
  setRoute(hash || "prompts");
  const route = state.route;
  pageContent.inert = true;
  if (!pageContent.innerHTML) pageContent.innerHTML = '<div class="empty-state"><strong>读取书序</strong><p>正在连接本机命题服务</p></div>';
  try {
    if (route === "prompts") {
      const preferences = await getPreferences(false, generation);
      if (generation !== state.pageGeneration) return;
      renderPromptPage(preferences, generation);
    }
    if (route === "records") await renderRecords(generation);
    if (route === "preferences") await renderPreferences(generation);
    if (route in MATERIAL_ROUTES) await renderMaterialPage(route, generation);
    if (generation !== state.pageGeneration) return;
    pageContent.inert = false;
  } catch (error) {
    if (generation !== state.pageGeneration) return;
    showNotice(userError(error), "error");
    pageContent.innerHTML = '<div class="empty-state"><strong>暂时无法读取</strong><p>确认本机服务仍在运行后重试</p><button id="page-retry" class="button" type="button">重试</button></div>';
    $("#page-retry").addEventListener("click", function () {
      runButton(this, async function () {
        await establishSession();
        if (generation !== state.pageGeneration) return;
        state.preferences = null;
        await renderPage();
      });
    });
    pageContent.inert = false;
  }
}

function setStatusPanel(open) {
  $("#status-panel").hidden = !open;
  $("#status-launch").setAttribute("aria-expanded", open ? "true" : "false");
  if (open && !state.statusLoaded) loadRuntimeStatus();
}

async function loadRuntimeStatus() {
  $("#status-error").hidden = true;
  $("#status-service").textContent = "读取中";
  $("#status-database").textContent = "读取中";
  $("#status-mcp").textContent = "读取中";
  $("#status-started").textContent = "读取中";
  $("#status-logs").innerHTML = '<li class="log-empty">读取中</li>';
  try {
    const results = await Promise.all([
      requestJSON("/api/status", { method: "GET" }),
      requestJSON("/api/logs?limit=8", { method: "GET" })
    ]);
    const status = results[0];
    const logs = results[1];
    $("#status-service").textContent = status.service === "running" ? "运行中" : status.service;
    $("#status-database").textContent = status.database === "ready" ? "就绪" : status.database;
    $("#status-mcp").textContent = formatDate(status.mcp_last_request_at, true);
    $("#status-started").textContent = formatDate(status.started_at, true);
    $("#status-updated").textContent = "更新于 " + new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
    $("#status-logs").innerHTML = logs.items.length ? logs.items.map(function (item) {
      return '<li><time datetime="' + escapeHTML(item.at) + '">' + escapeHTML(formatShortTime(item.at)) + '</time><div><span class="log-level">' +
        escapeHTML(item.level) + '</span><p>' + escapeHTML(item.message) + '</p></div></li>';
    }).join("") : '<li class="log-empty">暂无诊断记录</li>';
    $("#status-error").hidden = true;
    $("#status-error").textContent = "";
    state.statusLoaded = true;
    const running = status.service === "running";
    $(".local-dot").classList.toggle("is-running", running);
    $(".local-dot").classList.toggle("is-error", !running);
    $(".status-light").classList.toggle("is-running", running);
    $(".status-light").classList.toggle("is-error", !running);
  } catch (error) {
    $("#status-error").textContent = userError(error);
    $("#status-error").hidden = false;
    $("#status-service").textContent = "暂不可用";
    $("#status-database").textContent = "—";
    $("#status-mcp").textContent = "—";
    $("#status-started").textContent = "—";
    $("#status-logs").innerHTML = '<li class="log-empty">无法读取诊断记录</li>';
    state.statusLoaded = false;
    $(".status-light").classList.add("is-error");
  }
}

function initialiseStatusPanel() {
  const saved = localStorage.getItem(STATUS_DEFAULT_KEY) || "collapsed";
  const defaultSelect = $("#status-default");
  defaultSelect.value = saved;
  $("#status-panel").hidden = saved !== "expanded";
  $("#status-launch").setAttribute("aria-expanded", saved === "expanded" ? "true" : "false");
  if (saved === "expanded") loadRuntimeStatus();
  $("#status-launch").addEventListener("click", function () { setStatusPanel(true); });
  $("#status-collapse").addEventListener("click", function () { setStatusPanel(false); });
  defaultSelect.addEventListener("change", function () { localStorage.setItem(STATUS_DEFAULT_KEY, this.value); });
  $("#status-refresh").addEventListener("click", loadRuntimeStatus);
  $("#status-panel").addEventListener("contextmenu", function (event) {
    event.preventDefault();
    setStatusPanel(false);
  });
}

async function start() {
  try {
    await establishSession();
    initialiseStatusPanel();
    await renderPage();
  } catch (error) {
    showNotice(userError(error), "error");
    pageContent.innerHTML = '<div class="empty-state"><strong>无法连接本机服务</strong><p>服务启动后可重试连接</p><button id="initial-retry" class="button" type="button">重试连接</button></div>';
    $("#initial-retry").addEventListener("click", function () {
      runButton(this, async function () {
        await establishSession();
        state.preferences = null;
        await renderPage();
      });
    });
    initialiseStatusPanel();
  }
}

window.addEventListener("hashchange", renderPage);
start();
