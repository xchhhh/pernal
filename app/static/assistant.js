// ======================================================================
// AI 问答主界面前端（ChatGPT 式）
// 功能：
//   1) 多会话管理：历史会话存 localStorage，侧栏可切换/删除，新对话一键开；
//   2) 流式问答：fetch + ReadableStream 解析 SSE（trace 思考面板 + token 打字机）；
//   3) 极简 Markdown 渲染（加粗 / 行内代码 / 无序列表），回答更美观；
//   4) 移动端抽屉侧栏；深浅色与门户共用 localStorage 的 'theme' 键。
// 零依赖原生 JS，无需打包构建。
// ======================================================================
(function () {
  "use strict";

  var STORAGE_KEY = "portal_convs_v1";

  // ---------- DOM ----------
  var logEl = document.getElementById("chat-log");
  var welcomeEl = document.getElementById("welcome");
  var inputEl = document.getElementById("chat-input");
  var sendBtn = document.getElementById("send-btn");
  var convListEl = document.getElementById("conv-list");
  var topbarTitle = document.getElementById("topbar-title");
  var sidebar = document.getElementById("sidebar");
  var overlay = document.getElementById("sidebar-overlay");
  var menuBtn = document.getElementById("menu-btn");

  // ---------- 状态 ----------
  var convs = loadConvs();      // [{id, title, messages:[{role,text,trace}]}]
  var currentId = null;         // 当前会话 id（null = 新对话欢迎态）
  var busy = false;
  var currentAnswerEl = null;   // 正在流式写入的助手消息容器
  var currentTrace = null;      // 正在生成的消息的思考轨迹
  var currentSources = null;    // 正在生成的消息的资料来源（溯源）

  // ================= 会话持久化 =================
  function loadConvs() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; }
    catch (e) { return []; }
  }
  function saveConvs() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(convs.slice(0, 30))); } catch (e) {}
  }
  function currentConv() {
    for (var i = 0; i < convs.length; i++) if (convs[i].id === currentId) return convs[i];
    return null;
  }

  // ================= 侧栏会话列表 =================
  function renderConvList() {
    convListEl.innerHTML = "";
    if (!convs.length) {
      convListEl.innerHTML = '<div class="conv-empty">暂无历史会话</div>';
      return;
    }
    convs.forEach(function (c) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "conv-item" + (c.id === currentId ? " active" : "");
      var title = document.createElement("span");
      title.className = "conv-title";
      title.textContent = c.title;
      var del = document.createElement("span");
      del.className = "conv-del";
      del.textContent = "✕";
      del.title = "删除会话";
      del.setAttribute("role", "button");
      del.setAttribute("aria-label", "删除会话 " + c.title);
      del.onclick = function (e) {
        e.stopPropagation();
        convs = convs.filter(function (x) { return x.id !== c.id; });
        saveConvs();
        if (currentId === c.id) newChat();
        renderConvList();
      };
      item.appendChild(title);
      item.appendChild(del);
      item.onclick = function () { selectConv(c.id); closeDrawer(); };
      convListEl.appendChild(item);
    });
  }

  function selectConv(id) {
    currentId = id;
    var c = currentConv();
    logEl.innerHTML = "";
    welcomeEl.style.display = "none";
    topbarTitle.textContent = c ? c.title : "新对话";
    if (c) c.messages.forEach(function (m) { addMessage(m.role, m.text, m.trace, m.sources); });
    renderConvList();
    scrollBottom();
  }

  function newChat() {
    if (busy) return;
    currentId = null;
    logEl.innerHTML = "";
    welcomeEl.style.display = "";
    topbarTitle.textContent = "新对话";
    renderConvList();
    inputEl.focus();
  }

  // ================= 主题切换 =================
  var root = document.documentElement;
  var themeBtn = document.getElementById("theme-toggle-chat");
  function syncThemeBtn() {
    themeBtn.textContent = (root.getAttribute("data-theme") === "dark" ? "☀️" : "🌙") + " 切换主题";
  }
  var savedTheme = localStorage.getItem("theme");
  if (savedTheme) root.setAttribute("data-theme", savedTheme);
  syncThemeBtn();
  themeBtn.onclick = function () {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    syncThemeBtn();
  };

  // ================= 移动端抽屉 =================
  function openDrawer() {
    sidebar.classList.add("open");
    overlay.hidden = false;
    menuBtn.setAttribute("aria-expanded", "true");
  }
  function closeDrawer() {
    sidebar.classList.remove("open");
    overlay.hidden = true;
    menuBtn.setAttribute("aria-expanded", "false");
  }
  menuBtn.onclick = function () {
    sidebar.classList.contains("open") ? closeDrawer() : openDrawer();
  };
  overlay.onclick = closeDrawer;
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDrawer();
  });

  // ================= 输入框 =================
  inputEl.addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 180) + "px";
  });
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  sendBtn.onclick = send;
  document.getElementById("new-chat").onclick = function () { newChat(); closeDrawer(); };
  document.querySelectorAll(".suggest").forEach(function (b) {
    b.onclick = function () { inputEl.value = b.textContent; send(); };
  });

  // ================= 发送 + SSE 流式 =================
  function send() {
    var text = inputEl.value.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    welcomeEl.style.display = "none";
    inputEl.value = "";
    inputEl.style.height = "auto";

    // 新对话：首条消息时才真正建会话（标题取问题前 22 字）
    if (!currentId) {
      currentId = "c" + Date.now();
      var title = text.length > 22 ? text.slice(0, 22) + "…" : text;
      convs.unshift({ id: currentId, title: title, messages: [] });
      topbarTitle.textContent = title;
    }
    var conv = currentConv();

    // 1) 用户消息
    addMessage("user", text);
    if (conv) { conv.messages.push({ role: "user", text: text }); saveConvs(); }

    // 2) 助手占位消息（闪烁光标）
    var refs = addMessage("assistant", "");
    currentAnswerEl = refs.answer;
    currentTrace = null;
    currentSources = null;
    refs.bubble.classList.add("empty");
    renderConvList();

    // 3) 流式请求
    fetch("/api/assistant/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (t) { throw new Error("HTTP " + resp.status + " " + t); });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) return;
            buf += decoder.decode(r.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf("\n\n")) !== -1) {
              var block = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              handleBlock(block, refs.bubble);
            }
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        if (currentAnswerEl) {
          currentAnswerEl.dataset.raw = (currentAnswerEl.dataset.raw || "") + "\n[出错了：" + err.message + "]";
          renderAnswer(currentAnswerEl);
        }
      })
      .finally(function () {
        refs.bubble.classList.remove("empty");
        // 持久化助手消息（含思考轨迹，供会话重放时还原面板）
        if (conv && currentAnswerEl) {
          conv.messages.push({
            role: "assistant",
            text: currentAnswerEl.dataset.raw || "",
            trace: currentTrace,
            sources: currentSources,
          });
          saveConvs();
        }
        busy = false;
        sendBtn.disabled = false;
        renderConvList();
        scrollBottom();
      });
  }

  function handleBlock(block, bubble) {
    var event = "message", data = "";
    block.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) event = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!data) return;
    try {
      if (event === "trace") {
        currentTrace = JSON.parse(data);
        renderTrace(currentTrace, bubble);
      } else if (event === "sources") {
        currentSources = JSON.parse(data);
        renderSources(currentSources, bubble);
      } else if (event === "token") {
        appendToken(data);
      }
    } catch (e) { /* 忽略单条解析失败 */ }
  }

  function appendToken(t) {
    if (!currentAnswerEl) return;
    currentAnswerEl.dataset.raw = (currentAnswerEl.dataset.raw || "") + t;
    renderAnswer(currentAnswerEl);
    scrollBottom();
  }

  // ================= 渲染 =================
  // 极简 Markdown：先转义 HTML，再处理 **加粗**、`行内代码`、- 无序列表
  function mdToHtml(raw) {
    var esc = raw.replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
    esc = esc.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    esc = esc.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    esc = esc.replace(/^[-•]\s+(.+)$/gm, "<span class='li'>• $1</span>");
    return esc;
  }
  function renderAnswer(el) { el.innerHTML = mdToHtml(el.dataset.raw || ""); }

  function renderTrace(t, bubble) {
    if (!t || !bubble || bubble.querySelector(".trace")) return;
    bubble.insertAdjacentHTML("beforeend", traceHTML(t));
  }

  // 资料来源（溯源）：把检索命中的板块/文档标题渲染成可点击的溯源列表
  function renderSources(srcs, bubble) {
    if (!bubble || !srcs || !srcs.length) return;
    var old = bubble.querySelector(".sources");
    if (old) old.parentNode.removeChild(old);
    var html = '<div class="sources"><span class="sources-label">📚 资料来源：</span>';
    html += srcs.map(function (s) {
      return '<span class="source-chip">' + esc(s.title || s.section || "资料") + "</span>";
    }).join("");
    html += "</div>";
    bubble.insertAdjacentHTML("beforeend", html);
  }


  function traceHTML(t) {
    var html = '<details class="trace"><summary>🧠 思考过程（查询改写 / 多智能体 / 混合检索 / RRF / rerank）</summary>';
    if (t.rewritten) html += row("查询改写", "<code>" + esc(t.rewritten) + "</code>");
    if (t.plan && t.plan.length) {
      html += row("主管分派", t.plan.map(function (p) { return "<code>" + esc(p) + "</code>"; }).join(" "));
    }
    if (t.agent_trace && t.agent_trace.length) {
      html += '<div class="trace-row"><span class="trace-label">多智能体协作：</span><ul>';
      t.agent_trace.forEach(function (s) {
        html += "<li><b>" + esc(s.agent) + "</b>：" + esc(s.detail) + "</li>";
      });
      html += "</ul></div>";
    }
    var r = t.retrieval || {};
    if (r.vector_top || r.bm25_top) {
      html += row("混合检索",
        "向量召回 " + (r.vector_top || []).length + " 条 + BM25 召回 " + (r.bm25_top || []).length + " 条 → RRF 融合");
      if (r.rerank_before && r.rerank_after) {
        html += row("rerank 重排前→后", short(r.rerank_before) + " → " + short(r.rerank_after));
      }
    }
    if (t.graph_triples && t.graph_triples.length) {
      html += '<div class="trace-row"><span class="trace-label">图谱关系命中：</span><ul>';
      t.graph_triples.slice(0, 6).forEach(function (g) {
        html += "<li>" + esc(g.subject) + " —" + esc(g.relation) + "→ " + esc(g.obj) + "</li>";
      });
      if (t.graph_triples.length > 6) html += "<li>…共 " + t.graph_triples.length + " 条</li>";
      html += "</ul></div>";
    }
    return html + "</details>";
  }

  function row(label, val) {
    return '<div class="trace-row"><span class="trace-label">' + label + "：</span>" + val + "</div>";
  }
  function short(arr) {
    return "[" + arr.slice(0, 4).map(function (x) { return esc(String(x)); }).join(", ") + (arr.length > 4 ? ", …" : "") + "]";
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function addMessage(role, text, trace, sources) {
    var msg = document.createElement("div");
    msg.className = "msg " + role;
    var avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "🤖";
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    var answer = document.createElement("div");
    answer.className = "answer";
    answer.dataset.raw = text || "";
    renderAnswer(answer);
    bubble.appendChild(answer);
    msg.appendChild(avatar);
    msg.appendChild(bubble);
    logEl.appendChild(msg);
    if (trace) renderTrace(trace, bubble);   // 会话重放时还原思考面板
    if (sources && sources.length) renderSources(sources, bubble);  // 还原资料来源
    scrollBottom();
    return { bubble: bubble, answer: answer };
  }

  function scrollBottom() {
    var main = document.getElementById("chat-main");
    main.scrollTop = main.scrollHeight;
  }

  // ================= 启动 =================
  renderConvList();
  if (convs.length) selectConv(convs[0].id);   // 默认打开最近会话
  inputEl.focus();
})();
