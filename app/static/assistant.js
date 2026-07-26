// ======================================================================
// 助手对话前端：用 fetch + ReadableStream 解析 SSE，实现打字机式流式回答。
// 零依赖（原生 JS），可直接被浏览器运行，不需打包构建。
// ======================================================================
(function () {
  "use strict";

  var logEl = document.getElementById("chat-log");
  var welcomeEl = document.getElementById("welcome");
  var inputEl = document.getElementById("chat-input");
  var sendBtn = document.getElementById("send-btn");
  var busy = false;           // 是否正在等待回答（防重复发送）
  var currentAnswerEl = null; // 当前助手消息的「回答文本容器」

  // ---------- 主题切换（与门户其它页面共用 localStorage 的 'theme' 键）----------
  var root = document.documentElement;
  var saved = localStorage.getItem("theme");
  if (saved) root.setAttribute("data-theme", saved);
  document.getElementById("theme-toggle-chat").onclick = function () {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
    this.textContent = next === "dark" ? "☀️" : "🌙";
  };

  // ---------- 输入框：自动撑高 + Enter 发送 ----------
  inputEl.addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 160) + "px";
  });
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  sendBtn.onclick = send;

  // 建议问题：点击直接发送
  document.querySelectorAll(".suggest").forEach(function (b) {
    b.onclick = function () {
      inputEl.value = b.textContent;
      send();
    };
  });

  document.getElementById("new-chat").onclick = function () {
    if (busy) return;
    logEl.innerHTML = "";
    welcomeEl.style.display = "";
  };

  // ---------- 发送 ----------
  function send() {
    var text = inputEl.value.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    welcomeEl.style.display = "none";
    inputEl.value = "";
    inputEl.style.height = "auto";

    // 1) 用户气泡
    addMessage("user", text);
    // 2) 助手气泡（先空，带光标），并记下回答容器
    var refs = addMessage("assistant", "");
    currentAnswerEl = refs.answer;
    refs.bubble.classList.add("empty"); // 显示闪烁光标

    // 3) 流式请求
    fetch("/api/assistant/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.text().then(function (t) {
            throw new Error("HTTP " + resp.status + " " + t);
          });
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
              handleBlock(block);
            }
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        if (currentAnswerEl) currentAnswerEl.textContent += "\n[出错了：" + err.message + "]";
      })
      .finally(function () {
        if (refs.bubble) refs.bubble.classList.remove("empty");
        busy = false;
        sendBtn.disabled = false;
        scrollBottom();
      });
  }

  // ---------- 解析一段 SSE（event:/data: 两行）----------
  function handleBlock(block) {
    var event = "message", data = "";
    block.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) event = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!data) return;
    try {
      if (event === "trace") renderTrace(JSON.parse(data));
      else if (event === "token") appendToken(data);
      // done 事件无需处理（finally 会收尾）
    } catch (e) {
      /* 忽略单条解析失败 */
    }
  }

  function appendToken(t) {
    if (currentAnswerEl) currentAnswerEl.textContent += t;
    scrollBottom();
  }

  // ---------- 渲染「思考过程」面板 ----------
  function renderTrace(t) {
    if (!t || !currentAnswerEl) return;
    var bubble = currentAnswerEl.closest(".bubble");
    if (!bubble) return;
    if (bubble.querySelector(".trace")) return; // 只渲染一次

    var html = '<details class="trace" open><summary>🧠 思考过程（查询改写 / 多智能体 / 检索 / rerank）</summary>';

    // 查询改写
    if (t.rewritten) {
      html += row("查询改写", "<code>" + esc(t.rewritten) + "</code>");
    }
    // 主管分派
    if (t.plan && t.plan.length) {
      html += row("主管分派工人", t.plan.map(function (p) { return "<code>" + esc(p) + "</code>"; }).join(" "));
    }
    // agent 协作日志
    if (t.agent_trace && t.agent_trace.length) {
      html += '<div class="trace-row"><span class="trace-label">多智能体协作：</span><ul>';
      t.agent_trace.forEach(function (s) {
        html += "<li><b>" + esc(s.agent) + "</b>：" + esc(s.detail) + "</li>";
      });
      html += "</ul></div>";
    }
    // 检索轨迹
    var r = t.retrieval || {};
    if (r.vector_top || r.bm25_top) {
      html += row("混合检索",
        "向量召回 " + (r.vector_top || []).length + " 条 + BM25 召回 " + (r.bm25_top || []).length + " 条 → RRF 融合");
      if (r.rerank_before && r.rerank_after) {
        html += row("rerank 重排前→后",
          short(r.rerank_before) + " → " + short(r.rerank_after));
      }
    }
    // 图谱命中
    if (t.graph_triples && t.graph_triples.length) {
      html += '<div class="trace-row"><span class="trace-label">图谱关系命中：</span><ul>';
      t.graph_triples.slice(0, 6).forEach(function (g) {
        html += "<li>" + esc(g.subject) + " —" + esc(g.relation) + "→ " + esc(g.obj) + "</li>";
      });
      html += "</ul></div>";
    }
    html += "</details>";
    bubble.insertAdjacentHTML("beforeend", html);
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

  // ---------- 工具 ----------
  function addMessage(role, text) {
    var msg = document.createElement("div");
    msg.className = "msg " + role;
    var avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "你" : "🤖";
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    var answer = document.createElement("div");
    answer.className = "answer";
    answer.textContent = text;
    bubble.appendChild(answer);
    msg.appendChild(avatar);
    msg.appendChild(bubble);
    logEl.appendChild(msg);
    scrollBottom();
    return { bubble: bubble, answer: answer };
  }
  function scrollBottom() {
    var main = document.getElementById("chat-main");
    main.scrollTop = main.scrollHeight;
  }
})();
