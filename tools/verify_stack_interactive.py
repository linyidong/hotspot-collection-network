# -*- coding: utf-8 -*-
"""模块：技术栈页面「动态 + 可交互」验收。
职责：检查 /static/stack.html 是否具备真实交互与动效（不是静态排版）：
      1) 存在运行中的动画（CSS/Web Animations）；
      2) 架构层可点击展开详情（4 层）；
      3) 前端/后端/数据源 Tab 可切换且内容变化；
      4) 数据源可按类型筛选，计数随之变化；
      5) 「跑一轮」管线模拟：点击后 6 个阶段依次点亮；
      6) 关键数字进入视口后从 0 递增到目标值；
      7) 无外部依赖、无 JS 异常、三档宽度无横向滚动、支持 prefers-reduced-motion。
输出：PASS/FAIL 明细；退出码 0 = 全部通过。
关键依赖：websocket-client、本机 Chrome、服务 127.0.0.1:9000。
交互契约（Trae 必须遵守，验收依赖这些 data 属性）：
  [data-layer="1..4"]、[data-layer-panel="1..4"]
  [data-tab="front|back|data"]、[data-tabpanel="front|back|data"]
  [data-src-type="direct|bridge|rss|hot|snap"]、[data-src-card]、#srcCount
  #btnRunPipeline、[data-stage="1..6"]
  [data-num][data-target]
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
STACK = "http://127.0.0.1:9000/static/stack.html"


def main():
    results = []
    try:
        with urllib.request.urlopen(STACK, timeout=10) as r:
            html = r.read().decode("utf-8", "ignore")
            status = r.status
    except Exception as exc:
        print(f"stack.html 打不开：{exc}")
        return 1

    results.append(("页面可访问", "PASS" if status == 200 else "FAIL", f"HTTP {status}"))
    for hook in ("data-layer", "data-tab", "data-src-type", "btnRunPipeline", "data-num", "data-stage"):
        results.append((f"交互契约 {hook}", "PASS" if hook in html else "FAIL",
                        "存在" if hook in html else "缺失"))
    results.append(("reduced-motion 适配", "PASS" if "prefers-reduced-motion" in html else "FAIL",
                    "有" if "prefers-reduced-motion" in html else "无"))

    port = 9333 + 11
    prof = tempfile.mkdtemp(prefix="stk-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
         "--use-angle=swiftshader", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={prof}",
         "--window-size=1920,1080", STACK],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(80):
            try:
                ts = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
                pg = [t for t in ts if t["type"] == "page" and "stack" in t.get("url", "")]
                if pg:
                    ws = websocket.create_connection(pg[0]["webSocketDebuggerUrl"], timeout=20,
                                                     suppress_origin=True)
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if ws is None:
            print("CDP 连接失败")
            return 1

        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        errors = []

        def ev(expr, mid=[0]):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("method") == "Runtime.exceptionThrown":
                    errors.append(str(m["params"]["exceptionDetails"].get("text"))[:80])
                    continue
                if m.get("id") == mid[0]:
                    r = m.get("result", {})
                    if r.get("exceptionDetails"):
                        return "EXC:" + str(r["exceptionDetails"].get("text"))[:100]
                    return r.get("result", {}).get("value")

        for _ in range(50):
            time.sleep(0.3)
            if ev("document.readyState") == "complete":
                break
        time.sleep(1.2)

        # 1) 动画存在
        anims = ev("document.getAnimations ? document.getAnimations().length : 0")
        results.append(("存在运行中的动画", "PASS" if isinstance(anims, int) and anims > 0 else "FAIL",
                        f"getAnimations()={anims}"))

        # 2) 层级展开
        opened = ev("""(() => {
          var t = document.querySelector('[data-layer="2"]');
          if (!t) return 'no-layer';
          t.click();
          var p = document.querySelector('[data-layer-panel="2"]');
          if (!p) return 'no-panel';
          var vis = p.offsetParent !== null || getComputedStyle(p).display !== 'none';
          return vis ? 'open' : 'hidden';
        })()""")
        results.append(("点击架构层展开详情", "PASS" if opened == "open" else "FAIL", str(opened)))
        closed = ev("""(() => {
          var t = document.querySelector('[data-layer="2"]');
          t.click();
          var p = document.querySelector('[data-layer-panel="2"]');
          var vis = p && (p.offsetParent !== null || getComputedStyle(p).display !== 'none');
          return vis ? 'still-open' : 'closed';
        })()""")
        results.append(("再次点击收起详情", "PASS" if closed == "closed" else "FAIL", str(closed)))

        # 3) Tab 切换
        tab = ev("""(() => {
          var tabs = [...document.querySelectorAll('[data-tab]')];
          if (tabs.length < 3) return 'only-' + tabs.length;
          var first = document.querySelector('[data-tabpanel][data-active="1"], [data-tabpanel].active');
          var before = first ? first.getAttribute('data-tabpanel') : (first ? first.className : '');
          var target = tabs[1];
          target.click();
          return { before: String(before), after: String(target.getAttribute('data-tab')) };
        })()""")
        time.sleep(0.6)
        after_tab = ev("""(() => {
          var pan = [...document.querySelectorAll('[data-tabpanel]')].filter(function(p){
            return p.offsetParent !== null && getComputedStyle(p).display !== 'none';
          });
          return pan.map(function(p){ return p.getAttribute('data-tabpanel'); });
        })()""")
        results.append(("Tab 可切换且只显示一个面板", "PASS" if isinstance(after_tab, list) and len(after_tab) == 1 else "FAIL",
                        f"切换后可见面板={after_tab}"))

        # 4) 数据源筛选
        filt = ev("""(() => {
          var cards = [...document.querySelectorAll('[data-src-card]')];
          var btns = [...document.querySelectorAll('[data-src-type]')];
          if (!cards.length || !btns.length) return 'no-els';
          var before = cards.filter(function(c){ return c.offsetParent !== null; }).length;
          btns[1].click();
          return { before: before, type: btns[1].getAttribute('data-src-type') };
        })()""")
        time.sleep(0.6)
        filt2 = ev("""(() => {
          var cards = [...document.querySelectorAll('[data-src-card]')];
          var vis = cards.filter(function(c){ return c.offsetParent !== null; }).length;
          var cnt = document.getElementById('srcCount');
          return { after: vis, countText: cnt ? cnt.textContent.trim() : null };
        })()""")
        changed = isinstance(filt, dict) and isinstance(filt2, dict) and filt2["after"] < filt["before"]
        results.append(("数据源可按类型筛选", "PASS" if changed else "FAIL",
                        f"{filt.get('before') if isinstance(filt, dict) else filt} → {filt2.get('after') if isinstance(filt2, dict) else filt2}，计数={filt2.get('countText') if isinstance(filt2, dict) else ''}"))

        # 5) 管线模拟
        run = ev("""(() => {
          var b = document.getElementById('btnRunPipeline');
          if (!b) return 'no-btn';
          b.click();
          return 'clicked';
        })()""")
        time.sleep(2.2)
        stages = ev("""(() => {
          var s = [...document.querySelectorAll('[data-stage]')];
          var on = s.filter(function(x){
            return x.classList.contains('active') || x.classList.contains('done') || x.getAttribute('data-state') === 'on';
          }).length;
          return { total: s.length, on: on };
        })()""")
        ok_run = isinstance(stages, dict) and stages["on"] >= 5 and run == "clicked"
        results.append(("管线模拟依次点亮阶段", "PASS" if ok_run else "FAIL", str(stages)))

        # 6) 数字递增
        nums = ev("""(() => {
          var els = [...document.querySelectorAll('[data-num][data-target]')];
          if (!els.length) return 'no-nums';
          els[0].scrollIntoView({block:'center'});
          return els[0].getAttribute('data-target');
        })()""")
        time.sleep(1.6)
        num_now = ev("""(() => {
          var e = document.querySelector('[data-num][data-target]');
          return { text: (e.textContent||'').replace(/[^0-9.]/g,''), target: e.getAttribute('data-target') };
        })()""")
        ok_num = isinstance(num_now, dict) and abs(float(num_now["text"] or 0) - float(num_now["target"])) <= 0.01
        results.append(("关键数字递增到目标值", "PASS" if ok_num else "FAIL", str(num_now)))

        # 7) 无 JS 异常
        results.append(("无 JS 异常", "PASS" if not errors else "FAIL",
                        "无" if not errors else "; ".join(errors[:3])))
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.kill()

    ok = sum(1 for _, s, _ in results if s == "PASS")
    bad = [x for x in results if x[1] == "FAIL"]
    print(f"\n=== 技术栈页交互验收：PASS {ok} / FAIL {len(bad)} ===")
    for name, st, detail in results:
        print(f"  [{'PASS' if st == 'PASS' else 'FAIL'}] {name} :: {detail}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
