# 第三方依赖清单（前端 vendor）

> 规则：**只放 MIT / Apache-2.0 / BSD 等宽松许可证的库**；GPL/AGPL 一律不进项目（见 `AGENTS.md` 许可证边界）。

## 当前依赖

| 文件 | 库 | 版本 | 许可证 | 大小 | 来源 | SHA-256 |
|---|---|---|---|---|---|---|
| `three.min.js` | [three.js](https://threejs.org/) | **0.149.0（UMD）** | **MIT** | 608,081 B | `unpkg.com/three@0.149.0/build/three.min.js` | `8A5F7249903B54D30F79F708699D2FED2D6A1D0741A4CD41377D1F01BB5A2271` |
| `three.module.min.js` | [three.js](https://threejs.org/) | 0.160.0（ESM） | **MIT** | 670,681 B | `unpkg.com/three@0.160.0/build/three.module.min.js` | `3E690AC7D180B0AADF0891BEA39EEC643E29E2D3E75C99B18689518665F69BA6` |

**为什么同时放两份**：
- `three.min.js`（UMD）用普通 `<script>` 引入，**双击 html 就能跑**（`file://` 下不受 CORS 限制），当前静态稿用它。
- `three.module.min.js`（ESM）留给正式项目（本地服务 `http://127.0.0.1:8000`）用 `importmap` 引入，版本更新。
- 正式实现统一走 UMD 也完全可以，ESM 那份暂作备选，不引入构建链。

three.js 文件头自带许可声明：

```
/**
 * @license
 * Copyright 2010-2023 Three.js Authors
 * SPDX-License-Identifier: MIT
 */
```

## 明确不引入

| 项目 | 原因 |
|---|---|
| `songsummer920-dazzle/three-scope-map-skill`（552 star，Vue 3 + Three.js） | **GPL-3.0**，传染性许可证；且为 Vue 构建链 + 39MB 资源 |
| `MidaTechnology/Situation-aggregation-monitoring` | 无 LICENSE（默认保留所有权利） |
| `hipcityreg/situation-monitor` | 无 LICENSE |
| `sansan0/TrendRadar` | GPL-3.0 |

## 使用方式（零构建）

```html
<script type="importmap">
  { "imports": { "three": "/static/vendor/three.module.min.js" } }
</script>
<script type="module">
  import * as THREE from 'three';
</script>
```

不需要 npm、不需要打包。浏览器不支持 `importmap`（极老版本）时，回落到 Canvas 2D 版本。

## 更新流程

1. 下载新版本到 `static/vendor/`
2. 更新本文件的版本号、大小、SHA-256
3. 跑一次视觉回归：地球可见、光点可见、拖拽缩放正常、无控制台报错
4. 提交时把 vendor 文件一起提交（保证离线可用）
