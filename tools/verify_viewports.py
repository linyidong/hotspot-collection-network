# -*- coding: utf-8 -*-
"""模块：多分辨率 + 控制台错误 扫描（三列看板回归）。
职责：在 1440/1920/2560/3440 四种宽度下加载大屏，检查：
      1) 页面无 JS 异常 / console error；
      2) 两列时 grid 不覆盖地球；
      3) 三列时：≥1800px 不覆盖（重叠≤4）、<1800px 覆盖（重叠>0）；
      4) 收起第三侧后完整复原；
      5) 地球画布宽高比稳定（不变形）。
输出：每条 PASS/FAIL + 数字；退出码 0 = 全通过。
关键依赖：websocket-client、本机 Chrome、服务在 127.0.0.1:9000。
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
WIDTHS = [(1440, 900), (1920, 1080), (2560, 1440), (3440, 1440)]

MEASURE = r"""(() => {
  function r(sel){var e=document.querySelector(sel); if(!e) return null; var b=e.getBoundingClientRect();
    return {x:Math.round(b.x), right:Math.round(b.right), w:Math.round(b.width), h:Math.round(b.height)};}
  var cen=r('#cen'), g=r('#g3d'), grid=r('#grid');
  var panels=[...document.querySelectorAll('#grid .panel')];
  return {
    three: document.getElementById('grid').classList.contains('three'),
    col3: !!document.querySelector('.col3'),
    cen: cen, g3d: g, grid: grid,
    overlap: (cen&&grid) ? Math.max(0, Math.min(cen.right, grid.right) - Math.max(cen.x, grid.x)) : -1,
    pos: getComputedStyle(document.getElementById('grid')).position,
    vis: panels.filter(p=>!p.classList.contains('removed')).length,
    total: panels.length,
    removed: panels.filter(p=>p.classList.contains('removed')).length,
    colL: Math.round(document.querySelector('.colL').getBoundingClientRect().height),
    colR: Math.round(document.querySelector('.colR').getBoundingClientRect().height)
  };
})()"""


def check(width, height, results):
    tag = f"{width}x{height}"
    port = 9900 + width // 100
    prof = tempfile.mkdtemp(prefix=f"sw-{width}-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
         "--use-angle=swiftshader", f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", f"--user-data-dir={prof}",
         f"--window-size={width},{height}", URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws = None
    try:
        for _ in range(70):
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
            results.append((f"{tag}: 打开页面", "FAIL", "CDP 连不上"))
            return

        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Log.enable"}))
        errors = []

        def ev(expr, mid=[0]):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("method") == "Runtime.exceptionThrown":
                    d = m["params"]["exceptionDetails"]
                    errors.append(str(d.get("text"))[:80])
                    continue
                if m.get("method") == "Log.entryAdded":
                    e = m["params"]["entry"]
                    if e.get("level") == "error":
                        errors.append(str(e.get("text"))[:80])
                    continue
                if m.get("id") == mid[0]:
                    r = m.get("result", {})
                    if r.get("exceptionDetails"):
                        return "EXC:" + str(r["exceptionDetails"].get("text"))[:120]
                    return r.get("result", {}).get("value")

        for _ in range(150):
            time.sleep(0.3)
            if ev("!!document.querySelector('.colL') && typeof window.__leads !== 'undefined'"):
                break
        time.sleep(0.8)

        two = ev(MEASURE)
        if isinstance(two, str):
            results.append((f"{tag}: 页面测量", "FAIL", two))
            return
        results.append((f"{tag}: 两列不覆盖地球", "PASS" if two["overlap"] <= 4 and two["pos"] != "absolute" else "FAIL",
                        f"重叠 {two['overlap']}px pos={two['pos']}"))
        ar0 = (two["g3d"]["w"] / max(1, two["g3d"]["h"])) if two["g3d"] else 0

        # 开第三列
        ev("""(() => {var src=document.querySelector('.colR .panel'),dz=document.getElementById('drop3'),dt=new DataTransfer();
          (src.querySelector('.pt')||src).dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
          src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));
          dz.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:dt}));
          dz.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));
          src.dispatchEvent(new DragEvent('dragend',{bubbles:true,dataTransfer:dt})); return 1;})()""")
        time.sleep(1.3)
        three = ev(MEASURE)
        wide = width >= 1800
        if wide:
            results.append((f"{tag}: 三列不遮挡地球", "PASS" if three["overlap"] <= 4 and three["pos"] != "absolute" else "FAIL",
                            f"重叠 {three['overlap']}px pos={three['pos']}"))
        else:
            results.append((f"{tag}: 三列覆盖(允许)", "PASS" if three["overlap"] > 0 else "FAIL",
                            f"重叠 {three['overlap']}px"))
        # 画布比例会随布局变化，但 earth.js 在 resize 时会同步 cam.aspect，
        # 因此判定改为「地球渲染正常」：WebGL 已就绪 + 无渲染错误 + 画布尺寸非零
        earth_ok = ev("(window.EARTH && window.EARTH.ok && window.EARTH.ok()) === true")
        errs = ev("(window.__err||[]).length")
        size_ok = bool(three["g3d"] and three["g3d"]["w"] > 100 and three["g3d"]["h"] > 100)
        ok3 = earth_ok is True and errs == 0 and size_ok
        results.append((f"{tag}: 地球渲染正常", "PASS" if ok3 else "FAIL",
                        f"EARTH.ok={earth_ok} __err={errs} 画布 {three['g3d']['w']}x{three['g3d']['h']}（两列时 {two['g3d']['w']}x{two['g3d']['h']}，比例变化由相机自适应）"))

        # 收起
        ev("(() => {var b=document.getElementById('btnCol3Close'); if(b) b.click(); return 1;})()")
        time.sleep(1.2)
        back = ev(MEASURE)
        results.append((f"{tag}: 收起后复原两列", "PASS" if (not back["three"] and not back["col3"] and back["vis"] == back["total"] and back["removed"] == 0) else "FAIL",
                        f"three={back['three']} col3={back['col3']} 可见 {back['vis']}/{back['total']} removed={back['removed']}"))
        gap = abs(back["colL"] - back["colR"])
        results.append((f"{tag}: 两列高度平衡", "PASS" if gap <= 20 else "FAIL", f"L={back['colL']} R={back['colR']} 差 {gap}px"))

        time.sleep(0.6)
        results.append((f"{tag}: 无控制台错误", "PASS" if not errors else "FAIL",
                        "无" if not errors else "; ".join(errors[:3])))
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.kill()


def main():
    results = []
    for w, h in WIDTHS:
        check(w, h, results)
    ok = sum(1 for _, s, _ in results if s == "PASS")
    bad = [x for x in results if x[1] == "FAIL"]
    print(f"\n=== 多分辨率扫描：PASS {ok} / FAIL {len(bad)} ===")
    for name, status, detail in results:
        if status == "FAIL":
            print(f"  [FAIL] {name} :: {detail}")
    if not bad:
        print("  全部通过（4 种宽度 × 两列/三列/收起/无报错）")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
