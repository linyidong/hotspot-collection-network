# China Finance RSS Bridge

Tiny RSS feeds for Chinese financial news: CLS, Eastmoney, THS, Jin10,
Wallstreetcn, and optional Xueqiu.

## Start

```bash
python server.py
```

Open `http://localhost:8053/`.

## Feeds

| Source | RSS URL | Notes |
| --- | --- | --- |
| CLS 财联社 | `http://localhost:8053/cls/telegraph` | Real-time flashes |
| Eastmoney 东方财富 | `http://localhost:8053/eastmoney/kuaixun` | 7x24 news |
| THS 同花顺 | `http://localhost:8053/ths/kuaixun` | 7x24 news |
| Jin10 金十 | `http://localhost:8053/jin10/flash` | 7x24 news |
| Wallstreetcn 华尔街见闻 | `http://localhost:8053/wallstreetcn/live` | 7x24 news |
| Xueqiu 雪球 | `http://localhost:8053/xueqiu/user/{uid}` | Requires Chrome CDP |

Import all built-in feeds with OPML:

```text
http://localhost:8053/opml.xml
```

Check upstream status:

```text
http://localhost:8053/healthz?check=1
```

## Configure Only If Needed

| Variable | Default | Use |
| --- | --- | --- |
| `PORT` | `8053` | Change the server port |
| `CACHE_TTL` | `300` | Cache upstream responses |
| `REQUEST_TIMEOUT` | `10` | Timeout for upstream requests |
| `PUBLIC_BASE_URL` | auto | Public URL for OPML and RSS self links |
| `CDP_URL` | `http://localhost:9222` | Chrome DevTools URL for Xueqiu |

Configuration names are public. Do not commit `.env` files, cookies, tokens,
private keys, Chrome profiles, or HAR captures.

## Xueqiu Optional Setup

Xueqiu blocks direct API requests. This bridge can fetch through a logged-in
Chrome tab via Chrome DevTools Protocol.

1. Install the optional dependency:
   ```bash
   pip install websocket-client
   ```
2. Start Chrome with remote debugging:
   ```bash
   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --remote-allow-origins=*
   ```
3. Open Xueqiu in Chrome and log in.
4. Run the bridge:
   ```bash
   CDP_URL=http://localhost:9222 python server.py
   ```

Keep the CDP port local or on a trusted private network.

## Docker

```bash
docker build -t china-finance-rss .
docker run -d -p 8053:8053 --name china-finance-rss china-finance-rss
```

Behind a reverse proxy:

```bash
docker run -d -p 8053:8053 \
  -e PUBLIC_BASE_URL=https://rss.example.com \
  --name china-finance-rss china-finance-rss
```

## Notes

- CLS, Eastmoney, THS, Jin10, and Wallstreetcn use public web endpoints.
- Xueqiu uses your local browser session but does not store or print cookies.
- If an upstream source breaks, the feed stays valid and includes a diagnostic
  item. Use `/healthz?check=1` to see which source failed.

## 中文简版

启动：

```bash
python server.py
```

打开 `http://localhost:8053/`，把页面里的 RSS 地址加到阅读器。内置源包括
财联社、东方财富、同花顺、金十、华尔街见闻和可选雪球。阅读器支持 OPML
的话，直接导入 `http://localhost:8053/opml.xml`。

如果订阅为空或不更新，打开 `http://localhost:8053/healthz?check=1` 看是哪
个上游源失败。

雪球需要 Chrome CDP 和已登录的本地浏览器标签页；不要把 CDP 端口暴露到公网，
也不要提交 cookies、tokens、`.env`、Chrome profile 或 HAR 文件。

## License

MIT
