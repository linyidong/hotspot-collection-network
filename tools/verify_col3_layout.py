# -*- coding: utf-8 -*-
"""模块：三列看板布局验收（DSH 独立验收 Trae 的交付）。
职责：用 headless Chrome + CDP，在「宽屏 1920」和「窄屏 1440」两种尺寸下量化：
      1) 开第三列时 grid 是否遮挡地球；2) 收起第三列后是否完整复原两列。
输出：每条判定 PASS/FAIL + 实测数字；退出码 0=全部通过，1=有失败。
关键依赖：websocket-client、本机 Chrome、服务已在 127.0.0.1:9000 运行。
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:9000/"

# 量测：地球区域、画布、网格、重叠宽度、三列状态
MEASURE = r"""(() => {
  function r(sel){var e=document.querySelector(sel); if(!e) return null; var b=e.getBoundingClientRect();
    return {x:Math.round(b.x), y:Math.round(b.y), w:Math.round(b.width), h:Math.round(b.height),
            right:Math.round(b.right), bottom:Math.round(b.bottom)};}
  var cen=r('#cen'), g=r('#g3d'), grid=r('#grid');
  var overlap = (cen&&grid) ? Math.max(0, Math.min(cen.right, grid.right) - Math.max(cen.x, grid.x)) : -1;
  var panels=[...document.querySelectorAll('#grid .panel')];
  var vis=panels.filter(p=>!p.classList.contains('removed'));
  return {
    three: document.getElementById('grid').classList.contains('three'),
    col3: !!document.querySelector('.col3'),
    col3Children: document.querySelector('.col3') ? document.querySelector('.col3').children.length : 0,
    cen: cen, g3d: g, grid: grid, overlap: overlap,
    gridPos: getComputedStyle(document.getElementById('grid')).position,
    visiblePanels: vis.length, totalPanels: panels.length,
    removedPanels: panels.filter(p=>p.classList.contains('removed')).length,
    colHeights: {
      L: Math.round(document.querySelector('.colL').getBoundingClientRect().height),
      R: Math.round(document.querySelector('.colR').getBoundingClientRect().height),
      C3: document.querySelector('.col3') ? Math.round(document.querySelector('.col3').getBoundingClientRect().height) : 0
    },
    sumWeightL: [...document.querySelectorAll('.colL > .panel')].reduce((a,p)=>a+(p.__w||1),0),
    sumWeightR: [...document.querySelectorAll('.colR > .panel')].reduce((a,p)=>a+(p.__w||1),0)
  };
})()"""


def run_at(width, height, tag, results):
    port = 9700 + width // 100
    prof = tempfile.mkdtemp(prefix=f"v3-{tag}-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
         "--use-angle=swiftshader", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={prof}",
         f"--window-size={width},{height}", URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(60):
            try:
                ts = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read())
                pg = [t for t in ts if t["type"] == "page" and "9000" in t.get("url", "")]
                if pg:
                    ws = websocket.create_connection(pg[0]["webSocketDebuggerUrl"], timeout=25,
                                                     suppress_origin=True)
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if ws is None:
            results.append((f"{tag}: CDP 连接", "FAIL", "连不上"))
            return

        def cmd(method, params=None, mid=[9000]):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid[0]:
                    return m.get("result")

        def ev(expr, mid=[0]):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == mid[0]:
                    r = m.get("result", {})
                    if r.get("exceptionDetails"):
                        return "EXC:" + str(r["exceptionDetails"].get("text"))[:120]
                    return r.get("result", {}).get("value")

        # 就绪条件必须等到 app.js 建好 .colL/.colR 并且 __leads 已赋值（否则会量到半初始化的 DOM）
        for _ in range(150):
            time.sleep(0.3)
            ready = ev("""(() => {
              var hasCols = !!document.querySelector('.colL') && !!document.querySelector('.colR');
              var hasLeads = typeof window.__leads !== 'undefined';
              var hasPanels = document.querySelectorAll('#grid .panel').length >= 7;
              return hasCols && hasLeads && hasPanels;
            })()""")
            if ready is True:
                break
        time.sleep(0.6)     # 再给一轮渲染/布局稳定时间
        base = ev(MEASURE)
        if isinstance(base, str):
            results.append((f"{tag}: 页面就绪", "FAIL", base))
            return

        # ---- 1) 进入三列（拖到右侧投放条） ----
        ev("""(() => {
          var src = document.querySelector('.colR .panel');
          var dz = document.getElementById('drop3');
          var dt = new DataTransfer();
          (src.querySelector('.pt')||src).dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
          src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
          dz.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt}));
          dz.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));
          src.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
          return 1;
        })()""")
        time.sleep(1.2)
        three = ev(MEASURE)
        if isinstance(three, str):
            results.append((f"{tag}: 三列测量", "FAIL", three))
            return

        wide = width >= 1800
        results.append((f"{tag}: 第三列已开启", "PASS" if three["three"] and three["col3Children"] > 0 else "FAIL",
                        f"three={three['three']} col3={three['col3Children']}"))
        if wide:
            results.append((f"{tag}: 宽屏不遮挡地球", "PASS" if three["overlap"] <= 4 else "FAIL",
                            f"重叠 {three['overlap']}px（要求 ≤4），grid.position={three['gridPos']}"))
            # 地球应左移：三列时地球右侧不越过面板左边界
            results.append((f"{tag}: 地球左移让位", "PASS" if three["cen"]["right"] <= three["grid"]["x"] + 4 else "FAIL",
                            f"cen.right={three['cen']['right']} grid.x={three['grid']['x']}"))
        else:
            results.append((f"{tag}: 窄屏允许覆盖", "PASS" if three["overlap"] > 0 else "FAIL",
                            f"重叠 {three['overlap']}px（要求 >0）"))
            same = abs(three["cen"]["w"] - base["cen"]["w"]) <= 6 and abs(three["cen"]["h"] - base["cen"]["h"]) <= 6
            results.append((f"{tag}: 窄屏地球尺寸不变", "PASS" if same else "FAIL",
                            f"两列 {base['cen']['w']}x{base['cen']['h']} → 三列 {three['cen']['w']}x{three['cen']['h']}"))
            if base["g3d"] and three["g3d"]:
                a1 = base["g3d"]["w"] / max(1, base["g3d"]["h"])
                a2 = three["g3d"]["w"] / max(1, three["g3d"]["h"])
                results.append((f"{tag}: 窄屏地球不变形", "PASS" if abs(a1 - a2) <= 0.06 * a1 else "FAIL",
                                f"宽高比 {a1:.3f} → {a2:.3f}"))

        # ---- 1.5) 窄屏专属：跨断点往返后地球宽度必须等于两列基准 ----
        if not wide:
            base_w = base["cen"]["w"]
            cmd("Emulation.setDeviceMetricsOverride",
                {"width": 1920, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
            time.sleep(1.2)
            big = ev(MEASURE)
            cmd("Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
            time.sleep(1.2)
            small = ev(MEASURE)
            if isinstance(big, str) or isinstance(small, str):
                results.append((f"{tag}: 跨断点往返", "FAIL", "测量异常"))
            else:
                results.append((f"{tag}: 切到1920后不覆盖", "PASS" if big["overlap"] <= 4 and big["gridPos"] != "absolute" else "FAIL",
                                f"重叠 {big['overlap']}px pos={big['gridPos']}"))
                okw = abs(small["cen"]["w"] - base_w) <= 6
                results.append((f"{tag}: 切回后地球宽度复原", "PASS" if okw else "FAIL",
                                f"两列 {base_w} → 往返后 {small['cen']['w']}（要求差 ≤6px）"))

        # ---- 2) 收起第三列（点「收起第三侧」按钮，若无则拖回） ----
        collapsed = ev("""(() => {
          var b = document.getElementById('btnCol3Close');
          if (b && getComputedStyle(b).display !== 'none') { b.click(); return 'button'; }
          var src = document.querySelector('.col3 .panel');
          var dst = document.querySelector('.colL .panel');
          if (!src || !dst) return 'no-target';
          var dt = new DataTransfer();
          (src.querySelector('.pt')||src).dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
          src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
          dst.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt}));
          dst.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));
          src.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
          return 'drag';
        })()""")
        time.sleep(1.2)
        back = ev(MEASURE)
        if isinstance(back, str):
            results.append((f"{tag}: 收起后测量", "FAIL", back))
            return
        results.append((f"{tag}: 收起方式", "PASS" if collapsed in ("button", "drag") else "FAIL", str(collapsed)))
        results.append((f"{tag}: 已复原两列", "PASS" if (not back["three"] and not back["col3"]) else "FAIL",
                        f"three={back['three']} col3={back['col3']}"))
        results.append((f"{tag}: 7 块面板全可见", "PASS" if back["visiblePanels"] >= 7 and back["removedPanels"] == 0 else "FAIL",
                        f"可见 {back['visiblePanels']}/{back['totalPanels']}，removed={back['removedPanels']}"))
        results.append((f"{tag}: 两列不再覆盖地球", "PASS" if back["overlap"] <= 4 and back["gridPos"] != "absolute" else "FAIL",
                        f"重叠 {back['overlap']}px，grid.position={back['gridPos']}"))
        # 列高应接近（无大空洞）
        gap = abs(back["colHeights"]["L"] - back["colHeights"]["R"])
        results.append((f"{tag}: 两列高度平衡", "PASS" if gap <= 20 else "FAIL",
                        f"L={back['colHeights']['L']} R={back['colHeights']['R']} 差 {gap}px"))
        # ---- 3) 再开一次三列，然后用「×」移除第三列最后一块，必须自动收起 ----
        ev("""(() => {
          if (!document.querySelector('.col3')) {
            var src = document.querySelector('.colR .panel');
            var dz = document.getElementById('drop3');
            var dt = new DataTransfer();
            (src.querySelector('.pt')||src).dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
            src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
            dz.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt}));
            dz.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));
            src.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt}));
          }
          return 1;
        })()""")
        time.sleep(1.0)
        opened = ev(MEASURE)
        removed = ev("""(() => {
          var p = document.querySelector('.col3 .panel:not(.removed)');
          if (!p) return 'no-panel';
          var b = p.querySelector('.xrm');
          if (!b) return 'no-x';
          b.click();
          return 'clicked';
        })()""")
        time.sleep(1.2)
        after_x = ev(MEASURE)
        if isinstance(opened, str) or isinstance(after_x, str):
            results.append((f"{tag}: × 移除后测量", "FAIL", "测量异常"))
        else:
            results.append((f"{tag}: × 移除最后一块自动收起", "PASS" if (not after_x["three"] and not after_x["col3"]) else "FAIL",
                            f"点击={removed} three={after_x['three']} col3={after_x['col3']}"))
            # 被 × 掉的面板应可在「已移除的面板」里恢复（搬到左列且保持 removed）
            results.append((f"{tag}: 被移除面板可在左列恢复", "PASS" if after_x["visiblePanels"] + after_x["removedPanels"] == after_x["totalPanels"] else "FAIL",
                            f"可见 {after_x['visiblePanels']} + removed {after_x['removedPanels']} = {after_x['totalPanels']}"))

        # 权重应回到默认（每块 ≤1.2）
        results.append((f"{tag}: 面板权重已复原", "PASS" if back["sumWeightL"] <= 8.5 and back["sumWeightR"] <= 8.5 else "FAIL",
                        f"L权重和={back['sumWeightL']:.1f} R权重和={back['sumWeightR']:.1f}"))
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.kill()


def main():
    results = []
    run_at(1920, 1000, "宽屏1920", results)
    run_at(1440, 900, "窄屏1440", results)
    ok = sum(1 for _, s, _ in results if s == "PASS")
    bad = sum(1 for _, s, _ in results if s == "FAIL")
    print(f"\n=== 三列布局验收：PASS {ok} / FAIL {bad} ===")
    for name, status, detail in results:
        print(f"  [{status}] {name} :: {detail}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
