# 第三方素材来源（static/vendor/textures）

本项目只使用**可再分发**的地球贴图，均为 NASA 公有领域影像的二次加工版本，
取自 three.js 官方示例仓库（MIT License）与 NASA Visible Earth：
https://github.com/mrdoob/three.js/tree/r149/examples/textures/planets

| 文件 | 大小 | 用途 | 来源 |
|---|---|---|---|
| earth_day_5400.jpg | 2.45 MB | **主地表（5400×2700 Blue Marble）** | NASA Visible Earth（eoimages.gsfc.nasa.gov，公有领域） |
| earth_atmos_4096.jpg | 0.68 MB | 备用地表（4096） | three.js examples/planets |
| earth_atmos_2048.jpg | 0.49 MB | 备用地表（2048，回退用） | three.js examples/planets |
| earth_lights_2048.png | 0.70 MB | 夜间城市灯光 | three.js examples/planets |
| earth_normal_2048.jpg | 0.32 MB | 地形法线（山脊起伏） | three.js examples/planets |
| earth_specular_2048.jpg | 0.21 MB | 海面高光遮罩 | three.js examples/planets |
| earth_clouds_2048.png | 4.22 MB | **云层（alpha 为云量）** | three.js examples/planets |
| earth_clouds_1024.png | 0.25 MB | 备用云层（1024） | three.js examples/planets |

- three.js 仓库：MIT License
- 原始影像：NASA Visible Earth / Blue Marble，公有领域
- 合计约 9.3 MB，全部本地加载，无 CDN 依赖
- 说明：3D 椭球渲染不需要 8K 级别（8K 单张约 20–50 MB，收益有限）；当前 5400 已比原 2048 清晰 2.6 倍
