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

  var LEAD = [], BATCH_ID = null, BATCH_INFO = null, SOURCES = null, BATCHES = [], RUN = null, HEALTH = null, EXCL = [], BUDGET = null;
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
      api("/api/run/status").catch(function () { return null; }),
      api("/api/budget").catch(function () { return null; }),
      api("/api/platform_items?limit=40").catch(function () { return null; })
    ];
    var res = await Promise.all(tasks);
    HEALTH = res[0];
    SOURCES = (res[1] && res[1].sources) || null;
    BATCHES = (res[2] && res[2].batches) || [];
    RUN = res[4];
    BUDGET = res[5];
    window.__platform = (res[6] && res[6].items) || [];
    // 合并最近几个含线索的批次（latest 批优先 + 最近几个 deep_ok>0 的批，标题全部来自真实接口）
    var latestLeads = (res[3] && res[3].leads) || [];
    var latestBid = (res[3] && res[3].batch_id) || null;
    var okBatches = BATCHES.filter(function (b) { return b.stats && b.stats.deep_ok > 0; }).slice(0, 3);
    var cand = [];
    var hasLatest = false;
    okBatches.forEach(function (b) { if (b.id === latestBid) hasLatest = true; });
    if (!hasLatest) cand.push(latestBid);
    okBatches.forEach(function (b) { if (cand.indexOf(b.id) < 0) cand.push(b.id); });
    cand = cand.slice(0, 3).filter(Boolean);
    var leadList = latestLeads.slice();
    for (var i = 0; i < cand.length; i++) {
      var lj = await api("/api/leads?batch=" + cand[i] + "&limit=50").catch(function () { return null; });
      if (lj && lj.leads) leadList = leadList.concat(lj.leads);
    }
    var seen = {};
    LEAD = [];
    leadList.forEach(function (l) { if (!seen[l.id]) { seen[l.id] = 1; LEAD.push(l); } });
    BATCH_ID = latestBid || (okBatches.length ? okBatches[0].id : null);
    BATCH_INFO = null;
    for (var j = 0; j < BATCHES.length; j++) if (BATCHES[j].id === BATCH_ID) BATCH_INFO = BATCHES[j];
    if (BATCH_ID) {
      var it = await api("/api/items?batch=" + BATCH_ID + "&limit=200").catch(function () { return null; });
      var all = (it && it.items) || [];
      // 重复条目单独计数，不进「已排除」列表（否则面板像垃圾堆）
      window.__dupCount = all.filter(function (x) { return x.is_duplicate === 1; }).length;
      EXCL = all.filter(function (x) {
        return x.is_duplicate !== 1 && (x.coarse_status === "rejected" ||
               (x.coarse_status === "ok" && x.is_economic === false) || x.coarse_status === "skipped");
      });
    } else { EXCL = []; }
    buildIndChips();
    showEmptyState();
    // 先暴露给地球模块，再渲染（否则 setDots 时拿不到 leads，光点缺 id/潜力）
    window.__leads = LEAD; window.__batch = BATCH_INFO; window.__sources = SOURCES; window.__excl = EXCL;
    render();
    // 地球延迟初始化：先把卡片渲染出来，再加载 3D（否则贴图/着色器会阻塞首屏）
    if (window.__earthBoot && !window.__earthBooted) {
      window.__earthBooted = true;
      var boot = function () {
        try { if (window.__earthBoot) window.__earthBoot(); } catch (e) { }
        if (window.EARTH && window.EARTH.setDots) {
          try { window.EARTH.setDots(buildDots()); } catch (e) { }
        }
      };
      if (window.requestIdleCallback) window.requestIdleCallback(boot, { timeout: 1200 });
      else setTimeout(boot, 200);
    }
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
    var doneB = null;
    BATCHES.forEach(function (b) {
      if (!doneB && (b.status === "done" || b.status === "degraded") && b.stats && b.stats.cost != null) doneB = b;
    });
    var cost = doneB ? doneB.stats.cost : null;
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
    renderBudgetBanner();
  }

  // 预算熔断横幅：mode≠ok 时大屏顶部显示中文提示（硬停 / 软停）
  function renderBudgetBanner() {
    var b = $("budgetBanner");
    if (!b) return;
    var m = BUDGET && BUDGET.mode;
    if (!m || m === "ok") { b.className = ""; b.innerHTML = ""; return; }
    var spent = BUDGET.spent == null ? "—" : Number(BUDGET.spent).toFixed(2);
    var budget = BUDGET.budget == null ? "—" : Number(BUDGET.budget).toFixed(2);
    b.className = "show " + m;
    b.innerHTML = m === "hard"
      ? "预算硬停：本月已用 ¥" + spent + "，剩余不足 2%（预算 ¥" + budget +
        "），已停止所有模型调用，仅保留采集"
      : "预算预警：本月已用 ¥" + spent + "，剩余不足 10%（预算 ¥" + budget +
        "），深加工已暂停，仅保留粗筛";
    window.__budgetMode = m;
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
      // 兜底：取最近一轮 done/degraded 且有统计的批（BATCH_INFO 可能是 failed 批，stats 为空）
      var st = null;
      BATCHES.forEach(function (b) {
        if (!st && (b.status === "done" || b.status === "degraded") && b.stats && b.stats.sources_ok != null) st = b.stats;
      });
      st = st || {};
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
    return '<div class="card s' + st.slice(1) + '" data-id="' + (l.id || "") + '">' +
      '<div class="ch"><span class="st s' + st.slice(1) + '">' + (STAGE_NAME[st] || st) + "</span>" +
      '<span class="tg">' + esc(indLabel(l.vertical)) + "</span>" +
      '<button class="ai" data-ai="' + (l.id || "") + '" title="让 AI 解析这条的赚钱机会">AI解析</button>' +
      '<span class="gr ' + GRADE_CLS[g] + '">' + g + "</span></div>" +
      '<div class="fact"><a href="' + esc(l.url || "#") + '" target="_blank" rel="noopener" ' +
      'title="' + esc(tt) + '">' + esc(tt) + "</a></div>" +
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
    var platBox = $("pPlat");
    if (platBox) platBox.innerHTML = platHTML();
    var platCnt = $("cPlat");
    if (platCnt) platCnt.textContent = (window.__platform || []).length;
    // 内容溢出的面板加底部渐隐提示（否则隐藏滚动条后看不出还有内容）
    Array.prototype.forEach.call(document.querySelectorAll("#grid .panel"), function (pn) {
      var box = pn.querySelector(".pc");
      if (!box) return;
      pn.classList.toggle("has-more", box.scrollHeight > box.clientHeight + 4);
    });
    // 点击卡片进详情页（委托到面板容器，避免每次重渲染重复绑定）
    ["pNew", "pA", "pS1", "pS3", "pOld"].forEach(function (pid) {
      var box = $(pid);
      if (box && !box.__bound) {
        box.__bound = true;
        box.addEventListener("click", function (ev) {
          // 点标题链接 / AI解析按钮时不进详情
          if (ev.target.closest("a,button.ai")) {
            var b = ev.target.closest("button.ai");
            if (b) { ev.stopPropagation(); openAnalyze(b.getAttribute("data-ai")); }
            return;
          }
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
    $("cEx").textContent = EXCL.length + (window.__dupCount ? "（去重 " + window.__dupCount + "）" : "");
    // __cards：渲染出的去重标题数（线索卡 + 排除卡）
    var titles = {};
    vis.forEach(function (l) { titles["l" + l.id] = 1; });
    EXCL.slice(0, 40).forEach(function (x) { titles["x" + x.id] = 1; });
    window.__cards = Object.keys(titles).length;
  }

  // 平台信号：热搜 / 榜单原始条目（非线索卡），按来源分组展示
  function platHTML() {
    var rows = window.__platform || [];
    if (!rows.length) return '<div class="empty">当前批次暂无平台侧条目</div>';
    var by = {};
    rows.forEach(function (r) { (by[r.source] = by[r.source] || []).push(r); });
    var html = "";
    Object.keys(by).forEach(function (src) {
      html += '<div class="pg"><div class="pg-t">' + esc(src) + "</div>";
      by[src].forEach(function (r) {
        var meta = [];
        if (r.rank) meta.push("第 " + r.rank + " 位");
        if (r.heat) meta.push("热度 " + r.heat);
        if (r.is_duplicate) meta.push("重复");
        html += '<a class="prow" href="/static/detail.html?id=' + r.id + '" target="_blank">' +
          '<span class="pr-t">' + esc(r.title) + "</span>" +
          (meta.length ? '<span class="pr-m">' + esc(meta.join(" · ")) + "</span>" : "") +
          "</a>";
      });
      html += "</div>";
    });
    return html;
  }

  function buildDots() {
    // 按「单条线索」打点（不再按国家聚合成一个点）：点更多、阶段色更杂。
    // 同国家的点做小幅螺旋偏移，避免完全重叠。
    var seen = {};
    var out = [];
    LEAD.forEach(function (l) {
      var c = l.country;
      if (!c) return;
      var e = LL[c];
      if (!e) return;
      if (out.length >= 30) return;
      var k = seen[c] || 0; seen[c] = k + 1;
      var dlat = 0, dlon = 0;
      if (k > 0) {
        var ang = k * 2.39996;              // 黄金角，散得均匀
        var rad = 5.0 + k * 3.2;            // 度
        dlat = Math.cos(ang) * rad;
        dlon = Math.sin(ang) * rad / Math.max(0.25, Math.cos(e[0] * Math.PI / 180));
      }
      out.push({
        lat: e[0] + dlat, lon: e[1] + dlon, n: 1,
        stage: l.stage || "S1", country: c,
        id: l.id, pot: Number(l.potential) || 0
      });
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
      var _bs = BATCH_INFO && BATCH_INFO.status;
    $("sfail").textContent = _bs === "no_sources"
      ? "本轮没有任何可用数据源：请在左侧「采集来源」启用数据源后重试"
      : (_bs === "no_network"
        ? "全部数据源不可用：请检查网络/代理后重试（页面数据为上次结果）"
        : "全部数据源采集失败：请检查网络或源配置后重试");
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
  var drw = $("drw"), dtog = $("dtog"), cenEl = $("cen");
  function syncDrwLayout() {
    if (!cenEl) return;
    // 折叠时抽屉变浮层（脱离布局），用左边距补回原宽度，地球局部尺寸不变
    cenEl.style.marginLeft = drw.classList.contains("coll")
      ? "calc(clamp(180px,13.5vw,240px) + 10px)" : "";
    dtog.title = drw.classList.contains("coll") ? "展开左侧筛选面板" : "收起左侧筛选面板";
  }
  if (dtog) {
    dtog.onclick = function () {
      drw.classList.toggle("coll");
      dtog.textContent = drw.classList.contains("coll") ? "›" : "‹";
      syncDrwLayout();
    };
    syncDrwLayout();
  }
  bindChips($("opTime"), "time");
  bindChips($("opStage"), "stage");
  bindChips($("opGrade"), "grade");

  // ===== 采集来源列表（左抽屉）+ 逐源 直连/代理 开关 =====
  var DOMESTIC = { "cls": 1, "eastmoney": 1, "ths": 1, "jin10": 1, "sina": 1,
                   "wscn-a-stock": 1, "wscn-rss": 1, "weibo-hot": 1,
                   "douyin-hot": 1, "baidu-hot": 1 };
  function renderSources() {
    var box = $("srcList");
    if (!box) return;
    if (!SOURCES || !SOURCES.length) { box.innerHTML = '<div class="srcrow dim">暂无来源数据</div>'; return; }
    box.innerHTML = SOURCES.map(function (s) {
      var cls = s.status === "ok" ? "ok" : (s.status === "never" ? "" : "err");
      var isProxy = s.proxy_mode === "proxy";
      var region = DOMESTIC[s.id] ? "国内" : "国际";
      return '<div class="srcrow ' + cls + '" data-sid="' + esc(s.id) + '" title="' +
        esc(s.name) + "（" + (s.status === "ok" ? "正常" : "异常") + "）" + '">' +
        '<span class="d"></span><span class="n">' + esc(s.name) + '</span>' +
        '<span class="rg">' + region + '</span>' +
        '<button class="pm' + (isProxy ? " on" : "") + '" data-pm="' +
        (isProxy ? "direct" : "proxy") + '" title="点击切换为' +
        (isProxy ? "直连" : "代理") + '">' + (isProxy ? "代理" : "直连") + "</button>" +
        (function () {
          // 信号强度：ok 且条数充足=4 格，ok=3，异常=1，未采=0
          var lv = s.status === "ok" ? ((s.last_count || 0) >= 20 ? 4 : 3)
                 : (s.status === "never" ? 0 : 1);
          var cls = s.status === "ok" ? "ok" : (s.status === "never" ? "" : "err");
          return '<span class="sig ' + cls + " s" + lv + '" title="信号 ' + lv +
            "/4 · 上次 " + (s.last_count || 0) + ' 条"><i></i><i></i><i></i><i></i></span>';
        })() + "</div>";
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll("button.pm"), function (b) {
      b.onclick = function (ev) {
        ev.stopPropagation();
        var row = b.closest(".srcrow");
        var sid = row.getAttribute("data-sid");
        var mode = b.getAttribute("data-pm");
        b.disabled = true;
        b.textContent = "…";
        fetch("/api/sources/" + encodeURIComponent(sid), {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ proxy_mode: mode })
        }).then(function (r) { return r.json(); }).then(function (j) {
          if (!j.ok) { toast("切换失败：" + (j.error || "未知")); }
          return fetchAll();
        }).catch(function () { b.disabled = false; toast("切换失败：网络错误"); });
      };
    });
  }

  // ===== AI 建议解析 =====
  function sugHTML(x) {
    var tt = String(x.title || "").replace(/\s+/g, " ").trim();
    return '<div class="sug"><div class="st">潜力 ' + (x.potential || 0) + " · " +
      esc(x.category || "未分类") + (x.country ? " · " + esc(x.country) : "") + "</div>" +
      '<div class="ti" title="' + esc(tt) + '"><a href="' + esc(x.url || "#") +
      '" target="_blank" rel="noopener">' + esc(tt) + "</a></div>" +
      '<div class="ac"><span class="dim">' + esc(x.source || "") + " · " + fmtTime(x.published_at) + "</span>" +
      '<button data-sug="' + x.id + '">AI解析</button></div></div>';
  }
  async function loadSuggestions() {
    var j = await api("/api/suggestions?limit=8").catch(function () { return null; });
    var items = (j && j.items) || [];
    var box = $("pSug");
    if (!box) return;
    box.innerHTML = items.length ? items.map(sugHTML).join("")
      : '<div class="dim">暂无建议：本轮粗筛通过的条目都已解析，或还没有新批次。</div>';
    $("cSug").textContent = items.length;
    Array.prototype.forEach.call(box.querySelectorAll("button[data-sug]"), function (b) {
      b.onclick = function (ev) { ev.stopPropagation(); openAnalyze(b.getAttribute("data-sug")); };
    });
  }

  // ===== AI 解析弹层 =====
  function modalShow(title) {
    $("mTitle").textContent = title || "AI 解析结果";
    $("mBody").innerHTML = '<div class="dim">解析中…（约 5–10 秒，会消耗少量模型费用）</div>';
    $("modal").classList.add("show");
  }
  function modalHide() { $("modal").classList.remove("show"); }
  function renderCard(c) {
    if (!c) return '<div class="dim">没有可展示的解析结果。</div>';
    var money = c.money_hypothesis || {}, na = c.next_action || {}, fe = c.feasibility || {};
    var fails = [];
    if (fe.needs_license) fails.push("需资质");
    if (fe.needs_capital) fails.push("需垫资");
    if (fe.needs_team) fails.push("需团队");
    if (fe.needs_identity) fails.push("需企业身份");
    var h = "";
    h += "<h4>事实</h4><div class=\"v\">" + esc(c.fact || "—") + "</div>";
    h += '<div class="g">阶段 ' + esc(c.stage || "—") +
      (c.anchor && c.anchor.vertical ? " · 行业 " + esc(c.anchor.vertical) : "") +
      " · 等级 " + (GRADE[c.confidence] || "C") + "</div>";
    h += "<h4>谁在缺什么</h4><div class=\"v\">" + esc(c.buyer_hypothesis || "—") + "</div>";
    h += "<h4>钱流</h4><div class=\"v\">" + esc(money.form || "—") + " · 付钱方 " +
      esc(money.payer || "—") + "</div><div class=\"g\">你赚：" + esc(money.your_cut || "—") +
      "（" + esc(money.status || "待验证") + "）</div>";
    h += "<h4>下一步</h4><div class=\"v\">" + esc(na.action || "—") + "</div><div class=\"g\">对象：" +
      esc(na.target || "—") + "</div><div class=\"g\">做法：" + esc(na.how || "—") + "</div>";
    if (c.verify_plan && c.verify_plan.length) {
      h += "<h4>验证计划</h4><ul style=\"padding-left:18px\">" +
        c.verify_plan.map(function (v) { return "<li>" + esc(v) + "</li>"; }).join("") + "</ul>";
    }
    h += "<h4>能力闸</h4><div class=\"v\">" + (fails.length ? "不通过：" + esc(fails.join("、"))
      : "通过（个人能力范围内）") + "</div><div class=\"g\">" + esc(fe.reason || "") + "</div>";
    return h;
  }
  async function openAnalyze(id) {
    if (!id) return;
    modalShow("AI 解析中…");
    try {
      var r = await fetch("/api/item/" + id + "/analyze", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
      });
      var j = await r.json();
      if (!j.ok) { $("mBody").innerHTML = '<div class="dim">解析失败：' + esc(j.error || ("HTTP " + r.status)) + "</div>"; return; }
      $("mTitle").textContent = j.cached ? "AI 解析结果（已缓存）" : "AI 解析结果";
      if (j.card) { $("mBody").innerHTML = renderCard(j.card); }
      else { $("mBody").innerHTML = '<div class="dim">这条没有可执行机会：' +
        esc((j.reasons || []).join("；") || "模型按规则认输") + "</div>"; }
      loadSuggestions();
    } catch (e) {
      $("mBody").innerHTML = '<div class="dim">解析失败：' + esc(e.message) + "</div>";
    }
  }
  $("mClose").onclick = modalHide;
  $("modal").addEventListener("click", function (e) { if (e.target === $("modal")) modalHide(); });

  // ===== 备忘录 / 收藏 弹层 =====
  var popTab = "notes";
  function popItem(r, isNotes) {
    return '<div class="pitem" data-id="' + r.item_id + '"><div class="t">' + esc(r.title) + "</div>" +
      (isNotes && r.content ? '<div class="n">' + esc(r.content) + "</div>" : "") +
      '<div class="m">' + esc(r.source || "") + " · " +
      (isNotes ? fmtTime(r.updated_at) : fmtTime(r.created_at)) + "</div></div>";
  }
  async function loadPop() {
    var body = $("popBody");
    body.innerHTML = '<div class="dim">加载中…</div>';
    var isNotes = popTab === "notes";
    var j = await api(isNotes ? "/api/notes?limit=50" : "/api/favorites?limit=50")
      .catch(function () { return null; });
    var rows = (j && (isNotes ? j.notes : j.favorites)) || [];
    body.innerHTML = rows.length
      ? rows.map(function (r) { return popItem(r, isNotes); }).join("")
      : '<div class="dim">' + (isNotes ? "还没有备注。打开任意线索卡详情即可写。"
        : "还没有收藏。在详情页点「☆ 收藏」。") + "</div>";
    Array.prototype.forEach.call(body.querySelectorAll(".pitem"), function (el) {
      el.onclick = function () { window.open("/static/detail.html?id=" + el.getAttribute("data-id"), "_blank"); };
    });
  }
  $("btnPop").onclick = function (e) {
    e.stopPropagation();
    var p = $("pop");
    p.classList.toggle("show");
    if (p.classList.contains("show")) loadPop();
  };
  $("popClose").onclick = function () { $("pop").classList.remove("show"); };
  Array.prototype.forEach.call(document.querySelectorAll(".ptab"), function (t) {
    t.onclick = function () {
      Array.prototype.forEach.call(document.querySelectorAll(".ptab"), function (x) { x.classList.remove("on"); });
      t.classList.add("on");
      popTab = t.getAttribute("data-t");
      loadPop();
    };
  });
  document.addEventListener("click", function (e) {
    var p = $("pop");
    if (p.classList.contains("show") && !p.contains(e.target) && e.target.id !== "btnPop") {
      p.classList.remove("show");
    }
  });

  // ===== 面板拖动换位（拖标题栏，位置记忆在 localStorage）=====
  function panelKey(p) { var b = p.querySelector(".pc"); return b ? b.id : ""; }
  function saveOrder() {
    var c = cols();
    var order = { L: [].map.call(c.L.children, panelKey), R: [].map.call(c.R.children, panelKey) };
    try { localStorage.setItem("hcn_panel_order", JSON.stringify(order)); } catch (e) {}
  }
  function restoreOrder() {
    var c = cols();
    try {
      var raw = JSON.parse(localStorage.getItem("hcn_panel_order") || "null");
      if (!raw || Array.isArray(raw)) return;      // 旧格式（全局顺序）不再套用
      [["L", c.L], ["R", c.R]].forEach(function (pair) {
        var keys = raw[pair[0]] || [];
        var col = pair[1], map = {};
        [].forEach.call(col.children, function (p) { var k = panelKey(p); if (k) map[k] = p; });
        keys.forEach(function (k) { if (map[k]) col.appendChild(map[k]); });
      });
    } catch (e) {}
  }
  function initDrag() {
    var grid = $("grid");
    if (!grid || grid.__drag) return;
    grid.__drag = true;
    var dragEl = null;
    var _c = cols();
    var allPanels = [].slice.call(_c.L.children).concat([].slice.call(_c.R.children));
    allPanels.forEach(function (p) {
      p.draggable = true;
      // mousedown 时记录是否按在标题栏（dragstart 的 event.target 恒为 panel 本身，
      // 不能靠 e.target.closest(".pt") 判断，否则拖动永远被 preventDefault 取消）
      p.addEventListener("mousedown", function (e) {
        p.__canDrag = !!e.target.closest(".pt");
      });
      p.addEventListener("dragstart", function (e) {
        // 只有按住标题栏才能拖，避免干扰卡片内点击/选择
        if (!p.__canDrag) { e.preventDefault(); return; }
        dragEl = p;
        var dz = $("drop3"); if (dz) dz.classList.add("on");
        p.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", panelKey(p)); } catch (_) {}
      });
      p.addEventListener("dragend", function () {
        p.classList.remove("dragging");
        dragEl = null;
        var dz = $("drop3");
        if (dz) { dz.classList.remove("on"); dz.classList.remove("hot"); }
        collapseCol3IfEmpty();
        saveOrder();
      });
      p.addEventListener("dragover", function (e) {
        if (!dragEl || dragEl === p) return;
        e.preventDefault();
        p.classList.add("target");
      });
      p.addEventListener("dragleave", function () { p.classList.remove("target"); });
      p.addEventListener("drop", function (e) {
        e.preventDefault();
        p.classList.remove("target");
        if (!dragEl || dragEl === p) return;
        var parent = p.parentNode;                 // 允许跨列：拖到哪列就进哪列
        if (!parent) return;
        var kids = [].slice.call(parent.children);
        var i = kids.indexOf(dragEl), j = kids.indexOf(p);
        if (j < 0) return;
        if (i >= 0 && i < j) parent.insertBefore(dragEl, p.nextSibling);
        else parent.insertBefore(dragEl, p);
        // 换列后重置权重为 1，并重新分配两列
        dragEl.__w = 1; dragEl.__w0 = 1; dragEl.style.flexGrow = 1;
        saveSize();
        colMapSave();
        dragEl = null;
        saveOrder();
      });
    });
    // 页面最右侧的投放条：拖进来就开第三列
    var dz = $("drop3");
    if (dz) {
      dz.addEventListener("dragover", function (e) {
        if (!dragEl) return;
        e.preventDefault();
        dz.classList.add("hot");
      });
      dz.addEventListener("dragleave", function () { dz.classList.remove("hot"); });
      dz.addEventListener("drop", function (e) {
        e.preventDefault();
        dz.classList.remove("hot");
        if (!dragEl) return;
        var c3 = ensureCol3(true);
        c3.appendChild(dragEl);
        dragEl.__w = 1; dragEl.__w0 = 1;
        dragEl.style.flexGrow = 1;
        dragEl.classList.remove("dragging");
        dragEl = null;
        saveSize();
        colMapSave();
      });
    }
    restoreOrder();
  }
  initDrag();
  // ===== 两列独立布局：列内按权重分配高度（拉大只挤同列下方，挤到 0 自动移除）=====
  var LAYOUT_V = "hcn_layout_v3";
  function cols() {
    var g = $("grid");
    if (!g) return null;
    if (!g.querySelector(".colL")) {
      var panels = Array.prototype.slice.call(g.children);
      var L = document.createElement("div"), R = document.createElement("div");
      L.className = "colL"; R.className = "colR";
      g.innerHTML = ""; g.appendChild(L); g.appendChild(R);
      var savedCols = {};
      try { savedCols = JSON.parse(localStorage.getItem("hcn_colmap") || "{}") || {}; } catch (e) {}
      var need3 = Object.keys(savedCols).some(function (k) { return savedCols[k] === "3"; });
      var C3 = null;
      if (need3) {
        C3 = document.createElement("div");
        C3.className = "col3";
        g.appendChild(C3);
        g.classList.add("three");
      }
      panels.forEach(function (p, i) {
        var k = panelKey(p);
        var side = savedCols[k] || (i % 2 === 0 ? "L" : "R");
        if (side === "3" && C3) C3.appendChild(p);
        else (side === "R" ? R : L).appendChild(p);
      });
    }
    return { g: g, L: g.querySelector(".colL"), R: g.querySelector(".colR"),
             C3: g.querySelector(".col3") };
  }
  // 窄屏三列宽度缓存：key=window.innerWidth 取整，value=同宽度下两列模式的 #cen 宽度。
  // 跨断点往返后公式实时计算会与初始两列不一致（1440 下 508→469），故优先用缓存锁定。
  var CEN2COL = {};
  window.__CEN2COL = CEN2COL;   // 诊断钩子：活引用，供量测脚本读取缓存命中情况
  function recordCen2Col() {
    var g = $("grid");
    if (!g || g.classList.contains("three")) return;   // 仅两列模式记录
    var cen = $("cen"); if (!cen) return;
    var w = Math.round(cen.getBoundingClientRect().width);
    if (w > 0) CEN2COL[Math.round(window.innerWidth)] = w;
  }
  // 三列时锁住地球区域宽度（地球不变形，画板盖在上面）
  function lockCen(on) {
    var cen = $("cen"), main = document.querySelector("main"), aside = document.querySelector("aside");
    if (!cen || !main) return;
    if (on) {
      var iw = Math.round(window.innerWidth);
      var cached = CEN2COL[iw];
      if (!cached) {                                   // 精确键缺失：取最近视口宽度的缓存
        var bestD = 101;                               // （跨断点往返后 innerWidth 有 ±16 级微差）
        for (var k in CEN2COL) {
          var d = Math.abs(Number(k) - iw);
          if (d < bestD) { bestD = d; cached = CEN2COL[k]; }
        }
      }
      var w;
      if (cached) {
        w = cached;                                    // 优先：同宽度两列时的实测宽度
      } else {
        var asideW = aside ? aside.offsetWidth : 0;
        var gridW = Math.min(1060, Math.max(560, window.innerWidth * 0.48));  // 两列时网格宽度
        w = Math.max(320, main.clientWidth - 20 - asideW - gridW - 20);       // 兜底公式
      }
      cen.style.flex = "0 0 auto";
      cen.style.width = w + "px";
    } else {
      cen.style.flex = "";
      cen.style.width = "";
    }
  }
  // 响应式三列断点：≥1800px 宽屏走真实 flex 布局（CSS 媒体查询已取消 absolute），<1800px 保持覆盖
  var WIDE = 1800;
  function isWide() { return (window.innerWidth || document.documentElement.clientWidth) >= WIDE; }
  var _applyingThree = false;
  function applyThreeLayout() {
    var g = $("grid"); if (!g) return;
    var three = g.classList.contains("three");
    if (three && !isWide()) {
      lockCen(true);                 // 窄屏三列：写死 #cen 宽度，画板 absolute 覆盖
    } else {
      lockCen(false);                // 宽屏三列 / 任意两列：#cen flex:1 自适应
      var cen = $("cen");
      if (cen) { cen.style.width = ""; cen.style.marginLeft = ""; cen.style.flex = ""; }
    }
    if (!_applyingThree) {           // 派发 resize 让地球按新容器尺寸重算（防重入）
      _applyingThree = true;
      try { window.dispatchEvent(new Event("resize")); } catch (e) {}
      _applyingThree = false;
    }
  }
  window.addEventListener("resize", function () {
    var g = $("grid");
    if (g && g.classList.contains("three") && !isWide()) lockCen(true);  // 仅窄屏三列需重算锁宽
    else recordCen2Col();                                                 // 两列尺寸变化时刷新缓存
  });
  // 跨断点自动切换：只在宽/窄状态变化时执行，避免每帧重排
  var lastWide = isWide();
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      var w = isWide();
      if (w !== lastWide) { lastWide = w; applyThreeLayout(); }
      recordCen2Col();                 // 两列状态下记录 #cen 宽度（含首次 observe 回调）
    }).observe(document.documentElement);
  } else {
    window.addEventListener("resize", function () {
      var w = isWide();
      if (w !== lastWide) { lastWide = w; applyThreeLayout(); }
      recordCen2Col();
    });
  }

  // 开启 / 收起第三列（force=true 时无条件收起）
  function ensureCol3(on, force) {
    var g = $("grid");
    var c3 = g.querySelector(".col3");
    if (on && !c3) {
      c3 = document.createElement("div");
      c3.className = "col3";
      g.appendChild(c3);
      g.classList.add("three");
      applyThreeLayout();         // 宽屏真实占位 / 窄屏覆盖锁宽
      syncCol3Btn();
      return c3;
    }
    if (!on && c3) {
      var vis = Array.prototype.slice.call(c3.children).filter(function (p) {
        return !p.classList.contains("removed");
      });
      if (force === true || !vis.length) {   // force 无条件收；否则仅无可见面板时收
        var L = g.querySelector(".colL");
        Array.prototype.slice.call(c3.children).forEach(function (p) {
          L.appendChild(p);       // 全部子面板（含 removed）按 DOM 顺序搬回左列，避免丢
          p.__w = 1; p.__w0 = 1; p.style.flexGrow = 1; p.style.flexBasis = "0";
        });
        g.removeChild(c3);
        g.classList.remove("three");
        applyThreeLayout();       // 复原两列：清 #cen 内联宽度/左边距 + 重算地球
        recordCen2Col();          // 退出三列后立即刷新两列宽度缓存
        saveSize();
        colMapSave();
        syncCol3Btn();
        return null;
      }
    }
    return c3;
  }
  function collapseCol3IfEmpty(force) {
    var g = $("grid");
    var c3 = g.querySelector(".col3");
    if (!c3) return;
    if (force === true) {          // 强制收起：不再判断可见性，直接走 ensureCol3
      ensureCol3(false, true);
      syncCol3Btn();
      return;
    }
    var vis = Array.prototype.slice.call(c3.children).filter(function (p) {
      return !p.classList.contains("removed");
    });
    if (vis.length) return;        // 还有可见面板 → 不收
    ensureCol3(false);
    syncCol3Btn();
  }
  function syncCol3Btn() {
    var g = $("grid"), b = $("btnCol3Close");
    if (!g || !b) return;
    b.style.display = g.classList.contains("three") ? "block" : "none";
  }
  function colMapSave() {
    var c = cols(); var out = {};
    [c.L, c.R, c.C3].forEach(function (col, idx) {
      if (!col) return;
      Array.prototype.slice.call(col.children).forEach(function (p) {
        var k = panelKey(p); if (k) out[k] = idx === 0 ? "L" : (idx === 1 ? "R" : "3");
      });
    });
    try { localStorage.setItem("hcn_colmap", JSON.stringify(out)); } catch (e) {}
  }
  function saveSize() {
    var c = cols(); var out = {};
    [c.L, c.R, c.C3].forEach(function (col) {
      if (!col) return;
      Array.prototype.slice.call(col.children).forEach(function (p) {
        var k = panelKey(p); if (!k) return;
        out[k] = { w: +(p.__w || 1).toFixed(2) };
      });
    });
    try {
      localStorage.setItem("hcn_panel_size", JSON.stringify(out));
      localStorage.setItem(LAYOUT_V, "1");
    } catch (e) {}
    colMapSave();
  }
  function restoreSize() {
    var c = cols();
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem("hcn_panel_size") || "{}") || {}; } catch (e) { saved = {}; }
    var colsAll = [c.L, c.R, c.C3].filter(Boolean);
    colsAll.forEach(function (col) {
      Array.prototype.slice.call(col.children).forEach(function (p) {
        var k = panelKey(p); if (!k) return;
        var v = saved[k], w = 1;
        if (v && typeof v.w === "number") w = v.w;
        else if (v && v.length === 2) w = Math.max(0.5, Math.min(6, +v[1] || 1));
        p.__w = w;
        p.style.flexGrow = w;
        p.style.flexBasis = "0";
      });
      var sum = Array.prototype.slice.call(col.children).reduce(function (x, y) {
        return x + (y.__w || 1);
      }, 0);
      col.__total = sum || 1;
    });
  }
  // 某列面板明显少于对面列时，从对面列末尾搬一块过来（填满空位）
  function autoFill(col) {
    var c = cols();
    if (!c) return;
    var other = col === c.L ? c.R : c.L;
    var mine = Array.prototype.slice.call(col.children).filter(function (x) {
      return !x.classList.contains("removed");
    });
    var theirs = Array.prototype.slice.call(other.children).filter(function (x) {
      return !x.classList.contains("removed");
    });
    if (theirs.length - mine.length < 2 || !theirs.length) return;
    var move = theirs[theirs.length - 1];
    col.appendChild(move);
    move.__w = 1; move.__w0 = 1;
    move.style.flexGrow = 1;
    move.classList.remove("resizing");
  }

  function initResize() {
    var c = cols();
    if (!c || c.g.__rsz) return;
    c.g.__rsz = true;
    [c.L, c.R].forEach(function (col) {
      Array.prototype.slice.call(col.children).forEach(function (p) {
        if (p.querySelector(".rh")) return;
        var rh = document.createElement("span");
        rh.className = "rh";
        rh.title = "上下拖动调整高度：只挤压同列下方的面板，挤到没有高度会自动移除";
        p.appendChild(rh);
        rh.addEventListener("mousedown", function (e) {
          e.preventDefault(); e.stopPropagation();
          p.__canDrag = false;
          var items = Array.prototype.slice.call(col.children);
          var pos = items.indexOf(p);
          var followers = items.slice(pos + 1);
          var colH = col.getBoundingClientRect().height - 10 * Math.max(0, items.length - 1);
          var total = items.reduce(function (x, y) { return x + (y.__w || 1); }, 0) || 1;
          var startW = p.__w || 1;
          var fStart = followers.map(function (x) { return x.__w || 1; });
          var startY = e.clientY;
          var MINW = 0.3;
          var cap = startW + fStart.reduce(function (x, y) { return x + Math.max(0, y - MINW); }, 0);
          p.classList.add("resizing");
          function onMove(ev) {
            var dPx = ev.clientY - startY;
            var dW = (dPx / (colH || 1)) * total;
            var w = Math.max(MINW, Math.min(cap, startW + dW));
            p.__w = w;
            var need = w - startW;
            followers.forEach(function (x, i) {
              var avail = Math.max(0, fStart[i] - MINW);
              var take = Math.max(0, Math.min(avail, need));
              x.__w = fStart[i] - take;
              need -= take;
            });
            items.forEach(function (x) { x.style.flexGrow = x.__w || 1; });
          }
          function onUp() {
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
            p.classList.remove("resizing");
            followers.forEach(function (x) {
              if ((x.__w || 1) <= MINW + 0.01 && window.__panelHide) window.__panelHide(x);
            });
            if ((p.__w || 1) < startW - 0.05) autoFill(col);   // 只有缩小腾出空间时才补位
            saveSize();
          }
          document.addEventListener("mousemove", onMove);
          document.addEventListener("mouseup", onUp);
        });
      });
    });
  }
  restoreSize();
  initResize();


  // ===== 面板移除 / 恢复（× 按钮；重置或「恢复显示」可还原）=====
  (function initPanelRemove() {
    var grid = $("grid");
    if (!grid) return;
    var HID = {};
    try { HID = JSON.parse(localStorage.getItem("hcn_panel_hidden") || "{}") || {}; } catch (e) { HID = {}; }
    function save() {
      try { localStorage.setItem("hcn_panel_hidden", JSON.stringify(HID)); } catch (e) {}
      updateHint();
    }
    var TITLE = {};   // pc id → 面板标题
    function titlesOf(p) {
      var span = p.querySelector(".pt > span");
      return span ? span.textContent.trim() : (p.querySelector(".pc") || {}).id || "面板";
    }
    // 供拉伸逻辑调用：某块被挤到没有高度 → 自动移除
    window.__panelHide = function (panel) {
      var pc = panel && panel.querySelector(".pc");
      if (!pc) return;
      panel.classList.add("removed");
      HID[pc.id] = 1;
      save();
      // 隐藏生效后再尝试收起第三列（若第三列已无可见面板）
      setTimeout(function () { collapseCol3IfEmpty(); }, 0);
    };
    function updateHint() {
      var keys = Object.keys(HID).filter(function (k) { return HID[k]; });
      // 只在右下角浮层列出被移除面板的名字，点名字单独恢复
      var rb = $("removedBox"), rl = $("rbList");
      if (!rb || !rl) return;
      rb.classList.toggle("show", keys.length > 0);
      rl.innerHTML = keys.map(function (k) {
        return '<button class="rb-i" data-k="' + k + '">' + (TITLE[k] || k) + "</button>";
      }).join("");
      Array.prototype.forEach.call(rl.querySelectorAll(".rb-i"), function (b) {
        b.onclick = function () {
          var k = b.getAttribute("data-k");
          delete HID[k];
          var target = null;
          Array.prototype.forEach.call(grid.querySelectorAll(".panel"), function (p) {
            var pc = p.querySelector(".pc");
            if (pc && pc.id === k) target = p;
          });
          if (target) {
            target.classList.remove("removed");
            target.__w = 1;                    // 恢复默认高度权重
            target.style.flexGrow = 1;
            target.style.flexBasis = "0";
          }
          save();
          if (typeof saveSize === "function") saveSize();
        };
      });
    }
    Array.prototype.forEach.call(grid.querySelectorAll(".panel"), function (p) {
      var pc = p.querySelector(".pc");
      var key = pc && pc.id;
      if (!key) return;
      var head = p.querySelector(".pt");
      if (!head) return;
      TITLE[key] = titlesOf(p);
      var b = document.createElement("button");
      b.className = "xrm";
      b.title = "移除该面板（可用左侧「恢复显示」或「重置面板布局」还原）";
      b.textContent = "×";
      b.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        p.classList.add("removed");
        HID[key] = 1;
        save();
      });
      head.appendChild(b);
      if (HID[key]) p.classList.add("removed");
    });
    // 加载后自愈：第三列若只剩隐藏面板，自动收回两列
    setTimeout(function () { collapseCol3IfEmpty(); syncCol3Btn(); }, 60);
    // 兜底：任何把第三列变空的操作（× 移除、拖走、程序化 removed 类等）都自动收起
    var col3G = $("grid");
    if (col3G && typeof MutationObserver !== "undefined" && !window.__col3Observer) {
      var moCfg = { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] };
      var col3Timer = null;
      function checkCol3Empty() {
        var c3 = col3G.querySelector(".col3");
        if (!c3) return;
        var visCount = Array.prototype.filter.call(c3.children, function (p) {
          return !p.classList.contains("removed");
        }).length;
        if (visCount) return;                       // 还有可见面板 → 不收
        mo.disconnect();                             // 收起会改 DOM，先断开避免自循环
        try { collapseCol3IfEmpty(); } finally { mo.observe(col3G, moCfg); }
      }
      var mo = new MutationObserver(function () {
        clearTimeout(col3Timer);
        col3Timer = setTimeout(checkCol3Empty, 50);  // 50ms 防抖
      });
      mo.observe(col3G, moCfg);
      window.__col3Observer = mo;
    }
    updateHint();
  })();
  // 夜间模式切换（状态在 earth.js，这里只做按钮与文案）
  var nightBtn = $("btnNight");
  function syncNightLabel() {
    if (!nightBtn || !window.EARTH || !window.EARTH.night) return;
    var on = window.EARTH.night() === 1;
    nightBtn.textContent = on ? "白天" : "夜间";
    nightBtn.classList.toggle("on", on);
  }
  if (nightBtn) {
    nightBtn.onclick = function () {
      if (!window.EARTH || !window.EARTH.setNight) return;
      window.EARTH.setNight(window.EARTH.night() ? 0 : 1);
      syncNightLabel();
    };
    setTimeout(syncNightLabel, 300);
  }

  // 出口网络徽标：只显示，点它去设置页切换采集模式
  (function initNetBadge() {
    var el = $("netBadge");
    if (!el) return;
    function load(force) {
      fetch("/api/network" + (force ? "?refresh=1" : ""))
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) { el.textContent = "出口 未知"; el.title = j.error || ""; return; }
          var where = j.is_cn ? "境内" : (j.country || "境外");
          el.textContent = "出口 " + where + " · " + (j.ip || "?");
          el.title = "出口 IP " + (j.ip || "?") + "\n地区 " + (j.region || "-") +
                     "\n检测于 " + (j.checked_at || "-") + "\n点击到设置页切换采集模式";
        })
        .catch(function () { el.textContent = "出口 未知"; });
    }
    el.onclick = function () { location.href = "/static/settings.html"; };
    load(false);
    setInterval(function () { load(true); }, 10 * 60 * 1000);   // 每 10 分钟刷新
  })();

  var col3Btn = $("btnCol3Close");
  if (col3Btn) {
    col3Btn.onclick = function () {
      collapseCol3IfEmpty(true);   // 强制收起第三侧：面板全部搬回左列
      saveSize();
    };
  }
  setTimeout(syncCol3Btn, 300);
  applyThreeLayout();   // 初始化：按当前断点确定三列布局模式

  var resetBtn = $("resetLayout");
  if (resetBtn) {
    resetBtn.onclick = function () {
      try { localStorage.removeItem("hcn_panel_order"); } catch (e) {}
      try { localStorage.removeItem("hcn_panel_size"); } catch (e) {}
      try { localStorage.removeItem("hcn_panel_hidden"); } catch (e) {}
      try { localStorage.removeItem("hcn_colmap"); } catch (e) {}
      try { localStorage.removeItem("hcn_layout_v3"); } catch (e) {}
      location.reload();
    };
  }

  var _render = render;
  render = function () { _render(); renderSources(); };
  loadSuggestions();

  // 深链：?panel=notes|fav 打开弹层；?analyze=<id> 直接解析该条
  (function () {
    var q = new URLSearchParams(location.search);
    var panel = q.get("panel");
    if (panel) {
      $("pop").classList.add("show");
      var tab = panel === "fav" ? "fav" : "notes";
      Array.prototype.forEach.call(document.querySelectorAll(".ptab"), function (t) {
        t.classList.toggle("on", t.getAttribute("data-t") === tab);
      });
      popTab = tab;
      loadPop();
    }
    var aid = q.get("analyze");
    if (aid) openAnalyze(aid);
  })();

  fetchAll();
  setInterval(fetchAll, 60000);
  setInterval(loadSuggestions, 60000);
})();
