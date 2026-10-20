/* 3D 地球模块 V2：L0 三层星空 / L1 昼夜 Shader 地球 / L2 双层大气 / L3 经纬网格 / L4 潜力光点(Sprite) / L5 贝塞尔飞线。
   依赖 static/vendor/three.min.js（r149 UMD，无 EffectComposer/Bloom，辉光全用加法混合 + 代码生成贴图）。
   输入：window.EARTH.setDots([{lat,lon,n,stage}])，国家/潜力/id 从 window.__leads 匹配补齐。
   输出：window.EARTH.ok()、window.__fps5/__spots/__lines/__dots/__sun/__fallback 等验收用调试量。
   回退：WebGL 或 Shader 编译失败 → MeshBasicMaterial 贴图球 + 控制台警告，绝不留黑屏。 */
(function () {
  var $ = function (i) { return document.getElementById(i); };
  window.__err = window.__err || [];
  var el = $("g3d"), dgl = $("dgl");
  var webgl = false, globe, cam, rn, scene;
  var hot = [], lines = [], ripples = [], ambArcs = [];
  var STAGE_COLOR = { S1: 0x4d9de0, S2: 0x8a63f5, S3: 0xf5a623, S4: 0x2ecc8f };
  var STAGE_NAME = { S1: "S1 选品", S2: "S2 供应链", S3: "S3 交易结构", S4: "S4 客户开发" };
  var MAX_DOTS = 30, MAX_LINES = 20, MAX_STARS = 3000;

  function latLon(lat, lon, r) {
    var a = lat * Math.PI / 180, b = lon * Math.PI / 180;
    return new THREE.Vector3(Math.cos(a) * Math.sin(b) * r, Math.sin(a) * r, Math.cos(a) * Math.cos(b) * r);
  }
  // 计算太阳方向单位向量（真实 UTC 时间，每 60s 更新；失败回退固定向量）
  function computeSun() {
    var now = new Date();
    var day = (Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - Date.UTC(now.getUTCFullYear(), 0, 0)) / 864e5;
    var gamma = 2 * Math.PI / 365 * (day - 1 + (now.getUTCHours() - 12) / 24);
    var decl = 0.006918 - 0.399912 * Math.cos(gamma) + 0.070257 * Math.sin(gamma);
    var eqTime = 229.18 * (0.000075 + 0.001868 * Math.cos(gamma) - 0.032077 * Math.sin(gamma) - 0.014615 * Math.cos(2 * gamma) - 0.040849 * Math.sin(2 * gamma)) / 60;
    var ha = (now.getUTCHours() + now.getUTCMinutes() / 60 - 12 + eqTime) / 24 * 2 * Math.PI;
    var latR = decl, lonR = -ha;
    return new THREE.Vector3(Math.cos(latR) * Math.sin(lonR), Math.sin(latR), Math.cos(latR) * Math.cos(lonR)).normalize();
  }
  // 覆盖太阳方向：?sun=x,y,z（调试）
  function parseSunOverride() {
    try {
      var m = (location.search || "").match(/[?&]sun=([^&]+)/);
      if (m) {
        var p = m[1].split(",").map(parseFloat);
        if (p.length === 3 && p.every(function (v) { return isFinite(v); })) {
          var v = new THREE.Vector3(p[0], p[1], p[2]);
          if (v.lengthSq() > 0.001) { v.normalize(); window.__sunOverride = v.toArray(); return v; }
        }
      }
    } catch (e) { /* 参数非法时忽略 */ }
    return null;
  }
  var sunOverride = parseSunOverride();
  var sunDir = sunOverride || computeSun();
  var sunTimer = 0;
  function refreshSun() {
    if (!sunOverride) sunDir = computeSun();
    if (baseMat) { baseMat.uniforms.uSun.value.copy(sunDir); }
    if (cloudMat) { cloudMat.uniforms.uSun.value.copy(sunDir); }
    window.__sun = sunDir.toArray();
  }

  // ---- 代码生成径向渐变发光贴图（光点/光晕共用）----
  var glowTex = null;
  function buildGlowTex() {
    var c = document.createElement("canvas"); c.width = c.height = 64;
    var g = c.getContext("2d");
    var grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, "rgba(255,255,255,0.85)");
    grad.addColorStop(0.25, "rgba(255,255,255,0.62)");
    grad.addColorStop(0.55, "rgba(255,255,255,0.20)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    glowTex = new THREE.CanvasTexture(c);
  }

  // 按阶段色生成光点贴图（缓存 4 份），避免白色核心盖掉阶段色
  // 环形渐变贴图（涟漪用；RingGeometry 在部分驱动下会渲染成方斑）
  var ringTex = null;
  function buildRingTex() {
    var c = document.createElement("canvas"); c.width = c.height = 128;
    var g = c.getContext("2d");
    var grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    grad.addColorStop(0.00, "rgba(255,255,255,0)");
    grad.addColorStop(0.62, "rgba(255,255,255,0)");
    grad.addColorStop(0.80, "rgba(255,255,255,0.95)");
    grad.addColorStop(0.92, "rgba(255,255,255,0.25)");
    grad.addColorStop(1.00, "rgba(255,255,255,0)");
    g.fillStyle = grad; g.fillRect(0, 0, 128, 128);
    ringTex = new THREE.CanvasTexture(c);
  }
  // 四芒星贴图（光点炫光）
  var flareTex = null;
  function buildFlareTex() {
    var c = document.createElement("canvas"); c.width = c.height = 128;
    var g = c.getContext("2d");
    g.clearRect(0, 0, 128, 128);
    var core = g.createRadialGradient(64, 64, 0, 64, 64, 16);
    core.addColorStop(0, "rgba(255,255,255,1)");
    core.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = core; g.fillRect(0, 0, 128, 128);
    function ray(w, h) {
      var grd = g.createLinearGradient(64 - w / 2, 64, 64 + w / 2, 64);
      grd.addColorStop(0, "rgba(255,255,255,0)");
      grd.addColorStop(0.5, "rgba(255,255,255,0.95)");
      grd.addColorStop(1, "rgba(255,255,255,0)");
      g.fillStyle = grd;
      g.save(); g.translate(64, 64);
      g.fillRect(-h / 2, -w / 2, h, w);
      g.rotate(Math.PI / 2);
      g.fillRect(-h / 2, -w / 2, h, w);
      g.restore();
    }
    ray(3, 120);
    flareTex = new THREE.CanvasTexture(c);
  }
  var glowCache = {};
  function glowFor(hex) {
    var key = "#" + new THREE.Color(hex).getHexString();
    if (glowCache[key]) return glowCache[key];
    var c = document.createElement("canvas"); c.width = c.height = 64;
    var g = c.getContext("2d");
    var rgb = new THREE.Color(hex);
    var r = Math.round(rgb.r * 255), gg2 = Math.round(rgb.g * 255), b = Math.round(rgb.b * 255);
    var grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, "rgba(" + r + "," + gg2 + "," + b + ",0.72)");
    grad.addColorStop(0.34, "rgba(" + r + "," + gg2 + "," + b + ",0.46)");
    grad.addColorStop(0.62, "rgba(" + r + "," + gg2 + "," + b + ",0.22)");
    grad.addColorStop(1, "rgba(" + r + "," + gg2 + "," + b + ",0)");
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    var t = new THREE.CanvasTexture(c);
    glowCache[key] = t;
    return t;
  }

  try {
    if (!document.createElement("canvas").getContext("webgl") &&
        !document.createElement("canvas").getContext("experimental-webgl")) throw new Error("nowebgl");
    var W = el.clientWidth || 800, H = el.clientHeight || 400;
    scene = new THREE.Scene(); scene.background = new THREE.Color(0x060a12);
    cam = new THREE.PerspectiveCamera(45, W / H, 0.1, 300); cam.position.set(0, 0, 4);
    rn = new THREE.WebGLRenderer({ antialias: true });
    rn.setPixelRatio(Math.min(devicePixelRatio, 2)); rn.setSize(W, H, false);
    if (!rn.getContext()) throw new Error("nowebgl");
    webgl = true;
    el.appendChild(rn.domElement);
    var cv = rn.domElement;

    globe = new THREE.Group(); globe.position.x = -0.10;
    // 初始自转：让晨昏线横跨可见面（太阳在 +Z 附近时，-90° 使夜面/城市灯光可见）
    globe.rotation.y = -Math.PI / 2;
    scene.add(globe);
    var tl = new THREE.TextureLoader(); tl.crossOrigin = undefined;
    var baseTex = tl.load("./static/vendor/textures/earth_day_5400.jpg");
    var lightTex = tl.load("./static/vendor/textures/earth_lights_2048.png");
    baseTex.anisotropy = 2; lightTex.anisotropy = 2;

    // ---- L0 星空：三层 Points（近/中/远，共 1200–3000 点），顶点着色器闪烁 + 极慢自转 ----
    var starsLayer = [];
    function buildStars(count, r0, r1, sizeBase, bright) {
      var pos = new Float32Array(count * 3), pha = new Float32Array(count), br = new Float32Array(count);
      for (var i = 0; i < count; i++) {
        var v = new THREE.Vector3().randomDirection().multiplyScalar(r0 + Math.random() * (r1 - r0));
        pos[i * 3] = v.x; pos[i * 3 + 1] = v.y; pos[i * 3 + 2] = v.z;
        pha[i] = Math.random();
        var r = Math.random();
        br[i] = r < 0.55 ? 0.35 : (r < 0.85 ? 0.7 : 1);
      }
      var g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      g.setAttribute("aP", new THREE.BufferAttribute(pha, 1));
      g.setAttribute("aB", new THREE.BufferAttribute(br, 1));
      var m = new THREE.ShaderMaterial({
        uniforms: { uT: { value: 0 }, uC: { value: new THREE.Color(0xbfd9ff) }, uSize: { value: sizeBase }, uOp: { value: bright } },
        transparent: true, depthWrite: false,
        vertexShader: "attribute float aP;attribute float aB;uniform float uT;uniform float uSize;varying float vP;varying float vB;void main(){vP=aP;vB=aB;vec4 mv=modelViewMatrix*vec4(position,1.0);gl_PointSize=(uSize/-mv.z)*(0.75+0.5*sin(uT*1.2+aP*6.283))*(0.55+aB*1.15);gl_Position=projectionMatrix*mv;}",
        fragmentShader: "uniform float uT;uniform vec3 uC;uniform float uOp;varying float vP;varying float vB;void main(){vec2 c=gl_PointCoord-0.5;float d=length(c);float a=smoothstep(0.5,0.08,d)*(0.55+0.45*sin(uT*1.6+vP*6.283))*(0.5+vB*0.75)*uOp;if(a<0.05)discard;gl_FragColor=vec4(uC,a);}"
      });
      var p = new THREE.Points(g, m);
      scene.add(p);
      starsLayer.push({ obj: p, mat: m });
    }
    // 近 700 / 中 800 / 远 500 = 2000 点（≤3000，≥1200）
    buildStars(700, 13, 20, 150, 1.0);
    buildStars(800, 22, 34, 90, 0.62);
    buildStars(500, 38, 60, 50, 0.34);

    // ---- L1 地球：法线光照 + 海洋高光 + 晨昏暖光 + 夜灯（比纯漫反射真实得多）----
    var normalTex = tl.load("./static/vendor/textures/earth_normal_2048.jpg");
    var specTex = tl.load("./static/vendor/textures/earth_specular_2048.jpg");
    var cloudTex = tl.load("./static/vendor/textures/earth_clouds_2048.png");
    var baseMat = new THREE.ShaderMaterial({
      uniforms: {
        uSun: { value: sunDir.clone() },
        uDay: { value: baseTex }, uNight: { value: lightTex },
        uNormal: { value: normalTex }, uSpec: { value: specTex },
        uDayMul: { value: 1.25 }, uNightMul: { value: 2.2 }, uAmb: { value: 0.10 },
        uRelief: { value: 0.85 }
      },
      vertexShader: `
        varying vec2 vUv; varying vec3 vN; varying vec3 vV; varying vec3 vP;
        void main() {
          vUv = uv;
          vN = normalize(mat3(modelMatrix) * normal);
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          vV = -mv.xyz; vP = mv.xyz;
          gl_Position = projectionMatrix * mv;
        }`,
      fragmentShader: `
        uniform vec3 uSun;
        uniform sampler2D uDay, uNight, uNormal, uSpec;
        uniform float uDayMul, uNightMul, uAmb, uRelief;
        varying vec2 vUv; varying vec3 vN; varying vec3 vV; varying vec3 vP;
        void main() {
          vec3 N = normalize(vN);
          // 球面切线基：东向 / 北向
          vec3 east = normalize(cross(vec3(0.0, 1.0, 0.0), N));
          vec3 north = normalize(cross(N, east));
          vec3 nt = texture2D(uNormal, vUv).rgb * 2.0 - 1.0;
          vec3 Nm = normalize(N + (east * nt.x + north * nt.y) * uRelief * 0.55);
          float ndl = dot(Nm, uSun);
          vec3 V = normalize(vV);
          vec3 H = normalize(uSun + V);

          vec3 dayTex = texture2D(uDay, vUv).rgb;
          float lit = max(ndl, 0.0);
          vec3 col = dayTex * (uAmb + uDayMul * lit);

          // 海面镜面高光（贴图里海洋为亮区）
          float ocean = texture2D(uSpec, vUv).r;
          float sp = pow(max(dot(Nm, H), 0.0), 90.0) * ocean * 0.40;
          col += vec3(1.0, 0.97, 0.90) * sp;

          // 晨昏线暖光
          float term = exp(-pow(ndl / 0.16, 2.0));
          col += vec3(1.0, 0.48, 0.18) * term * 0.16;

          // 夜面灯光（只在暗面出现，边缘渐隐）
          float darkF = smoothstep(0.18, -0.10, ndl);
          col += texture2D(uNight, vUv).rgb * uNightMul * darkF;

          // 边缘冷调（大气散射感）
          float rim = pow(1.0 - max(dot(N, V), 0.0), 3.0);
          col += vec3(0.16, 0.34, 0.62) * rim * 0.55;

          gl_FragColor = vec4(col, 1.0);
        }`
    });
    try {
      var maxAn = rn.capabilities.getMaxAnisotropy();
      [baseTex, lightTex, normalTex, specTex, cloudTex].forEach(function (tx) {
        if (tx) { tx.anisotropy = maxAn; tx.needsUpdate = true; }
      });
    } catch (e2) { /* 忽略 */ }
    var base = new THREE.Mesh(new THREE.SphereGeometry(1, 96, 64), baseMat);
    base.renderOrder = 0; globe.add(base);

    // 云层（独立球体，略大，受同一太阳照明）
    var cloudMat = new THREE.ShaderMaterial({
      uniforms: {
        uSun: { value: sunDir.clone() }, uCloud: { value: cloudTex },
        uMul: { value: 0.78 }, uDrift: { value: 0 }
      },
      transparent: true, depthWrite: false,
      vertexShader: `
        varying vec2 vUv; varying vec3 vN;
        void main() {
          vUv = uv; vN = normalize(mat3(modelMatrix) * normal);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: `
        uniform sampler2D uCloud; uniform vec3 uSun; uniform float uMul, uDrift;
        varying vec2 vUv; varying vec3 vN;
        void main() {
          float a = texture2D(uCloud, vec2(fract(vUv.x + uDrift), vUv.y)).a;
          float ndl = dot(normalize(vN), uSun);
          float lit = clamp(ndl * 1.25 + 0.12, 0.0, 1.0);
          vec3 c = mix(vec3(0.16, 0.22, 0.36), vec3(1.0, 1.0, 1.0), lit);
          gl_FragColor = vec4(c, a * uMul * smoothstep(-0.15, 0.25, ndl) + a * uMul * 0.25);
        }`
    });
    var clouds = new THREE.Mesh(new THREE.SphereGeometry(1.006, 96, 64), cloudMat);
    clouds.renderOrder = 0; globe.add(clouds);
    // 第二层云：同贴图错位 60%，只做固定偏移（不漂移，避免接缝）拉高云量
    var cloudMat2 = cloudMat.clone();
    cloudMat2.uniforms.uMul.value = 0.42;
    cloudMat2.uniforms.uSun.value = cloudMat.uniforms.uSun.value;
    var clouds2 = new THREE.Mesh(new THREE.SphereGeometry(1.012, 96, 64), cloudMat2);
    clouds2.rotation.y = Math.PI * 0.6; clouds2.rotation.x = 0.12;
    clouds2.renderOrder = 0; globe.add(clouds2);
    refreshSun();


    // 经纬网格（L3，透明度降到 0.06，仅空间参照）
    var garr = [];
    for (var lo = 0; lo < 360; lo += 30) {
      var a = []; for (var la = -90; la <= 90; la += 6) a.push(latLon(la, lo, 1.004));
      for (var k = 0; k < a.length - 1; k++) { garr.push(a[k].x, a[k].y, a[k].z, a[k + 1].x, a[k + 1].y, a[k + 1].z); }
    }
    for (var la2 = -60; la2 <= 60; la2 += 30) {
      var a2 = []; for (var lo2 = 0; lo2 <= 360; lo2 += 6) a2.push(latLon(la2, lo2, 1.004));
      for (var k2 = 0; k2 < a2.length - 1; k2++) { garr.push(a2[k2].x, a2[k2].y, a2[k2].z, a2[k2 + 1].x, a2[k2 + 1].y, a2[k2 + 1].z); }
    }
    var gg = new THREE.BufferGeometry(); gg.setAttribute("position", new THREE.Float32BufferAttribute(garr, 3));
    var grid = new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0x3d6ea8, transparent: true, opacity: 0.06 }));
    grid.renderOrder = 1; globe.add(grid);

    // ---- L2 大气：内层 BackSide 菲涅尔加法辉光 + 外层半透明外晕 ----
    var atmMat = new THREE.ShaderMaterial({
      uniforms: { uCol: { value: new THREE.Color(0x3f8fdd) }, uPow: { value: 2.6 }, uK: { value: 0.45 } },
      side: THREE.BackSide, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      vertexShader: "varying vec3 vN;varying vec3 vV;void main(){vN=normalize((modelMatrix*vec4(normal,0.0)).xyz);vec4 mv=modelViewMatrix*vec4(position,1.0);vV=normalize(-mv.xyz);gl_Position=projectionMatrix*mv;}",
      fragmentShader: "uniform vec3 uCol;uniform float uPow;uniform float uK;varying vec3 vN;varying vec3 vV;void main(){float f=pow(1.0-max(dot(normalize(vN),normalize(vV)),0.0),uPow)*uK;gl_FragColor=vec4(uCol*f,f*0.85);}"
    });
    var atm = new THREE.Mesh(new THREE.SphereGeometry(1.03, 48, 24), atmMat);
    atm.renderOrder = 2; globe.add(atm);
    var haloMat = new THREE.ShaderMaterial({
      uniforms: { uCol: { value: new THREE.Color(0x3f8fdd) }, uPow: { value: 3.0 }, uK: { value: 0.20 } },
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      vertexShader: "varying vec3 vN;varying vec3 vV;void main(){vN=normalize((modelMatrix*vec4(normal,0.0)).xyz);vec4 mv=modelViewMatrix*vec4(position,1.0);vV=normalize(-mv.xyz);gl_Position=projectionMatrix*mv;}",
      fragmentShader: "uniform vec3 uCol;uniform float uPow;uniform float uK;varying vec3 vN;varying vec3 vV;void main(){float f=smoothstep(0.0,0.14,dot(normalize(vN),normalize(vV)));f=1.0-f;f=pow(f,uPow)*uK;gl_FragColor=vec4(uCol*f,f);}"
    });
    var halo = new THREE.Mesh(new THREE.SphereGeometry(1.16, 48, 24), haloMat);
    halo.renderOrder = 2; globe.add(halo);

    buildGlowTex();
    buildRingTex();
    buildFlareTex();

    // ---- 光点标签（悬停显示「国家 · 阶段 · 潜力」）----
    var label = document.createElement("div");
    label.className = "g3d-label";
    el.appendChild(label);
    function showLabel(h, x, y) {
      label.style.left = x + "px"; label.style.top = y + "px";
      label.style.borderColor = "#" + new THREE.Color(STAGE_COLOR[h.stage] || STAGE_COLOR.S1).getHexString();
      label.textContent = (h.country || "—") + " · " + (STAGE_NAME[h.stage] || h.stage) + " · 潜力 " + (h.pot == null ? "—" : h.pot);
      label.classList.add("show");
    }
    function hideLabel() { label.classList.remove("show"); }

    // ---- 交互：拖拽旋转 / 滚轮缩放（仅地球区）/ 悬停放大+标签 / 点击（位移<4px）----
    var drag = null, ivx = 0, ivy = 0, auto = performance.now(), zoomTarget = null, baseRotY = 0;
    var pointer = { x: -9999, y: -9999 }, hover = null, downPt = null;
    var raycaster = new THREE.Raycaster(); raycaster.params.Sprite = { threshold: 0.02 };

    cv.addEventListener("pointerdown", function (e) {
      downPt = { x: e.clientX, y: e.clientY };
      drag = { x: e.clientX, y: e.clientY };
      auto = performance.now(); ivx = ivy = 0;
      try { cv.setPointerCapture(e.pointerId); } catch (_) { /* 合成事件/无指针设备时忽略 */ }
      e.stopPropagation();
    });
    document.addEventListener("pointermove", function (e) {
      pointer.x = e.clientX; pointer.y = e.clientY;
      if (!drag) return;
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      globe.rotation.y += dx * 0.006; baseRotY = globe.rotation.y;
      globe.rotation.x = Math.max(-1.3, Math.min(1.3, globe.rotation.x + dy * 0.006));
      ivx = dx * 0.006 * 0.5; ivy = dy * 0.006 * 0.5; auto = performance.now();
      e.stopPropagation();
    });
    document.addEventListener("pointerup", function (e) {
      if (downPt && Math.abs(e.clientX - downPt.x) < 4 && Math.abs(e.clientY - downPt.y) < 4) {
        // 点击：命中光点则打开详情页（新标签），点击空白取消
        var h = hitTest(pointer.x, pointer.y);
        if (h && h.id) {
          // 点击前先轻微聚焦（镜头推近），有反馈再开新标签
          zoomTarget = Math.max(2.2, cam.position.z * 0.8);
          window.open("/static/detail.html?id=" + h.id, "_blank");
        }
      }
      if (hover && !h && h !== undefined) { hover.hover = false; hover.spr.scale.setScalar(hover.base * 1.55); hover = null; hideLabel(); }
      drag = null; downPt = null;
    });
    cv.addEventListener("wheel", function (e) {
      if (!el.contains(e.target)) return;
      e.preventDefault();
      cam.position.z = Math.min(7, Math.max(2.2, cam.position.z * (1 + e.deltaY * 0.0012)));
      auto = performance.now();
    }, { passive: false });
    cv.addEventListener("mouseleave", function () {
      pointer.x = -9999; pointer.y = -9999;
      if (hover) { hover.hover = false; hover.spr.scale.setScalar(hover.base * 1.55); hover = null; }
      hideLabel();
    });

    var hoverTick = 0;
    var HIT_SCALE = 2.6; // 命中区域放大，方便点击/悬停小光点
    function hitTest(cx, cy) {
      if (!hot.length || !cv) return null;
      var rr = cv.getBoundingClientRect();
      raycaster.setFromCamera(new THREE.Vector2((cx - rr.left) / rr.width * 2 - 1, -(cy - rr.top) / rr.height * 2 + 1), cam);
      hot.forEach(function (h) { h.spr.scale.setScalar(h.base * HIT_SCALE); });
      var hit = raycaster.intersectObjects(hot.map(function (h) { return h.spr; }), false);
      hot.forEach(function (h) { h.spr.scale.setScalar(h.base); });
      return hit.length ? hit[0].object.userData.h : null;
    }

    // 帧率统计 + 太阳 60s 刷新
    var fpsN = 0, fpsT = performance.now(), fpsA = []; window.__fps5 = 0;
    var clkT = new THREE.Clock();
    var sunRefreshMs = 60000;
    (function tick() {
      var now = performance.now(), t = clkT.getElapsedTime();
      globe.updateMatrixWorld(true); // 先刷新矩阵，保证悬停/点击射线命中正确
      starsLayer.forEach(function (s) { s.mat.uniforms.uT.value = t; });
      starsLayer.forEach(function (s) { s.obj.rotation.y += 0.00006; });
      if (now - sunTimer >= sunRefreshMs) { sunTimer = now; refreshSun(); }

      if (drag) { auto = now; }
      else if (ivx || ivy) {
        globe.rotation.y += ivx;
        globe.rotation.x = Math.max(-1.3, Math.min(1.3, globe.rotation.x + ivy));
        ivx *= 0.94; ivy *= 0.94; auto = now;
      }
      else if (now - auto > 3000 && !window.__stopAuto) {
        // 空闲：小幅度摆动，避免把线索转到背面
        globe.rotation.y = baseRotY + Math.sin(t * 0.16) * 0.22;
      }

      // 光点呼吸 + 脉冲环（呼吸缩放作用于 g，环在 g 内抵消世界缩放）
      hot.forEach(function (h) {
        var br = h.base * (1 + 0.10 * Math.sin(t * 2.1 + h.ph * 6.283));
        h.g.scale.setScalar(br);
        h.spr.scale.setScalar(h.hover ? h.base * 1.3 : h.base);
        var k = (now * 0.0008 + h.ph) % 1, s = 0.3 + k * 0.95;
        h.ring.scale.set(s / br, s / br, 1);
        h.ring.material.opacity = 0.7 * (1 - k);
        h.ring.quaternion.copy(cam.quaternion);
      });
      lines.forEach(function (L) {
        var k = (now * 0.0004 + L.ph) % 1;
        L.dot.position.copy(L.c.getPointAt(k));
        if (L.glow) L.glow.position.copy(L.c.getPointAt(0));
        // 流光带：头部亮、尾部渐隐（逐帧写顶点色，48 点开销可忽略）
        if (L.colors && L.geo) {
          var arr = L.colors.array, n = arr.length / 3;
          for (var i2 = 0; i2 < n; i2++) {
            var d2 = ((i2 / (n - 1)) - k + 1) % 1;      // 0..1，0 为头部
            var f2 = Math.max(0, 1 - d2 / 0.22);        // 22% 长度的光带
            f2 = f2 * f2;
            var base2 = L.baseCol;
            arr[i2 * 3] = base2.r * (0.18 + 2.2 * f2) + f2 * 0.8;
            arr[i2 * 3 + 1] = base2.g * (0.18 + 2.2 * f2) + f2 * 0.9;
            arr[i2 * 3 + 2] = base2.b * (0.18 + 2.2 * f2) + f2;
          }
          L.colors.needsUpdate = true;
        }
      });

      // 云层缓慢漂移 + 大气呼吸（±8%）
      // 扫描带旋转 + 星芒呼吸旋转
      if (window.__sweep) window.__sweep.rotation.y += 0.012;
      hot.forEach(function (h) {
        if (h.flare) h.flare.material.rotation = (now * 0.00035 + h.ph) % 6.283;
      });
      // 背景长途光点：慢速沿线移动（长距离扫过球面）
      for (var ab = 0; ab < ambArcs.length; ab++) {
        var A2 = ambArcs[ab];
        var ka = (now * 0.00016 + A2.ph) % 1;
        var apos = A2.c.getPointAt(ka);
        A2.dot.position.copy(apos);
        if (A2.dot.userData.glow) A2.dot.userData.glow.position.copy(apos);
      }
      // 云层漂移：小幅往复（±0.035），不跨越贴图接缝
      if (cloudMat) cloudMat.uniforms.uDrift.value = 0.035 * Math.sin(t * 0.045);
      if (haloMat) haloMat.uniforms.uK.value = 0.20 * (1 + 0.08 * Math.sin(t * 0.9));
      if (atmMat) atmMat.uniforms.uK.value = 0.45 * (1 + 0.05 * Math.sin(t * 1.3 + 1.7));
      // 点击聚焦：镜头平滑推近
      if (zoomTarget != null) {
        cam.position.z += (zoomTarget - cam.position.z) * 0.14;
        if (Math.abs(cam.position.z - zoomTarget) < 0.02) zoomTarget = null;
      }
      // 新线索涟漪
      for (var r3 = ripples.length - 1; r3 >= 0; r3--) {
        var rp = ripples[r3];
        rp.life += 1 / 60;
        var kk = rp.life / 1.2;
        if (kk >= 1) { globe.remove(rp.mesh); rp.mesh.geometry.dispose(); rp.mesh.material.dispose(); ripples.splice(r3, 1); continue; }
        rp.mesh.scale.setScalar(0.10 * (1 + kk * 2.8));
        rp.mesh.material.opacity = (1 - kk) * 0.85;
      }

      // 悬停检测（节流 ~每 4 帧）
      if (++hoverTick % 4 === 0 && !drag) {
        var h2 = hitTest(pointer.x, pointer.y);
        if (h2 !== hover) {
          if (hover) { hover.hover = false; hover.spr.scale.setScalar(hover.base * 1.55); }
          hover = h2;
          if (hover) {
            hover.hover = true;
            hover.spr.scale.setScalar(hover.base * 1.55 * 1.3);
            var p = new THREE.Vector3().copy(hover.g.position).applyMatrix4(globe.matrixWorld).project(cam);
            var rr2 = cv.getBoundingClientRect();
            showLabel(hover, rr2.left + (p.x + 1) / 2 * rr2.width, rr2.top + (1 - p.y) / 2 * rr2.height);
          } else hideLabel();
        }
      }

      if (cv.clientWidth > 0 && cv.clientHeight > 0) rn.render(scene, cam);

      if (window.__front === undefined || window.__recalcPts || window.__debugPts) {
        var ndc = hot.map(function (h) {
          return new THREE.Vector3().copy(h.g.position).applyMatrix4(globe.matrixWorld).project(cam);
        });
        window.__front = ndc.filter(function (v) { return Math.abs(v.x) <= 1 && Math.abs(v.y) <= 1 && v.z < 1; }).length;
        var rr = cv.getBoundingClientRect();
        window.__pts = ndc.map(function (v) {
          return (rr.left + (v.x + 1) / 2 * rr.width).toFixed(0) + "," + (rr.top + (1 - v.y) / 2 * rr.height).toFixed(0);
        }).join(";");
        window.__recalcPts = false;
      }
      window.__tex = { a: !!(baseTex.image && baseTex.image.complete), l: !!(lightTex.image && lightTex.image.complete) };
      fpsN++;
      if (now - fpsT >= 1000) {
        fpsA.push(fpsN * 1000 / (now - fpsT));
        if (fpsA.length > 6) fpsA.shift();
        window.__fps5 = Math.round(fpsA.reduce(function (a, b) { return a + b; }, 0) / fpsA.length);
        fpsT = now; fpsN = 0;
      }
      requestAnimationFrame(tick);
    })();

    addEventListener("resize", function () {
      var w = el.clientWidth, h = el.clientHeight;
      cam.aspect = w / h; cam.updateProjectionMatrix();
      rn.setPixelRatio(Math.min(devicePixelRatio, 2)); rn.setSize(w, h, false);
    });

    // ---- Shader 编译失败回退：MeshBasicMaterial 贴图球 + 警告，绝不留黑屏 ----
    try {
      rn.render(scene, cam);
    } catch (e) {
      window.__err.push("shader-fallback:" + String(e.message || e));
      console.warn("[earth-v2] Shader 编译失败，回退 MeshBasicMaterial 贴图球：", e);
      [base, atm, halo].forEach(function (m) { if (m) { globe.remove(m); m.material.dispose(); } });
      var fb = new THREE.Mesh(new THREE.SphereGeometry(1, 64, 40),
        new THREE.MeshBasicMaterial({ map: baseTex, color: 0x9fb8d8, transparent: true, opacity: 0.95 }));
      fb.renderOrder = 0;
      globe.add(fb);
      baseMat = null;
      window.__fallback = true;
    }

    window.__stars = starsLayer.reduce(function (a, s) { return a + s.obj.geometry.attributes.position.count; }, 0);
    window.__spots = 0; window.__lines = 0; window.__dots = 0;
    // 验收/调试钩子：返回活引用
    window.__dbg = {
      globe: function () { return globe; }, cam: function () { return cam; }, rn: function () { return rn; },
      hot: function () { return hot; }, lines: function () { return lines; },
      hitTest: function (x, y) { return hitTest(x, y); }, label: function () { return label; }
    };
  } catch (e) {
    window.__err.push(String(e.message || e));
    if (dgl) dgl.classList.add("show");
  }

  // ---- 动态线索点 + 飞线（dots=[{lat,lon,n,stage}]，country/pot/id 从 window.__leads 补齐）----
  function setDots(dots) {
    if (!webgl || !globe) return;
    hot.forEach(function (h) { globe.remove(h.g); });
    lines.forEach(function (L) { globe.remove(L.L); globe.remove(L.dot); if (L.glow) globe.remove(L.glow); });
    hot = []; lines = [];

    // 从 __leads 构建 国家→(pot 取最大, 代表 id)
    var leads = window.__leads || [];
    var lm = {};
    leads.forEach(function (l) {
      if (!l || !l.country) return;
      var e = lm[l.country] || { pot: -1, id: null };
      var p = Number(l.potential) || 0;
      if (p > e.pot) { e.pot = p; e.id = l.id; }
      lm[l.country] = e;
    });

    var arr = dots.slice(0, MAX_DOTS);
    var prevC = window.__dotCountries || null;   // 上一次的国家集合（用于「新线索」涟漪）
    var curC = {};
    arr.forEach(function (d, i) {
      var g = new THREE.Group(); g.position.copy(latLon(d.lat, d.lon, 1.02));
      var col = STAGE_COLOR[d.stage] || 0x4d9de0;
      var meta = lm[d.country] || { pot: null, id: null };
      var pot = d.pot != null ? d.pot : meta.pot;
      var lid = d.id != null ? d.id : meta.id;
      var size = 0.6 + 0.8 * ((pot == null ? 0 : Math.min(pot, 100)) / 100); // 0.6–1.4 倍
      var baseS = 0.060 * size;
      curC[d.country] = 1;
      if (prevC && !prevC[d.country]) {
        // 新出现的国家：扩一圈涟漪
        var rm = new THREE.Sprite(new THREE.SpriteMaterial({
          map: ringTex, color: 0x9fe8ff, transparent: true, opacity: 0.85,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        rm.position.copy(g.position); rm.scale.setScalar(0.10); rm.renderOrder = 5;
        globe.add(rm); ripples.push({ mesh: rm, life: 0 });
      }
      var entry = { g: g, ring: null, base: baseS, ph: (i * 0.17) % 1, country: d.country, stage: d.stage, pot: pot, id: lid, hover: false };
      // 外层彩色光晕（阶段色真正显色）
      var halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowFor(col), color: 0xffffff, transparent: true, opacity: 1.0,
        blending: THREE.AdditiveBlending, depthWrite: false
      }));
      halo.scale.setScalar(baseS * 1.55);
      halo.renderOrder = 4;
      halo.userData.h = entry;
      // 内层实心亮核（小而白，保证在亮面上也看得见）
      var core = new THREE.Mesh(new THREE.SphereGeometry(baseS * 0.22, 12, 12),
        new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95,
          blending: THREE.AdditiveBlending, depthWrite: false }));
      core.renderOrder = 5;
      var ring = new THREE.Mesh(new THREE.RingGeometry(baseS * 1.05, baseS * 1.22, 44),
        new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.55, side: THREE.DoubleSide, blending: THREE.AdditiveBlending, depthWrite: false }));
      ring.renderOrder = 4;
      var flare = new THREE.Sprite(new THREE.SpriteMaterial({
        map: flareTex, color: col, transparent: true, opacity: 0.42,
        blending: THREE.AdditiveBlending, depthWrite: false }));
      flare.scale.setScalar(baseS * 4.0);
      flare.renderOrder = 5;
      entry.spr = halo; entry.core = core; entry.ring = ring; entry.flare = flare; entry.col = col;
      g.add(halo, core, ring, flare); globe.add(g);
      hot.push(entry);
    });

    // 飞线：两两连线（按两端潜力之和排序取前 MAX_LINES 条），三次贝塞尔弧
    var ordered = arr.slice().map(function (d) {
      return { d: d, pot: lm[d.country] ? lm[d.country].pot : (d.pot != null ? d.pot : 0) };
    }).sort(function (a, b) { return (b.pot || 0) - (a.pot || 0); });
    var pairs = [];
    for (var ai = 0; ai < ordered.length; ai++) {
      for (var bi = ai + 1; bi < ordered.length; bi++) {
        pairs.push({ a: ordered[ai], b: ordered[bi],
                     w: (ordered[ai].pot || 0) + (ordered[bi].pot || 0) });
      }
    }
    pairs.sort(function (x, y) { return y.w - x.w; });
    if (pairs.length) {
      var tos = pairs.slice(0, MAX_LINES);
      tos.forEach(function (pr, i) {
        var pa = latLon(pr.a.d.lat, pr.a.d.lon, 1.0), pb = latLon(pr.b.d.lat, pr.b.d.lon, 1.0);
        var dist = pa.distanceTo(pb);
        var lift = 1.10 + Math.min(0.20, dist * 0.14);
        var mid = pa.clone().add(pb).multiplyScalar(0.5).normalize().multiplyScalar(lift);
        var c = new THREE.CubicBezierCurve3(pa.clone().multiplyScalar(1.02), mid, mid, pb.clone().multiplyScalar(1.02));
        var pts = c.getPoints(72);
        var lg = new THREE.BufferGeometry().setFromPoints(pts);
        var baseCol = new THREE.Color([0x7fd8ff, 0x8fb8ff, 0xc9a8ff][i % 3]);
        var cols = new Float32Array(pts.length * 3);
        for (var ci = 0; ci < cols.length; ci += 3) { cols[ci] = baseCol.r; cols[ci + 1] = baseCol.g; cols[ci + 2] = baseCol.b; }
        lg.setAttribute("color", new THREE.BufferAttribute(cols, 3));
        var L = new THREE.Line(lg, new THREE.LineBasicMaterial({
          vertexColors: true, transparent: true, opacity: 0.95,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        L.renderOrder = 3;
        var dot = new THREE.Mesh(new THREE.SphereGeometry(0.014, 10, 10),
          new THREE.MeshBasicMaterial({ color: baseCol, blending: THREE.AdditiveBlending, depthWrite: false }));
        dot.renderOrder = 4;
        var glow = new THREE.Sprite(new THREE.SpriteMaterial({
          map: glowTex, color: 0x7fd8ff, transparent: true, opacity: 0.6,
          blending: THREE.AdditiveBlending, depthWrite: false
        }));
        glow.scale.setScalar(0.09); glow.renderOrder = 3;
        globe.add(L, dot, glow);
        lines.push({ c: c, dot: dot, L: L, glow: glow, ph: i * 0.31,
                     geo: lg, colors: lg.getAttribute("color"), baseCol: baseCol });
      });
    }
    // 首次渲染时把线索重心转到正面（相机在 +Z）
    if (!window.__focusDone && arr.length && Object.keys(lm).length) {
      var best = arr.slice().sort(function (a, b) {
        var pa2 = lm[a.country] ? lm[a.country].pot : 0, pb2 = lm[b.country] ? lm[b.country].pot : 0;
        return (pb2 || 0) - (pa2 || 0);
      })[0];
      var wp = latLon(best.lat, best.lon, 1.0);
      var targetY = -Math.atan2(wp.x, wp.z);
      baseRotY = targetY; globe.rotation.y = targetY;
      // 同时按纬度俯仰，让焦点落在画面中部
      globe.rotation.x = Math.atan2(wp.y, Math.hypot(wp.x, wp.z));
      window.__focusDone = true;
    }
    // ---- 背景长途光弧：装饰层（不代表具体线索，只做空间叙事）----
    if (!window.__ambientDone) {
      var CITY = {
        "北京": [39.9, 116.4], "新加坡": [1.35, 103.8], "迪拜": [25.2, 55.3],
        "伦敦": [51.5, -0.13], "纽约": [40.7, -74.0], "圣保罗": [-23.5, -46.6],
        "拉各斯": [6.5, 3.4], "孟买": [19.1, 72.9], "东京": [35.7, 139.7],
        "悉尼": [-33.9, 151.2], "约翰内斯堡": [-26.2, 28.0], "洛杉矶": [34.1, -118.2]
      };
      var ROUTES = [
        ["北京", "伦敦"], ["北京", "纽约"], ["新加坡", "迪拜"], ["孟买", "拉各斯"],
        ["东京", "洛杉矶"], ["圣保罗", "约翰内斯堡"], ["北京", "悉尼"], ["迪拜", "圣保罗"],
        ["伦敦", "孟买"], ["纽约", "东京"], ["深圳", "新加坡"], ["北京", "洛杉矶"], ["迪拜", "东京"]
      ];
      ROUTES.forEach(function (rt, idx) {
        var A = CITY[rt[0]], B = CITY[rt[1]];
        if (!A || !B) return;
        var pa = latLon(A[0], A[1], 1.0), pb = latLon(B[0], B[1], 1.0);
        var dd = pa.distanceTo(pb);
        var liftA = 1.06 + Math.min(0.10, dd * 0.07);
        var midA = pa.clone().add(pb).multiplyScalar(0.5).normalize().multiplyScalar(liftA);
        var cv = new THREE.CubicBezierCurve3(pa.clone().multiplyScalar(1.01), midA, midA,
                                             pb.clone().multiplyScalar(1.01));
        var ptsA = cv.getPoints(80);
        var geoA = new THREE.BufferGeometry().setFromPoints(ptsA);
        var colA = new THREE.Color([0x2f7fd0, 0x3f6fd8, 0x4f8fe8][idx % 3]);
        var colsA = new Float32Array(ptsA.length * 3);
        for (var q = 0; q < colsA.length; q += 3) {
          colsA[q] = colA.r * 0.5; colsA[q + 1] = colA.g * 0.5; colsA[q + 2] = colA.b * 0.5;
        }
        geoA.setAttribute("color", new THREE.BufferAttribute(colsA, 3));
        var LA = new THREE.Line(geoA, new THREE.LineBasicMaterial({
          vertexColors: true, transparent: true, opacity: 0.62,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        LA.renderOrder = 2;
        var adot = new THREE.Mesh(new THREE.SphereGeometry(0.016, 10, 10),
          new THREE.MeshBasicMaterial({ color: colA, blending: THREE.AdditiveBlending, depthWrite: false }));
        adot.renderOrder = 3;
        var aglow = new THREE.Sprite(new THREE.SpriteMaterial({
          map: glowFor(colA.getHex()), color: 0xffffff, transparent: true, opacity: 0.75,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        aglow.scale.setScalar(0.075); aglow.renderOrder = 3;
        globe.add(LA, adot, aglow);
        adot.userData.glow = aglow;
        ambArcs.push({ c: cv, dot: adot, ph: idx * 0.17, col: colA });
      });
      // 背景全球节点：铺满其他区域（装饰层，dim 且更小）
      var NODES = [
        [64.1, -21.9], [48.9, 2.4], [52.5, 13.4], [41.9, 12.5], [37.98, 23.7],
        [30.0, 31.2], [6.5, 3.4], [-1.3, 36.8], [-26.2, 28.0], [-33.9, 18.4],
        [25.2, 55.3], [19.1, 72.9], [28.6, 77.2], [13.8, 100.5], [1.35, 103.8],
        [-6.2, 106.8], [22.3, 114.2], [35.7, 139.7], [37.6, 127.0], [43.1, 131.9],
        [-33.9, 151.2], [-36.8, 174.8], [40.7, -74.0], [34.1, -118.2], [41.9, -87.6],
        [19.4, -99.1], [-23.5, -46.6], [-34.6, -58.4], [51.5, -0.13], [48.9, 2.35],
        [55.8, 37.6], [59.9, 30.3], [60.2, 24.9], [59.3, 18.1], [47.5, 19.0]
      ];
      NODES.forEach(function (nz, nidx) {
        var pos = latLon(nz[0], nz[1], 1.015);
        var sp = new THREE.Sprite(new THREE.SpriteMaterial({
          map: glowFor(0x66d0ff), color: 0xffffff, transparent: true,
          opacity: 0.42, blending: THREE.AdditiveBlending, depthWrite: false }));
        var sz = 0.020 + (nidx % 5) * 0.004;
        sp.scale.setScalar(sz); sp.position.copy(pos); sp.renderOrder = 4;
        globe.add(sp);
        var fl = new THREE.Sprite(new THREE.SpriteMaterial({
          map: flareTex, color: 0x66d0ff, transparent: true, opacity: 0.30,
          blending: THREE.AdditiveBlending, depthWrite: false }));
        fl.scale.setScalar(sz * 5.0); fl.position.copy(pos); fl.renderOrder = 4;
        globe.add(fl);
      });
      window.__nodes = NODES.length;

      // 扫描光带：赤道平面上一段旋转的弧
      var swGrp = new THREE.Group();
      var swPts = [];
      for (var sai = 0; sai <= 64; sai++) {
        var aa = -0.55 + sai / 64 * 1.1;
        swPts.push(new THREE.Vector3(Math.sin(aa) * 1.28, 0, Math.cos(aa) * 1.28));
      }
      var swGeo = new THREE.BufferGeometry().setFromPoints(swPts);
      var swLine = new THREE.Line(swGeo, new THREE.LineBasicMaterial({
        color: 0x35d0e8, transparent: true, opacity: 0.55,
        blending: THREE.AdditiveBlending, depthWrite: false }));
      var swLine2 = new THREE.Line(swGeo.clone(), new THREE.LineBasicMaterial({
        color: 0x7fe6f5, transparent: true, opacity: 0.30,
        blending: THREE.AdditiveBlending, depthWrite: false }));
      swLine2.scale.setScalar(0.97);
      swGrp.add(swLine, swLine2);
      globe.add(swGrp);
      window.__sweep = swGrp;

      window.__ambientDone = true;
      window.__ambient = ROUTES.length;
    }
    window.__dotCountries = curC;
    window.__spots = hot.length; window.__lines = lines.length; window.__dots = hot.length;
    window.__front = undefined; window.__recalcPts = true;
  }

  window.EARTH = {
    setDots: setDots,
    ok: function () { return webgl; },
    sun: function () { return sunDir.toArray(); }
  };
})();
