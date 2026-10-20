/* 大屏数据层：真实接口拉取 + 面板渲染 + 筛选 + 轮询 + 一键采集。
   接口：/api/health /api/sources /api/batches /api/leads /api/items /api/run /api/run/status。 */
(function () {
  window.__err = window.__err || [];
  var $ = function (i) { return document.getElementById(i); };
  var STAGE_NAME = { S1: "S1 选品", S2: "S2 供应链", S3: "S3 交易结构", S4: "S4 客户开发" };
  var GRADE = { high: "A", medium: "B", low: "C" };
  var GRADE_CLS = { A: "a", B: "b", C: "c" };
  // 国家/区域 → [纬度, 经度]（内置 30 个，覆盖常见跨境目标市场；未收录国不打点，空国家不打点）
  var LL = {
    "美国":[38,-97],"日本":[36,138],"德国":[51,10],"英国":[54,-2],"法国":[47,2],
    "意大利":[42,12],"西班牙":[40,-4],"俄罗斯":[60,90],"澳大利亚":[-25,135],"加拿大":[56,-106],
    "巴西":[-10,-52],"墨西哥":[23,-102],"印度":[21,78],"印尼":[-2,118],"越南":[16,106],
    "泰国":[15,101],"马来西亚":[4,102],"菲律宾":[13,122],"新加坡":[1.3,103.8],"韩国":[36,128],
    "沙特":[24,45],"阿联酋":[24,54],"土耳其":[39,35],"埃及":[26,30],"南非":[-29,24],
    "尼日利亚":[9,8],"阿根廷":[-34,-64],"波兰":[52,19],"荷兰":[52,5],"瑞士":[47,8],
    "中国":[35,105],"中国山东":[36.7,117],"中国广东":[23.5,113.5],"中国浙江":[29,120],
    "中国江苏":[32.5,119.5],"中国福建":[26,118],"香港":[22.3,114.2],"台湾":[23.5,121],
    "东盟":[5,115],"欧洲":[50,15],"中东":[27,45],"北美":[40,-100],"东南亚":[10,105],
    "拉美":[0,-60],"非洲":[5,20],"中亚":[45,70],"南亚":[22,80]
  };
  var TIME_OPTS = [["1小时",3600e3],["3小时",10800e3],["6小时",21600e3],["24小时",86400e3],["48小时",172800e3],["7天",604800e3],["全部",Infinity]];

  var LEAD = [], BATCH_ID = null, BATCH_INFO = null, SOURCES = null, BATCHES = [], RUN = null, HEALTH = null, EXCL = [];
  var F = { time: "3小时", stage: { S1: 1, S2: 1, S3: 1, S4: 1 }, ind: {}, grade: { A: 1, B: 1, C: 1 } };
  var collecting = false;
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function indLabel(v) { return (v && String(v).trim()) ? String(v).trim() : "未标注"; }
  function fmtTime(t) {
    if (!t) return "—";
    var d = new Date(String(t).replace(" ", "T"));
    if (isNaN(d.getTime())) return t;
    var p = function (x) { return (x < 10 ? "0" : "") + x; };
    var now = new Date();
    if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()) {
      return p(d.getHours()) + ":" + p(d.getMinutes());
    }
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function winMs() {
    for (var i = 0; i < TIME_OPTS.length; i++) if (TIME_OPTS[i][0] === F.time) return TIME_OPTS[i][1];
    return 10800e3;
  }
  function api(path) {
    return fetch(path, { headers: { "Accept": "application/json" } }).then(function (r) {
      if (r.status === 200) return r.json();
      throw new Error(path + " HTTP " + r.status);
    });
  }

  async function fetchAll() {
    var tasks = [
      api("/api/health").catch(function () { return null; }),
      api("/api/sources").catch(function () { return null; }),
      api("/api/batches").catch(function () { return null; }),
      api("/api/leads?batch=latest&limit=50").catch(function () { return null; }),
      api("/api/run/status").catch(function () { return null; })
    ];
    var res = await Promise.all(tasks);
    HEALTH = res[0];
    SOURCES = (res[1] && res[1].sources) || null;
    BATCHES = (res[2] && res[2].batches) || [];
    LEAD = (res[3] && res[3].leads) || [];
    BATCH_ID = (res[3] && res[3].batch_id) || null;
    RUN = res[4];
    BATCH_INFO = null;
    for (var i = 0; i < BATCHES.length; i++) if (BATCHES[i].id === BATCH_ID) BATCH_INFO = BATCHES[i];
    if (BATCH_ID) {
      var it = await api("/api/items?batch=" + BATCH_ID + "&limit=200").catch(function () { return null; });
      EXCL = ((it && it.items) || []).filter(function (x) {
        return x.is_duplicate === 1 || x.coarse_status === "rejected" ||
               (x.coarse_status === "ok" && x.is_economic === false) || x.coarse_status === "skipped";
      });
    } else { EXCL = []; }
    buildIndChips();
    showEmptyState();
    render();
    window.__leads = LEAD; window.__batch = BATCH_INFO; window.__sources = SOURCES; window.__excl = EXCL;
  }

  function buildIndChips() {
    var vs = {};
    LEAD.forEach(function (l) { vs[indLabel(l.vertical)] = 1; });
    var keys = Object.keys(vs).sort();
    var keep = {};
    keys.forEach(function (k) { keep[k] = 1; if (F.ind[k] === undefined) F.ind[k] = 1; });
    Object.keys(F.ind).forEach(function (k) { if (!keep[k]) delete F.ind[k]; });
    $("inds").innerHTML = keys.map(function (k) {
      return '<span class="op' + (F.ind[k] ? " sel" : "") + '">' + esc(k) + "</span>";
    }).join("");
    bindChips($("inds"), "ind");
  }

  function bindChips(container, key) {
    container.querySelectorAll(".op").forEach(function (op) {
      op.onclick = function () {
        var v = op.dataset.code || op.textContent;
        if (key === "time") {
          F.time = op.textContent;
          container.querySelectorAll(".op").forEach(function (x) { x.classList.remove("sel"); });
          op.classList.add("sel");
        } else {
          var on = !F[key][v];
          F[key][v] = on ? 1 : 0;
          op.classList.toggle("sel", on);
        }
        render();
      };
    });
  }

  function render() {
    var ver = HEALTH ? HEALTH.version : "—";
    $("ver").textContent = "v" + ver;
    $("next").textContent = "下一轮 " + (RUN && RUN.next_run_at ? RUN.next_run_at : "—");
    var cost = (BATCH_INFO && BATCH_INFO.stats && BATCH_INFO.stats.cost != null) ? BATCH_INFO.stats.cost : null;
    $("spend").textContent = "最新批次 ¥" + (cost == null ? "—" : cost.toFixed(4));
    var cnt = BATCH_INFO ? BATCH_INFO.item_count : 0;
    if (!collecting) $("stat").innerHTML = '<span class="dot"></span>运行中 · 已采集 ' + cnt + " 条";
    renderSrcLights();
    renderPanels();
    renderFooter();
    var dots = buildDots();
    if (window.EARTH) window.EARTH.setDots(dots);
    window.__dots = dots.length;
    $("fdots").textContent = window.__dots;
  }

  // 源状态灯：优先 /api/sources；后端缺失(404)时用最近一轮 stats.sources_ok/failed 兜底
  function renderSrcLights() {
    var html = "";
    if (SOURCES && SOURCES.length) {
      var ok = 0;
      SOURCES.forEach(function (s) {
        if (s.status === "ok") ok++;
        var cls = s.status === "ok" ? "sg" : "sr";
        var tip = s.name + " · 最近抓取 " + (s.last_count || 0) + " 条" +
          (s.status === "error" ? " · 失败：" + (s.last_error || "") : s.status === "never" ? " · 尚未采集" : "");
        html += '<i class="' + cls + '" title="' + esc(tip) + '"></i>';
      });
      window.__greens = ok;
    } else {
      var st = (BATCH_INFO && BATCH_INFO.stats) || {};
      var okn = st.sources_ok || 0, failn = st.sources_failed || 0;
      window.__greens = okn;
      for (var i = 0; i < okn; i++) html += '<i class="sg" title="源 ' + (i + 1) + ' · 最近一轮正常"></i>';
      for (var j = 0; j < failn; j++) html += '<i class="sr" title="源失败 · 最近一轮异常"></i>';
    }
    $("srcs").innerHTML = html;
  }

  function visibleLeads() {
    return LEAD.filter(function (l) {
      if (!F.stage[l.stage]) return false;
      if (!F.ind[indLabel(l.vertical)]) return false;
      if (!F.grade[GRADE[l.confidence] || "C"]) return false;
      return true;
    });
  }

  function put(pid, arr, fn, empty) {
    var el = $(pid);
    el.innerHTML = arr.length ? arr.slice(0, 40).map(fn).join("") : '<div class="emp">' + empty + "</div>";
  }

  function cardHTML(l) {
    var st = l.stage || "S1", g = GRADE[l.confidence] || "C";
    var n = l.next_action || {};
    var tt = String(l.title || "").replace(/\s+/g, " ").trim();
    return '<div class="card s' + st.slice(1) + '" data-id="' + (l.id || "") +
      '" title="' + esc(tt) + '（点击查看详情）">' +
      '<div class="ch"><span class="st s' + st.slice(1) + '">' + (STAGE_NAME[st] || st) + "</span>" +
      '<span class="tg">' + esc(indLabel(l.vertical)) + "</span>" +
      '<span class="gr ' + GRADE_CLS[g] + '">' + g + "</span></div>" +
      '<div class="fact">' + esc(tt) + "</div>" +
      '<div class="next">→ ' + esc(n.action || "—") + "</div>" +
      '<div class="cf"><span>钱流：' + esc(l.money_form || "—") + "</span><span>" + fmtTime(l.published_at) + "</span></div></div>";
  }

  function exHTML(x) {
    var why = x.coarse_status === "rejected" ? "粗筛未通过" :
      (x.is_duplicate === 1 ? "重复条目，已去重" : (x.is_economic === false ? "经济性不足" : "粗筛跳过"));
    return '<div class="card off"><div class="ch"><span class="st s1" style="background:#6b7a94">已排除</span>' +
      '<span class="tg">' + esc(x.source || "") + '</span><span class="gr c">×</span></div>' +
      '<div class="fact">' + esc(x.title) + "</div>" +
      '<div class="cf"><span class="ex">' + why + "</span><span>" + fmtTime(x.published_at) + "</span></div></div>";
  }

  function renderPanels() {
    var vis = visibleLeads();
    var w = winMs();
    var start = BATCH_INFO ? new Date(String(BATCH_INFO.started_at).replace(" ", "T")).getTime() : 0;
    var isNew = (w === Infinity) || (start && (Date.now() - start) <= w);
    var newA = [], oldA = [], aB = [], s1 = [], s3 = [];
    vis.forEach(function (l) {
      if (isNew) newA.push(l); else oldA.push(l);
      var g = GRADE[l.confidence] || "C";
      if (g === "A" || g === "B") aB.push(l);
      if (l.stage === "S1") s1.push(l);
      if (l.stage === "S3") s3.push(l);
    });
    put("pNew", newA, cardHTML, "当前时间窗内暂无新增线索");
    put("pA", aB, cardHTML, "暂无 A·B 级线索");
    put("pS1", s1, cardHTML, "暂无 S1 选品线索");
    put("pS3", s3, cardHTML, "暂无 S3 交易结构线索");
    put("pOld", oldA, cardHTML, "暂无更早批次线索");
    put("pEx", EXCL, exHTML, "暂无已排除条目");
    // 点击卡片进详情页（委托到面板容器，避免每次重渲染重复绑定）
    ["pNew", "pA", "pS1", "pS3", "pOld"].forEach(function (pid) {
      var box = $(pid);
      if (box && !box.__bound) {
        box.__bound = true;
        box.addEventListener("click", function (ev) {
          var el = ev.target.closest(".card");
          var id = el && el.getAttribute("data-id");
          if (id) window.open("/static/detail.html?id=" + id, "_blank");
        });
      }
    });
    $("tNew").textContent = "新增（" + F.time + "）";
    $("cNew").textContent = newA.length;
    $("cA").textContent = aB.length;
    $("cS1").textContent = s1.length;
    $("cS3").textContent = s3.length;
    $("cOld").textContent = oldA.length;
    $("cEx").textContent = EXCL.length;
    // __cards：渲染出的去重标题数（线索卡 + 排除卡）
    var titles = {};
    vis.forEach(function (l) { titles["l" + l.id] = 1; });
    EXCL.slice(0, 40).forEach(function (x) { titles["x" + x.id] = 1; });
    window.__cards = Object.keys(titles).length;
  }

  function buildDots() {
    var m = {};
    LEAD.forEach(function (l) {
      var c = l.country;
      if (!c) return;
      var e = LL[c];
      if (!e) return;
      if (!m[c]) m[c] = { lat: e[0], lon: e[1], n: 0, stages: {} };
      m[c].n++;
      m[c].stages[l.stage || "S1"] = (m[c].stages[l.stage || "S1"] || 0) + 1;
    });
    var out = [];
    Object.keys(m).forEach(function (k) {
      var v = m[k], st = "S1", mx = 0;
      Object.keys(v.stages).forEach(function (s) { if (v.stages[s] > mx) { mx = v.stages[s]; st = s; } });
      out.push({ lat: v.lat, lon: v.lon, n: v.n, stage: st });
    });
    return out;
  }

  function renderFooter() {
    $("fver").textContent = "v" + (HEALTH ? HEALTH.version : "—");
    $("fsrc").textContent = "数据源 " + (window.__greens == null ? "-" : window.__greens) + "/" + (SOURCES ? SOURCES.length : 8);
    $("fbatch").textContent = "最新批次 #" + (BATCH_ID || "—");
    var dur = BATCH_INFO && BATCH_INFO.stats && BATCH_INFO.stats.duration_s;
    $("fdur").textContent = dur != null ? "本轮 " + Math.round(dur) + "s" : "本轮 —";
  }

  function showEmptyState() {
    var ov = $("ovl");
    if (!BATCHES.length) {
      ov.innerHTML = "<b>暂无批次</b> · 点击右上角「一键采集」开始第一轮采集";
      ov.classList.add("show");
    } else if (!LEAD.length) {
      ov.innerHTML = "<b>暂无深加工线索</b> · 最新批次尚未产出线索卡，等待下一轮采集";
      ov.classList.add("show");
    } else {
      ov.classList.remove("show");
    }
    if (SOURCES && SOURCES.length && SOURCES.every(function (s) { return s.status !== "ok"; })) {
      $("sfail").classList.add("show");
    } else {
      $("sfail").classList.remove("show");
    }
  }

  // ===== 一键采集 =====
  var toastTimer = null;
  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 6000);
  }
  var btn = $("btn");
  async function runNow() {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      var res = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      window.__runcode = res.status;
      if (res.status === 409) {
        var j409 = await res.json().catch(function () { return {}; });
        toast((j409 && j409.reason) || "上一轮尚未结束，请稍后重试");
      } else if (res.status === 200) {
        var j = await res.json().catch(function () { return {}; });
        toast((j && j.message) || "已开始本轮采集");
        startCollecting();
      } else {
        toast("触发失败（HTTP " + res.status + "）");
      }
    } catch (e) {
      window.__err.push(String(e));
      toast("无法连接后端服务");
    }
    btn.disabled = false;
  }
  function startCollecting() {
    collecting = true;
    var n = 0;
    $("stat").innerHTML = '<span class="dot" style="background:#f5a623"></span>采集中…';
    var iv = setInterval(function () {
      if (!collecting) { clearInterval(iv); return; }
      n++;
      $("stat").innerHTML = '<span class="dot" style="background:#f5a623"></span>采集中 ' + n + "s";
    }, 1000);
    statusLoop();
  }
  async function statusLoop() {
    while (collecting) {
      var st = await api("/api/run/status").catch(function () { return null; });
      if (!st || !st.running) {
        collecting = false;
        toast("本轮采集完成，正在刷新数据…");
        await fetchAll();
        break;
      }
      await sleep(5000);
    }
  }
  btn.onclick = runNow;

  // ===== 时钟 / 抽屉 / 轮询 =====
  function clk() {
    var d = new Date(Date.now() + 8 * 3600e3), p = function (n) { return (n < 10 ? "0" : "") + n; };
    $("clk").textContent = "UTC+8 " + d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" +
      p(d.getUTCDate()) + " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds());
  }
  clk(); setInterval(clk, 1000);
  var drw = $("drw"), dtog = $("dtog");
  dtog.onclick = function () {
    drw.classList.toggle("coll");
    dtog.textContent = drw.classList.contains("coll") ? "›" : "‹";
  };
  bindChips($("opTime"), "time");
  bindChips($("opStage"), "stage");
  bindChips($("opGrade"), "grade");
  fetchAll();
  setInterval(fetchAll, 60000);
})();
