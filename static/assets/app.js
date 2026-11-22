(() => {
  "use strict";

  const page = document.body.dataset.page;
  const state = {
    user: null,
    conversations: [],
    activeId: null,
    messages: [],
    evidenceTurns: [],
    openTurns: {},
    liveTurn: null,
    streaming: false,
    currentAssistant: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function setText(node, value) {
    if (node) node.textContent = value == null ? "" : String(value);
  }

  function showError(message) {
    const banner = $("#errorBanner");
    if (!banner) return;
    banner.textContent = message || "请求失败";
    banner.classList.add("show");
  }

  function hideError() {
    const banner = $("#errorBanner");
    if (!banner) return;
    banner.textContent = "";
    banner.classList.remove("show");
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return new Intl.NumberFormat("zh-CN").format(number);
  }

  function formatMs(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number <= 0) return "—";
    if (number < 1000) return Math.round(number) + " ms";
    return (number / 1000).toFixed(1) + " s";
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function formatDay(value) {
    if (!value) return "—";
    return String(value).slice(5);
  }

  function formatTurnRange(turns) {
    const values = (turns || []).map(Number).filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
    if (!values.length) return "";
    const first = values[0];
    const last = values[values.length - 1];
    return first === last ? String(first) : first + "-" + last;
  }

  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return "req-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function removedCitationCount(message) {
    const verify = message && message.meta && message.meta.verify;
    const count = verify ? Number(verify.removed_citations) : 0;
    return Number.isFinite(count) && count > 0 ? count : 0;
  }

  function statusInfo(status, message) {
    if (removedCitationCount(message) > 0 && (status === "done" || status === "degraded")) {
      return { label: "部分引用无法核实", dot: "warn" };
    }
    if (status === "done") return { label: "已完成", dot: "ok" };
    if (status === "degraded") return { label: "已降级", dot: "warn" };
    if (status === "error") return { label: "失败", dot: "err" };
    if (status === "cancelled") return { label: "已取消", dot: "warn" };
    if (status === "running") return { label: "进行中", dot: "" };
    return { label: status || "—", dot: "" };
  }

  async function api(url, options = {}) {
    const redirect401 = options.redirect401 !== false;
    const fetchOptions = { ...options };
    delete fetchOptions.redirect401;
    let response;
    try {
      response = await fetch(url, { credentials: "same-origin", ...fetchOptions });
    } catch (error) {
      throw new Error("网络连接失败，请确认本地服务已启动。");
    }
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      try { payload = await response.json(); } catch (error) { payload = null; }
    }
    if (!response.ok) {
      if (response.status === 401 && redirect401 && page !== "login") {
        location.href = "/ui/login";
      }
      let message = payload && payload.message;
      if (!message && payload && payload.detail && payload.detail.message) message = payload.detail.message;
      if (!message && payload && Array.isArray(payload.detail) && payload.detail[0]) message = payload.detail[0].msg;
      if (!message) message = "请求失败 (" + response.status + ")";
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function applyUser(user) {
    state.user = user;
    setText($("#userEmail"), user && user.user ? user.user.email : "");
    setText($("#userTenant"), user && user.tenant ? user.tenant.name : "");
  }

  async function logout() {
    try {
      await api("/api/v1/auth/logout", { method: "POST", redirect401: false });
    } catch (error) {
    } finally {
      location.href = "/ui/login";
    }
  }

  async function initLogin() {
    let mode = "login";
    const form = $("#authForm");
    const errorBox = $("#formError");
    const submit = $("#submitButton");
    const tenantField = $("#tenantField");
    const tenantInput = $("#tenantName");
    const switchButton = $("#switchMode");

    function setMode(next) {
      mode = next;
      const registering = mode === "register";
      tenantField.hidden = !registering;
      tenantInput.required = false;
      setText($("#authTitle"), registering ? "创建工作区" : "登录工作区");
      setText($("#authSub"), registering ? "一个邮箱对应一个独立租户。" : "使用已注册的邮箱继续。");
      setText($("#switchCopy"), registering ? "已经有账号？" : "还没有账号？");
      setText(switchButton, registering ? "返回登录" : "注册工作区");
      setText(submit, registering ? "创建工作区" : "登录");
      errorBox.classList.remove("show");
    }

    switchButton.addEventListener("click", () => setMode(mode === "login" ? "register" : "login"));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      errorBox.classList.remove("show");
      const email = $("#email").value.trim();
      const password = $("#password").value;
      if (!email || !password) {
        errorBox.textContent = "请填写邮箱和密码。";
        errorBox.classList.add("show");
        return;
      }
      submit.disabled = true;
      submit.textContent = "请稍候…";
      const body = { email, password };
      if (mode === "register" && tenantInput.value.trim()) body.tenant_name = tenantInput.value.trim();
      try {
        await api("/api/v1/auth/" + mode, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          redirect401: false,
        });
        location.href = "/ui/";
      } catch (error) {
        errorBox.textContent = error.message || "登录失败";
        errorBox.classList.add("show");
        submit.disabled = false;
        setText(submit, mode === "register" ? "创建工作区" : "登录");
      }
    });

    try {
      await api("/api/v1/auth/me", { redirect401: false });
      location.href = "/ui/";
    } catch (error) {
      // A signed-out visitor should see the form.
    }
  }
  function updateTitle(title, subtitle) {
    setText($("#pageTitle"), title || "研究工作台");
    setText($("#pageSub"), subtitle || "用自然语言提问，得到带证据编号的答案。");
  }

  function renderConversationList() {
    const list = $("#conversationList");
    if (!list) return;
    list.replaceChildren();
    if (!state.conversations.length) {
      const empty = document.createElement("div");
      empty.className = "evidence-empty";
      empty.textContent = "还没有会话。提第一个问题即可开始。";
      list.appendChild(empty);
      return;
    }
    for (const item of state.conversations) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "conversation-item" + (item.id === state.activeId ? " active" : "");
      button.dataset.id = item.id;
      const title = document.createElement("span");
      title.className = "conversation-title";
      title.textContent = item.title || "未命名会话";
      const date = document.createElement("span");
      date.className = "conversation-date";
      date.textContent = formatTime(item.updated_at || item.created_at);
      button.append(title, date);
      button.addEventListener("click", () => {
        if (!state.streaming) openConversation(item.id);
      });
      list.appendChild(button);
    }
  }

  async function loadConversations() {
    const data = await api("/api/v1/conversations?limit=50");
    state.conversations = Array.isArray(data.items) ? data.items : [];
    renderConversationList();
  }

  function setBusy(busy) {
    state.streaming = busy;
    const input = $("#questionInput");
    const send = $("#sendButton");
    if (input) input.disabled = busy;
    if (send) {
      send.disabled = busy;
      send.textContent = busy ? "处理中…" : "发送";
    }
  }

  function setAgentStatus(value) {
    setText($("#agentStatus"), value);
  }

  function createMessageRow(message) {
    const row = document.createElement("article");
    row.className = "message " + (message.role === "assistant" ? "assistant" : "user");
    if (message.id) row.dataset.messageId = message.id;
    const messageTurn = message.role === "assistant" ? Number(message.turn) : null;
    if (Number.isInteger(messageTurn) && messageTurn > 0) row.dataset.turn = String(messageTurn);

    const meta = document.createElement("div");
    meta.className = "message-meta";
    const role = document.createElement("span");
    role.className = "role";
    role.textContent = message.role === "assistant" ? "研究助手" : "你";
    meta.appendChild(role);
    const time = document.createElement("span");
    time.textContent = formatTime(message.created_at || new Date().toISOString());
    meta.appendChild(time);

    let statusLabelNode = null;
    let statusDotNode = null;
    if (message.role === "assistant") {
      const status = document.createElement("span");
      status.className = "message-status";
      statusDotNode = document.createElement("span");
      const info = statusInfo(message.status, message);
      statusDotNode.className = "status-dot " + info.dot;
      statusLabelNode = document.createElement("span");
      statusLabelNode.textContent = info.label;
      status.append(statusDotNode, statusLabelNode);
      meta.appendChild(status);
    }

    const body = document.createElement("div");
    body.className = "message-body";
    let textNode = null;
    if (message.role === "assistant") {
      textNode = document.createElement("div");
      if (message.content) {
        textNode.className = "answer-text";
        renderAnswer(textNode, message.content, message.evidence || [], message.meta);
      } else {
        textNode.className = "answer-empty";
        textNode.textContent = message.status === "running" ? "正在取数与组织答案…" : "暂无回答";
      }
      body.appendChild(textNode);
    } else {
      textNode = document.createElement("div");
      textNode.textContent = message.content || "";
      body.appendChild(textNode);
    }

    row.append(meta, body);
    const view = { row, textNode, statusLabelNode, statusDotNode, body };
    renderVerifyLine(view, message);
    return view;
  }

  function renderVerifyLine(view, message) {
    if (!view || !view.body) return;
    view.body.querySelectorAll(".verify-line").forEach((node) => node.remove());
    const meta = (message && message.meta) || {};
    const verify = meta.verify;
    const lines = [];
    if (verify) {
      if (verify.removed_citations > 0) {
        lines.push(
          "部分引用无法核实 · 已移除 " +
            verify.removed_citations +
            " 条无效引用，对应内容缺少证据支撑"
        );
      } else if (verify.mode === "raw") {
        lines.push("原始数据模式 · 未经程序解析");
      } else if (verify.passed === false) {
        lines.push("核对未通过" + (verify.issues && verify.issues.length ? " · " + verify.issues[0] : ""));
      }
    }
    const summaryTurns = Array.isArray(meta.agent_state_summary_turns)
      ? meta.agent_state_summary_turns
      : [];
    if (meta.agent_state_rebuilt) {
      const range = summaryTurns.length
        ? "第 " + formatTurnRange(summaryTurns) + " 轮以摘要载入"
        : "早期轮次以摘要载入";
      lines.push("上下文已重建 · " + range);
    }
    const gapTurns = Array.isArray(meta.agent_state_gap_turns)
      ? meta.agent_state_gap_turns
      : [];
    if (gapTurns.length) {
      lines.push(
        "上下文缺失第 " + gapTurns.join("、") + " 轮 · 相关追问可能无法回溯"
      );
    }
    if (!lines.length) return;
    for (const text of lines) {
      const line = document.createElement("div");
      line.className = "tool-line verify-line";
      line.textContent = text;
      view.body.appendChild(line);
    }
  }

  function appendMessage(message) {
    const messages = $("#messages");
    const view = createMessageRow(message);
    if (messages) messages.appendChild(view.row);
    const empty = $("#emptyState");
    if (empty) empty.hidden = true;
    return view;
  }

  function setRowStatus(view, status, label) {
    if (!view) return;
    const info = statusInfo(status);
    if (view.statusDotNode) view.statusDotNode.className = "status-dot " + info.dot;
    if (view.statusLabelNode) view.statusLabelNode.textContent = label || info.label;
  }

  const ANSWER_SECTIONS = [
    { key: "conclusion", label: "结论", marker: "结论：" },
    { key: "evidence", label: "依据", marker: "依据：" },
    { key: "numbers", label: "关键数据", marker: "关键数据：" },
    { key: "pending", label: "待确认", marker: "待确认：" },
  ];

  const LEGACY_SECTIONS = [
    { key: "conclusion", label: "结论", marker: "结论：" },
    { key: "numbers", label: "关键数字", marker: "关键数字：" },
    { key: "evidence", label: "正文", marker: "正文：" },
    { key: "pending", label: "边界", marker: "边界：" },
  ];

  function collectSections(raw, definitions) {
    const found = [];
    for (const section of definitions) {
      const index = raw.indexOf(section.marker);
      if (index >= 0) found.push({ ...section, index, start: index + section.marker.length });
    }
    if (found.length !== definitions.length) return null;
    found.sort((left, right) => left.index - right.index);
    if (found.map((item) => item.key).join(",") !== definitions.map((item) => item.key).join(",")) {
      return null;
    }
    const sections = {};
    for (let index = 0; index < found.length; index += 1) {
      const end = index + 1 < found.length ? found[index + 1].index : raw.length;
      sections[found[index].key] = raw.slice(found[index].start, end).trim();
    }
    return sections;
  }

  function parseAnswerSections(text) {
    const raw = String(text || "");
    return collectSections(raw, ANSWER_SECTIONS) || collectSections(raw, LEGACY_SECTIONS);
  }

  const SUBHEAD_RE = /^[\u4e00-\u9fa5A-Za-z0-9（）()·“”\- ]{2,24}：$/;
  const LIST_RE = /^\s*(?:\d{1,2}[.、)）](?!\d)|[（(]\d{1,2}[）)]|[一二三四五六七八九十]+、)\s*/;

  function channelInfo(evidence) {
    const key = String((evidence && evidence.channel) || "").toLowerCase();
    const kind = String((evidence && evidence.channel_kind) || "unknown").toLowerCase();
    const label = (evidence && evidence.channel_label) || "\u672a\u77e5\u6765\u6e90";
    const kindLabel = (evidence && (evidence.source_kind_label || evidence.channel_kind_label)) || "";
    return {
      key: key || "unknown",
      cls: kind.replace(/[^a-z0-9_-]/g, "") || "unknown",
      label,
      kindLabel,
    };
  }

  const CITATION_ID_RE = /^E\d+-\d+$/;

  function citationIdOf(evidence) {
    if (!evidence) return "";
    const explicit = String(evidence.citation_id || "");
    if (CITATION_ID_RE.test(explicit)) return explicit;
    const legacy = String(evidence.id || "");
    return CITATION_ID_RE.test(legacy) ? legacy : "";
  }

  function evidenceLocator(evidence) {
    return String((evidence && evidence.id) || (evidence && evidence.citation_id) || "");
  }

  function buildCitationIndex(evidences) {
    const map = new Map();
    for (const evidence of evidences || []) {
      for (const fact of evidence.facts || []) {
        if (fact && fact.fact_id && !map.has(fact.fact_id)) map.set(fact.fact_id, evidence);
      }
      const citationId = citationIdOf(evidence);
      if (citationId && !map.has(citationId)) map.set(citationId, evidence);
    }
    return map;
  }

  function turnForCitation(id) {
    const match = /^E(\d+)-\d+$/.exec(String(id || ""));
    return match ? Number(match[1]) : null;
  }

  function turnOfMessage(message) {
    const value = Number(message && message.turn);
    if (Number.isInteger(value) && value > 0) return value;
    for (const evidence of (message && message.evidence) || []) {
      const turn = turnForCitation(citationIdOf(evidence));
      if (turn) return turn;
    }
    return null;
  }

  function evidenceTurnEntry(turn) {
    if (!turn) return null;
    return state.evidenceTurns.find((item) => item.turn === turn) || null;
  }

  function citationCounts(content) {
    const counts = {};
    const pattern = /[\[（(](E\d+-\d+)[\]）)]/g;
    let match = pattern.exec(String(content || ""));
    while (match) {
      counts[match[1]] = (counts[match[1]] || 0) + 1;
      match = pattern.exec(String(content || ""));
    }
    return counts;
  }

  function dedupeEvidence(evidenceList) {
    const seen = new Set();
    const items = [];
    for (const evidence of evidenceList || []) {
      const key = citationIdOf(evidence) || evidence.id || "";
      if (!key || seen.has(key)) continue;
      seen.add(key);
      items.push(evidence);
    }
    return items;
  }

  function collectEvidenceTurns(messages) {
    const turns = [];
    let question = "";
    for (const message of messages || []) {
      if (!message) continue;
      if (message.role === "user") {
        question = String(message.content || "").replace(/\s+/g, " ").trim();
        continue;
      }
      if (message.role !== "assistant") continue;
      const evidence = dedupeEvidence(message.evidence);
      if (!evidence.length) continue;
      const turn = turnOfMessage(message);
      if (!turn) continue;
      turns.push({
        turn,
        evidence,
        counts: citationCounts(message.content),
        hint: question.slice(0, 28),
        messageId: message.id || "",
      });
    }
    return turns.sort((left, right) => left.turn - right.turn);
  }

  function focusTurnQuestion(turn) {
    const number = Number(turn);
    if (!Number.isInteger(number) || number <= 0) return;
    const assistant = document.querySelector('#messages article.assistant[data-turn="' + number + '"]');
    if (!assistant) return;
    let target = assistant.previousElementSibling;
    while (target && !target.classList.contains("user")) target = target.previousElementSibling;
    if (!target) target = assistant;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function focusCitation(citationId) {
    const turn = turnForCitation(citationId);
    if (!turn) return;
    const group = document.querySelector('#evidenceList .evidence-group[data-turn="' + turn + '"]');
    const card = group && group.querySelector('[data-citation-id="' + citationId + '"]');
    if (!card) return;
    group.open = true;
    state.openTurns[turn] = true;
    requestAnimationFrame(() => {
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      card.classList.remove("fact-flash", "is-focused");
      void card.offsetWidth;
      card.classList.add("fact-flash", "is-focused");
      window.setTimeout(() => card.classList.remove("is-focused"), 1800);
    });
  }

  function questionHint() {
    for (let index = state.messages.length - 1; index >= 0; index -= 1) {
      const message = state.messages[index];
      if (message && message.role === "user") {
        return String(message.content || "").replace(/\s+/g, " ").trim().slice(0, 28);
      }
    }
    return "";
  }

  function registerLiveEvidence(current, evidence) {
    const turn = turnForCitation(citationIdOf(evidence));
    if (!turn) return;
    let entry = evidenceTurnEntry(turn);
    if (!entry) {
      entry = { turn, evidence: [], counts: {}, hint: questionHint(), messageId: "" };
      state.evidenceTurns.push(entry);
      state.evidenceTurns.sort((left, right) => left.turn - right.turn);
    }
    if (current && current.id) entry.messageId = current.id;
    const key = citationIdOf(evidence) || evidence.id || "";
    if (key && !entry.evidence.some((item) => (citationIdOf(item) || item.id || "") === key)) {
      entry.evidence.push(evidence);
    }
    if (current) current.evidence = entry.evidence.slice();
    state.liveTurn = turn;
    renderEvidencePanel();
    if (current && current.view && current.view.row) current.view.row.dataset.turn = String(turn);
  }

  function refreshLiveCounts(current) {
    const entry = evidenceTurnEntry(state.liveTurn);
    if (entry && current) entry.counts = citationCounts(current.content);
    renderEvidencePanel();
  }
  function sourceInfoOf(evidence, fallbackId) {
    const info = channelInfo(evidence);
    if (!evidence) {
      return { key: "missing:" + String(fallbackId || ""), name: info.label, cls: info.cls };
    }
    const name = String(evidence.source_name || info.label || "\u672a\u77e5\u6765\u6e90");
    const key = String(evidence.source_key || ((evidence.channel || "unknown") + ":" + name));
    return { key, name, cls: info.cls };
  }

  function appendCitationPill(node, raw, ids, source) {
    const primary = String((ids && ids[0]) || "");
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "citation-pill " + source.cls;
    pill.dataset.sourceId = primary;
    pill.dataset.raw = raw;
    pill.dataset.sourceKey = source.key;
    const suffix = ids && ids.length > 1 ? "\uff08\u5171 " + ids.length + " \u6761\u5f15\u7528\uff09" : "";
    pill.title = "\u67e5\u770b\u6765\u6e90 " + source.name + suffix + " " + raw;
    pill.textContent = source.name;
    pill.addEventListener("click", () => focusCitation(primary));
    node.appendChild(pill);
  }

  function appendCitedText(node, text, citationIndex) {
    const raw = String(text || "");
    const index = citationIndex || new Map();
    const blockPattern = /[\[（(]E\d+-\d+[\]）)](?:[ \t]*[，,、;；]?[ \t]*[\[（(]E\d+-\d+[\]）)])*/g;
    let last = 0;
    let block = blockPattern.exec(raw);
    while (block) {
      if (block.index > last) node.appendChild(document.createTextNode(raw.slice(last, block.index)));
      const blockText = block[0];
      const tokenPattern = /[\[（(](E\d+-\d+)[\]）)]/g;
      const ids = [];
      const sources = new Map();
      let token = tokenPattern.exec(blockText);
      while (token) {
        const id = token[1];
        if (!ids.includes(id)) ids.push(id);
        const source = sourceInfoOf(index.get(id), id);
        if (!sources.has(source.key)) sources.set(source.key, { source, ids: [], raws: [] });
        const group = sources.get(source.key);
        group.ids.push(id);
        group.raws.push(token[0]);
        token = tokenPattern.exec(blockText);
      }
      for (const group of sources.values()) {
        appendCitationPill(node, group.raws.join(""), group.ids, group.source);
      }
      last = block.index + blockText.length;
      block = blockPattern.exec(raw);
    }
    if (last < raw.length) node.appendChild(document.createTextNode(raw.slice(last)));
  }

  function appendBody(container, text, citationIndex) {
    const normalized = String(text || "").replace(
      /([。；;！？!?])\s*(?=(?:\d{1,2}[.、)）](?!\d)|[（(]\d{1,2}[）)]|[一二三四五六七八九十]+、)\s*)/g,
      "$1\n",
    );
    const lines = normalized.split("\n");
    let paragraph = [];
    const flush = () => {
      const joined = paragraph.join("\n").trim();
      if (!joined) return;
      const node = document.createElement("p");
      node.className = "answer-paragraph";
      appendCitedText(node, joined, citationIndex);
      container.appendChild(node);
      paragraph = [];
    };
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) { flush(); continue; }
      if (LIST_RE.test(trimmed)) {
        flush();
        const item = document.createElement("p");
        item.className = "answer-paragraph answer-list-item";
        appendCitedText(item, trimmed, citationIndex);
        container.appendChild(item);
        continue;
      }
      if (SUBHEAD_RE.test(trimmed)) {
        flush();
        const head = document.createElement("h4");
        head.className = "answer-subhead";
        head.textContent = trimmed.replace(/：$/, "");
        container.appendChild(head);
        continue;
      }
      paragraph.push(trimmed);
    }
    flush();
    if (!container.querySelector(".answer-paragraph, .answer-subhead")) {
      const node = document.createElement("p");
      node.className = "answer-paragraph";
      appendCitedText(node, String(text || "").trim(), citationIndex);
      container.appendChild(node);
    }
  }

  function appendLines(container, text, citationIndex) {
    for (const line of String(text || "").split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const node = document.createElement("p");
      node.className = "answer-paragraph";
      appendCitedText(node, trimmed, citationIndex);
      container.appendChild(node);
    }
  }

  function renderAnswer(textNode, content, evidences, meta) {
    if (!textNode) return;
    const text = String(content || "");
    textNode.classList.remove("answer-empty");
    textNode.classList.add("answer-text");
    textNode.replaceChildren();
    const sections = parseAnswerSections(text);
    const citationIndex = buildCitationIndex(evidences);
    if (!sections) {
      const node = document.createElement("p");
      node.className = "answer-paragraph";
      appendCitedText(node, text, citationIndex);
      textNode.appendChild(node);
      return;
    }
    for (const section of ANSWER_SECTIONS) {
      const block = document.createElement("section");
      block.className = "answer-section answer-" + section.key;
      const label = document.createElement("div");
      label.className = "answer-label";
      label.textContent = section.label;
      block.appendChild(label);
      const value = sections[section.key];
      if (section.key === "conclusion" || section.key === "evidence") appendBody(block, value, citationIndex);
      else appendLines(block, value, citationIndex);
      textNode.appendChild(block);
    }
  }

  function buildEvidenceCard(evidence, citeCount) {
    const card = document.createElement("article");
    card.className = "evidence-card";
    card.dataset.evidenceId = evidence.id || "";
    card.dataset.citationId = citationIdOf(evidence);
    const head = document.createElement("div");
    head.className = "evidence-head";
    const id = document.createElement("span");
    id.className = "evidence-id";
    id.textContent = citationIdOf(evidence) || evidence.id || ("E" + (evidence.seq || ""));
    const tool = document.createElement("span");
    tool.className = "evidence-tool";
    tool.textContent = evidence.tool || evidence.server || "unknown";
    tool.title = tool.textContent;
    const quality = document.createElement("span");
    quality.className = "evidence-quality";
    const q = Number(evidence.parse_quality);
    quality.textContent = Number.isFinite(q) ? Math.round(q * 100) + "%" : "";
    const info = channelInfo(evidence);
    const badge = document.createElement("span");
    badge.className = "channel-badge " + info.cls;
    badge.textContent = info.label;
    head.append(id, badge, tool);
    if (Number(citeCount) > 1) {
      const refs = document.createElement("span");
      refs.className = "evidence-ref-count";
      refs.textContent = "引用 " + citeCount + " 次";
      head.appendChild(refs);
    }
    head.appendChild(quality);

    const meta = document.createElement("div");
    meta.className = "evidence-meta";
    const bits = [];
    if (info.kindLabel) bits.push(info.kindLabel);
    if (evidence.as_of_label) bits.push(evidence.as_of_label);
    else if (evidence.fetched_at) bits.push("取数 " + formatTime(evidence.fetched_at));
    if (evidence.query_text) bits.push(evidence.query_text.slice(0, 48));
    meta.textContent = bits.join(" · ");
    card.append(head, meta);

    const sourceItems = Array.isArray(evidence.source_items) ? evidence.source_items : [];
    if (sourceItems.length) {
      const sourceList = document.createElement("div");
      sourceList.className = "source-items";
      for (const item of sourceItems.slice(0, 6)) {
        const row = document.createElement(item.url ? "a" : "div");
        row.className = "source-item";
        if (item.url) {
          row.href = item.url;
          row.target = "_blank";
          row.rel = "noreferrer noopener";
        }
        const title = document.createElement("span");
        title.className = "source-item-title";
        title.textContent = item.title || item.url || "未命名来源";
        row.appendChild(title);
        const subText = [item.site || "", item.published_at || ""].filter(Boolean).join(" · ");
        if (subText) {
          const sub = document.createElement("span");
          sub.className = "source-item-meta";
          sub.textContent = subText;
          row.appendChild(sub);
        }
        sourceList.appendChild(row);
      }
      card.appendChild(sourceList);
    } else if (evidence.source_title || evidence.source_url) {
      const row = document.createElement(evidence.source_url ? "a" : "div");
      row.className = "source-item";
      if (evidence.source_url) {
        row.href = evidence.source_url;
        row.target = "_blank";
        row.rel = "noreferrer noopener";
      }
      const title = document.createElement("span");
      title.className = "source-item-title";
      title.textContent = evidence.source_title || evidence.source_url;
      row.appendChild(title);
      const subText = [evidence.source_publisher || "", evidence.source_published_at || ""]
        .filter(Boolean)
        .join(" · ");
      if (subText) {
        const sub = document.createElement("span");
        sub.className = "source-item-meta";
        sub.textContent = subText;
        row.appendChild(sub);
      }
      card.appendChild(row);
    }

    const facts = Array.isArray(evidence.facts) ? evidence.facts : [];
    if (facts.length) {
      const factList = document.createElement("div");
      factList.className = "fact-list";
      for (const fact of facts.slice(0, 10)) {
        const factRow = document.createElement("div");
        factRow.className = "fact-row";
        factRow.dataset.factId = fact.fact_id || "";
        const factId = document.createElement("span");
        factId.className = "fact-id";
        factId.textContent = fact.fact_id || "";
        const metric = document.createElement("span");
        metric.className = "fact-metric";
        metric.textContent = [fact.metric || "记录", fact.period].filter(Boolean).join(" · ");
        metric.title = metric.textContent;
        const value = document.createElement("span");
        value.className = "fact-value";
        value.textContent = String(fact.value == null ? "—" : fact.value) + (fact.unit && fact.unit !== "text" ? fact.unit : "");
        factRow.append(factId, metric, value);
        factList.appendChild(factRow);
      }
      const total = Number(evidence.facts_total || facts.length);
      if (total > 10) {
        const more = document.createElement("div");
        more.className = "fact-more";
        more.textContent = "另有 " + (total - 10) + " 条事实未展开";
        factList.appendChild(more);
      }
      card.appendChild(factList);
    } else if (evidence.raw_excerpt) {
      const excerpt = document.createElement("div");
      excerpt.className = "evidence-excerpt";
      excerpt.textContent = "原文片段：" + String(evidence.raw_excerpt).slice(0, 240);
      card.appendChild(excerpt);
    }
    return card;
  }

  function renderEvidencePanel() {
    const list = $("#evidenceList");
    if (!list) return;
    const turns = state.evidenceTurns;
    const total = turns.reduce((sum, item) => sum + item.evidence.length, 0);
    setText($("#evidenceCount"), total ? total + " 条" : "0 条");
    list.replaceChildren();
    if (!turns.length) {
      const empty = document.createElement("div");
      empty.className = "evidence-empty";
      empty.textContent = "提问后，这里会按轮次列出证据。";
      list.appendChild(empty);
      return;
    }
    for (const entry of turns) {
      const group = document.createElement("details");
      group.className = "evidence-group";
      group.dataset.turn = String(entry.turn);
      group.open = Boolean(state.openTurns[entry.turn]);
      const summary = document.createElement("summary");
      summary.className = "evidence-group-summary";
      const title = document.createElement("span");
      title.className = "evidence-group-title";
      title.textContent = "第 " + entry.turn + " 轮";
      const count = document.createElement("span");
      count.className = "evidence-group-count";
      count.textContent = entry.evidence.length + " 条";
      const hint = document.createElement("span");
      hint.className = "evidence-group-hint";
      hint.textContent = entry.hint || "";
      hint.title = entry.hint || "";
      summary.append(title, count, hint);
      const body = document.createElement("div");
      body.className = "evidence-group-body";
      for (const evidence of entry.evidence) {
        body.appendChild(buildEvidenceCard(evidence, entry.counts[citationIdOf(evidence)] || 0));
      }
      summary.title = "定位到左侧第 " + entry.turn + " 轮提问";
      summary.addEventListener("click", () => focusTurnQuestion(entry.turn));
      group.append(summary, body);
      group.addEventListener("toggle", () => {
        if (group.open) state.openTurns[entry.turn] = true;
        else delete state.openTurns[entry.turn];
      });
      list.appendChild(group);
    }
  }
  function renderHistory() {
    const messages = $("#messages");
    if (messages) messages.replaceChildren();
    const empty = $("#emptyState");
    if (empty) empty.hidden = state.messages.length > 0;
    for (const message of state.messages) appendMessage(message);
    state.evidenceTurns = collectEvidenceTurns(state.messages);
    renderEvidencePanel();
    scrollBottom();
  }

  function clearConversationView() {
    if (state.streaming) return;
    state.activeId = null;
    state.messages = [];
    state.evidenceTurns = [];
    state.openTurns = {};
    state.liveTurn = null;
    state.currentAssistant = null;
    const messages = $("#messages");
    if (messages) messages.replaceChildren();
    const empty = $("#emptyState");
    if (empty) empty.hidden = false;
    renderEvidencePanel();
    updateTitle("研究工作台");
    renderConversationList();
    hideError();
    setAgentStatus("准备就绪");
    const input = $("#questionInput");
    if (input) input.focus();
  }

  async function openConversation(id) {
    if (state.streaming || !id) return;
    hideError();
    state.activeId = id;
    renderConversationList();
    try {
      const data = await api("/api/v1/conversations/" + encodeURIComponent(id) + "/messages");
      state.messages = Array.isArray(data.items) ? data.items : [];
      state.openTurns = {};
      const conversation = data.conversation || {};
      updateTitle(conversation.title || "研究工作台", "会话记录 · " + state.messages.length + " 条消息");
      renderHistory();
    } catch (error) {
      showError(error.message);
    }
  }

  function scrollBottom() {
    const panel = $("#conversationPanel");
    if (!panel) return;
    requestAnimationFrame(() => { panel.scrollTop = panel.scrollHeight; });
  }

  async function streamChat(payload, onEvent, options = {}) {
    const method = options.method || "POST";
    const url = options.url || "/api/v1/chat";
    const headers = { Accept: "text/event-stream", ...(options.headers || {}) };
    if (method !== "GET") headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers,
        body: method === "GET" ? undefined : JSON.stringify(payload),
      });
    } catch (error) {
      throw new Error("无法连接问答服务，请确认本地服务已启动。");
    }
    if (!response.ok) {
      if (response.status === 401) location.href = "/ui/login";
      let detail = null;
      try { detail = await response.json(); } catch (error) { detail = null; }
      const message = detail && (detail.message || (detail.detail && detail.detail.message)) || "提问失败 (" + response.status + ")";
      throw new Error(message);
    }
    if (!response.body) throw new Error("当前浏览器不支持流式响应。");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const emit = (block) => {
      let eventName = "message";
      let eventId = null;
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("id:")) {
          const parsed = Number(line.slice(3).trim());
          if (Number.isInteger(parsed)) eventId = parsed;
        } else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) return;
      let data = null;
      try { data = JSON.parse(dataLines.join("\n")); } catch (error) { return; }
      onEvent(eventName, data, eventId);
    };

    while (true) {
      const result = await reader.read();
      if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (block.trim()) emit(block);
        boundary = buffer.indexOf("\n\n");
      }
    }
    buffer += decoder.decode();
    buffer = buffer.replace(/\r\n/g, "\n");
    if (buffer.trim()) emit(buffer);
  }

  function appendDelta(assistant, text) {
    if (!assistant || !assistant.view) return;
    if (!assistant.content) {
      assistant.view.textNode.classList.remove("answer-empty");
      assistant.view.textNode.classList.add("answer-text");
      assistant.view.textNode.textContent = "";
    }
    assistant.content += text;
    assistant.view.textNode.textContent = assistant.content;
    scrollBottom();
  }

  async function sendQuestion(rawQuestion) {
    const question = String(rawQuestion || "").trim().slice(0, 2000);
    if (!question || state.streaming) return;
    hideError();
    setBusy(true);
    state.currentAssistant = null;
    const input = $("#questionInput");
    if (input) input.value = "";
    if (input) input.style.height = "auto";

    const userMessage = { role: "user", content: question, status: "done", created_at: new Date().toISOString() };
    const assistant = {
      role: "assistant",
      content: "",
      status: "running",
      created_at: new Date().toISOString(),
      evidence: [],
      meta: {},
    };
    state.messages.push(userMessage, assistant);
    appendMessage(userMessage);
    const view = appendMessage(assistant);
    state.currentAssistant = { ...assistant, view };
    setAgentStatus("Agent 正在分析并选择工具…");
    scrollBottom();

    let lastEventId = 0;
    const handle = (eventName, data, eventId) => {
      if (Number.isInteger(eventId)) lastEventId = Math.max(lastEventId, eventId);
      const current = state.currentAssistant;
      if (eventName === "meta") {
        if (data.conversation_id && !state.activeId) {
          state.activeId = data.conversation_id;
          renderConversationList();
        }
        if (data.message_id) current.id = data.message_id;
        current.meta = { ...(current.meta || {}), ...data };
        if (Number(data.attempt) > 1) {
          current.content = "";
          current.evidence = [];
          if (state.liveTurn) {
            state.evidenceTurns = state.evidenceTurns.filter((item) => item.turn !== state.liveTurn);
            delete state.openTurns[state.liveTurn];
            state.liveTurn = null;
          }
          renderEvidencePanel();
          if (current.view) {
            current.view.textNode.textContent = "";
            current.view.textNode.classList.add("answer-empty");
            current.view.textNode.classList.remove("answer-text");
          }
          setAgentStatus("正在重试…");
        }
        return;
      }
      if (eventName === "plan") {
        if (data.mode === "agentscope_auto") {
          const allowed = Array.isArray(data.allowed) ? data.allowed.length : 0;
          setAgentStatus("Agent 自主选择工具" + (allowed ? " · 可用 " + allowed + " 个" : ""));
        } else {
          const tools = Array.isArray(data.tools) ? data.tools.map((item) => item.name).join("、") : "";
          setAgentStatus("已规划 · " + (data.intent || "unknown") + (tools ? " · " + tools : ""));
        }
        return;
      }
      if (eventName === "tool_start") {
        setAgentStatus("正在取数 · " + (data.tool || "工具"));
        return;
      }
      if (eventName === "tool_end") {
        const ok = data.status === "ok";
        setAgentStatus((ok ? "已取回" : "取数失败") + " · " + (data.tool || "工具") + " · " + formatMs(data.latency_ms));
        return;
      }
      if (eventName === "evidence") {
        const evidence = data && data.evidence;
        if (evidence) registerLiveEvidence(current, evidence);
        return;
      }
      if (eventName === "delta") {
        if (data && typeof data.text === "string") appendDelta(current, data.text);
        return;
      }
      if (eventName === "verify") {
        current.meta = { ...(current.meta || {}), verify: data };
        const label =
          data.removed_citations > 0
            ? "部分引用无法核实 · 已移除 " + data.removed_citations + " 条"
            : (data.mode === "raw" ? "原始数据已返回" : (data.passed ? "核对通过" : "核对未通过"));
        setRowStatus(view, "running", label);
        return;
      }
      if (eventName === "usage") {
        current.meta = { ...(current.meta || {}), budget: data };
        setAgentStatus("已完成 · " + formatNumber(data.tools_used || 0) + " 个工具 · " + formatMs(data.elapsed_ms));
        return;
      }
      if (eventName === "done") {
        current.status = data.status || "done";
        renderAnswer(view.textNode, current.content, current.evidence, current.meta);
        refreshLiveCounts(current);
        renderVerifyLine(view, current);
        setRowStatus(view, current.status, statusInfo(current.status, current.meta).label);
        setAgentStatus("完成 · " + statusInfo(current.status, current.meta).label);
        return;
      }
      if (eventName === "error") {
        current.status = "error";
        setRowStatus(view, "error");
        showError((data && data.message) || "回答失败");
        setAgentStatus("回答失败");
        return;
      }
      if (eventName === "ping") {
        if (data && data.remaining_ms) setAgentStatus("仍在取数 · 剩余约 " + Math.ceil(data.remaining_ms / 1000) + " 秒");
      }
    };

    const resume = async () => {
      const current = state.currentAssistant;
      if (!current || !current.id) return false;
      await streamChat(null, handle, {
        method: "GET",
        url: "/api/v1/messages/" + encodeURIComponent(current.id) + "/events?after=" + lastEventId,
      });
      return true;
    };

    try {
      await streamChat({
        conversation_id: state.activeId,
        client_request_id: newRequestId(),
        message: question,
      }, handle);
      let attempts = 0;
      while (state.currentAssistant && state.currentAssistant.status === "running" && state.currentAssistant.id && attempts < 20) {
        attempts += 1;
        await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * attempts, 5000)));
        await resume();
      }
      if (state.currentAssistant && state.currentAssistant.status === "running") {
        state.currentAssistant.status = "error";
        setRowStatus(view, "error");
        showError("连接恢复失败，请刷新会话查看结果。");
        setAgentStatus("连接中断");
      }
    } catch (error) {
      let recovered = false;
      if (state.currentAssistant && state.currentAssistant.status === "running" && state.currentAssistant.id) {
        try {
          recovered = await resume();
        } catch (resumeError) {
          recovered = false;
        }
      }
      if (!recovered) {
        state.currentAssistant.status = "error";
        setRowStatus(view, "error");
        showError(error.message || "提问失败");
        setAgentStatus("请求失败");
      }
    } finally {
      setBusy(false);
      try {
        await loadConversations();
        if (state.activeId) await openConversation(state.activeId);
      } catch (error) {
        showError(error.message);
      }
      const box = $("#questionInput");
      if (box) box.focus();
    }
  }

  async function initChat() {
    try {
      const me = await api("/api/v1/auth/me");
      applyUser(me);
    } catch (error) {
      return;
    }

    const logoutButton = $("#logoutButton");
    if (logoutButton) logoutButton.addEventListener("click", logout);
    const newButton = $("#newConversation");
    if (newButton) newButton.addEventListener("click", clearConversationView);

    const form = $("#questionForm");
    const input = $("#questionInput");
    const examples = $("#exampleList");

    if (examples) {
      examples.addEventListener("click", (event) => {
        const button = event.target.closest("[data-question]");
        if (!button || !input || state.streaming) return;
        input.value = button.dataset.question || "";
        input.focus();
        input.dispatchEvent(new Event("input"));
      });
    }
    if (input) {
      input.addEventListener("input", () => {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 160) + "px";
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          if (form) form.requestSubmit();
        }
      });
    }
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (input) sendQuestion(input.value);
      });
    }

    const note = $("#headNote");
    if (note) note.textContent = "EVIDENCE LEDGER · " + new Date().toLocaleDateString("zh-CN");
    try {
      await loadConversations();
      if (state.conversations.length) await openConversation(state.conversations[0].id);
    } catch (error) {
      showError(error.message);
    }
  }
  async function initUsage() {
    try {
      const me = await api("/api/v1/auth/me");
      applyUser(me);
    } catch (error) {
      return;
    }
    const logoutButton = $("#logoutButton");
    if (logoutButton) logoutButton.addEventListener("click", logout);
    try {
      const data = await api("/api/v1/usage?days=7");
      const today = data.today || {};
      setText($("#todayQuestions"), formatNumber(today.questions || 0));
      setText($("#todayTools"), formatNumber(today.tool_calls || 0));
      setText($("#todayTokens"), formatNumber((today.prompt_tokens || 0) + (today.completion_tokens || 0)));
      setText($("#todayModels"), formatNumber(today.llm_calls || 0));
      setText($("#modelName"), data.model || "—");
      setText($("#usageSub"), "最近 7 天 · " + (data.model || "当前模型"));

      const rows = $("#usageRows");
      if (rows) {
        rows.replaceChildren();
        const items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
          const tr = document.createElement("tr");
          const td = document.createElement("td");
          td.colSpan = 8;
          td.className = "empty-row";
          td.textContent = "暂无用量记录";
          tr.appendChild(td);
          rows.appendChild(tr);
        } else {
          for (const item of items) {
            const tr = document.createElement("tr");
            const cells = [
              item.day || "—",
              formatNumber(item.questions || 0),
              formatNumber(item.tool_calls || 0),
              formatNumber(item.llm_calls || 0),
              formatNumber(item.failed || 0),
              formatNumber(item.prompt_tokens || 0),
              formatNumber(item.completion_tokens || 0),
              formatMs(item.total_latency_ms || 0),
            ];
            for (const cell of cells) {
              const td = document.createElement("td");
              td.textContent = cell;
              tr.appendChild(td);
            }
            rows.appendChild(tr);
          }
        }
      }

      const limits = data.limits || {};
      const limitList = $("#limitList");
      if (limitList) {
        limitList.replaceChildren();
        const pairs = [
          ["每日提问", formatNumber(limits.questions_per_day) + " 次"],
          ["每日工具调用", formatNumber(limits.tool_calls_per_day) + " 次"],
          ["每分钟请求", formatNumber(limits.requests_per_minute) + " 次"],
          ["单次任务超时", formatNumber(limits.task_timeout_seconds) + " 秒"],
          ["单次最多工具", limits.max_tool_calls_per_question == null ? "不设上限" : formatNumber(limits.max_tool_calls_per_question) + " 次"],
          ["单次模型尝试", formatNumber(limits.model_max_attempts) + " 次"],
        ];
        for (const pair of pairs) {
          const item = document.createElement("div");
          item.className = "limit-item";
          const label = document.createElement("span");
          label.textContent = pair[0];
          const value = document.createElement("span");
          value.textContent = pair[1];
          item.append(label, value);
          limitList.appendChild(item);
        }
      }
    } catch (error) {
      showError(error.message);
    }
  }

  if (page === "login") initLogin();
  else if (page === "chat") initChat();
  else if (page === "usage") initUsage();
})();
