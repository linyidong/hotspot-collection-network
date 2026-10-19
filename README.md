# Hotspot Collection Network（热点采集网络）

单人本机的跨境热点采集与线索加工工具。本仓库当前为 **T2 项目骨架**：一条命令起服务，接上配置层与 SQLite 八张表，提供健康检查接口。采集（T3）、LLM 加工（T4/T5）、大屏（T7）尚未实现。

## 一、安装

1. 安装 **Python 3.11 或更高**（安装时勾选 **Add Python to PATH**）。
2. 在项目根目录执行：

   ```bash
   pip install -r requirements.txt
   ```

3. （可选但强烈建议）配置密钥：把 `.env.example` 复制为 `.env`，填入 `DEEPSEEK_API_KEY`（获取地址：https://platform.deepseek.com → API Keys）。不填也能启动，只是页面会提示去申请，模型功能暂不可用。

## 二、启动

```bash
python run.py
```

- 启动时会依次检查：Python 版本、依赖、端口（默认 8010）、数据库。
- 成功后在浏览器打开 <http://127.0.0.1:8010>（首页为占位页）。
- 健康检查：<http://127.0.0.1:8010/api/health>，返回 JSON（status / version / db.ok / db.tables / key_configured / uptime_seconds）。
- 改端口：在 `.env` 中设置 `PORT=8001` 后重启。

## 三、常见错误

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `需要 Python 3.11 或更高版本` | 版本过低 | 安装 Python 3.11+，勾选 Add Python to PATH |
| `缺少依赖：fastapi…` | 依赖未装 | 执行 `pip install -r requirements.txt` |
| `端口 8010 已被占用` | 端口被其他程序占用 | 改 `.env` 的 `PORT`，或关闭占用进程 |
| `数据目录不可写` | 权限问题 | 检查 `data/` 目录写入权限 |
| 页面提示「尚未配置 DeepSeek API Key」 | 没配密钥 | 按页面指引复制 `.env.example` 为 `.env` 填 Key 后重启 |

## 目录结构（当前已实现）

```
run.py              # 唯一入口：检查 → 建表 → 起服务
app/config.py       # 配置层：.env + data/config.json + 默认值
app/db.py           # SQLite：WAL + 幂等建 8 张表
app/api/routes.py   # /api/health + 静态首页
static/index.html   # 深色占位页
data/app.db         # 数据库（首次启动自动生成，不进 git）
```
