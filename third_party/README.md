# third_party（第三方依赖目录）

本目录下的 `china-finance-rss/` 是从上游 vendor 进来的第三方组件，用于把
国内财经快讯源（财联社/东方财富/同花顺/金十/华尔街见闻）转成标准 RSS，
主服务以子进程方式在本地端口 8053 运行它（见 `app/bridge.py`）。

## 来源

- 项目：china-finance-rss
- 上游：https://github.com/nicennnnnnnlee/china-finance-rss（以仓库 README/LICENSE 为准）
- 用途：提供 5 个国内源的 RSS 路由，本项目的采集层只消费其 HTTP 输出，
  不修改其内部实现。

## 许可证

- `china-finance-rss` 以 **MIT** 许可发布，见 `china-finance-rss/LICENSE`。
- MIT 允许自由使用与再分发（保留版权与许可声明）。请保留该 LICENSE 文件，
  勿将其代码改写后声称自有。

## 为何 vendor（不拉取依赖，直接放仓库）

1. **本机实测国内源直连不可达**（SSL EOF / curl 000，代理也无效），只有
   华尔街见闻的 `api-one.wallstcn.com` 直连可达。主服务因此分两条路：
   华尔街见闻 3 频道直连 + 其余 5 源走本地桥。
2. 桥作为独立子进程运行，天然实现「超时/隔离」：单个上游慢或挂，
   不影响主服务与其他源。
3. vendor 固定版本，避免上游仓库变动影响本机可用性；升级时手动替换
   整个目录并重新验收。

## 维护约定

- 不要向 `china-finance-rss/` 提交与本项目无关的改动；需要适配时优先
  在本项目 `app/sources/` 层做，不修改桥内部。
- 桥的启动端口由 `.env` 的 `RSS_BRIDGE_PORT` 控制（默认 8053）。
- 桥不可用时，主服务不崩溃：`/api/health` 的 `bridge.ok=false`，
  `app/bridge.py` 会自动重启（进程内最多 2 次）。
