# macOS 兼容性审计（2026-10-22）

> 结论：**代码层面已具备跨平台能力**（静态审计 + 5 处改造），但**尚未在真机 Mac 上实跑**。
> 下面列出已适配项、已修问题、以及你在 Mac 上需要验证的 6 步。

## 一、静态审计结果

| 检查项 | 结果 |
|---|---|
| 依赖（requirements.txt：fastapi/uvicorn/httpx/feedparser/apscheduler/openai/dotenv） | ✅ 全部跨平台 |
| 路径 | ✅ 代码统一用 `Path(__file__)` / `ROOT / "data"`，无硬编码盘符（已删掉残留的 `data/_probe_leads.py`） |
| 数据库 | ✅ SQLite + WAL，路径在项目内 |
| 定时调度 | ✅ APScheduler（Asia/Shanghai 时区），与时区无关的固定时刻 |
| 日志 | ✅ 无控制台时由 `run.py` 自己写 `data/logs/app.log`（UTF-8） |
| CSS 字体 | ✅ `"Microsoft YaHei","PingFang SC"`，Mac 有 PingFang |
| 平台榜单（Chrome） | ✅ Chrome 路径含 macOS：`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` |
| 启动/停止脚本 | ✅ `启动.command` / `停止.command`（bash + lsof + open） |
| 桥进程管理 | ⚠️ 原实现是 Windows 专有 → **已改为跨平台** |

## 二、本次修掉的 5 个 Mac 隐患

1. **桥的端口清理是 Windows 专有**
   `netstat -ano` / `wmic` / `taskkill` → 改为按平台分支：macOS/Linux 用 `lsof -ti tcp:PORT -sTCP:LISTEN` + `ps -p PID -o command=` + `os.kill(pid, 9)`。
2. **`.env` 是 Windows 上创建的（CRLF）**
   `启动.command` 里 `source .env` 会把 `PORT` 读成 `"9000\r"` → 改为 `source <(tr -d '\r' < .env)`。
3. **Chrome headless 参数版本差异**
   `--headless` 改为 `--headless=new`，失败自动回退旧参数（兼容不同 Chrome 版本）。
4. **错误信息写死 `chrome.exe`**
   改为「找不到 Chrome（Windows 装 Chrome；macOS 放 /Applications；Linux 装 google-chrome）」。
5. **残留脚本硬编码 `D:\...`**
   删除未被引用的 `data/_probe_leads.py`。

> 改完后在 Windows 上回归通过：桥 `ok=True`、`/cls/telegraph` 200 / 50 条、Amazon 抓取 677 KB（92 个商品链接）。

## 三、Mac 上需要你实机验证的 6 步

```bash
# 0. 准备（一次性）
brew install python@3.12
brew install --cask google-chrome
chmod +x 启动.command 停止.command

# 1. 启动（首次会自动建 .venv + 清华镜像装依赖）
./启动.command

# 2. 健康检查（应返回 status=ok、db.tables>=9、bridge.ok=true）
curl -s http://127.0.0.1:9000/api/health

# 3. 源体检（应 18/18 可用；不行就按设置页提示切代理）
curl -s -X POST http://127.0.0.1:9000/api/sources/check -H 'Content-Type: application/json' -d '{}' | head -c 400

# 4. 平台榜单抓取（需要 Chrome；约 30–60 秒）
python run.py --platform-snapshot

# 5. 手动跑一轮（验证模型调用与记账）
python run.py --run-once --deep-limit 2

# 6. 停止
./停止.command
```

## 四、Mac 已知差异（不影响使用）

| 现象 | 说明 |
|---|---|
| 启动后会停在 Terminal 里显示日志 | `启动.command` 是前台运行；想后台可 `nohup ./启动.command >/dev/null 2>&1 &` |
| 屏幕休眠会暂停定时 | 与 Windows 一致；2 小时内错过的轮次唤醒后会补跑（`misfire_grace_time=7200`） |
| 代理设置 | 桥/Chrome 跟随系统代理；直连源按界面「直连/代理」逐源控制 |

## 五、结论

- **可以运行**：依赖、路径、脚本、桥、Chrome 抓取都已适配
- **没有实机证据**：Mac 上的 6 步验证尚未执行（本次只在 Windows 上做了等价回归）
- 建议：第一次在 Mac 上跑完上面 6 步后，把结果补进 `docs/acceptance-report.md` 的 N-5 行
