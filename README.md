# 微纳光学新文献

每天自动汇总超表面、超材料、拓扑光学、光场调控、微纳光学、传感、光计算、二维材料、片上光学、计算成像、非线性与量子光子学方向的新论文：arXiv 预印本，加上 53 种期刊（经 Crossref，通讯作者来自 OpenAlex）。每篇论文有主题标签和中文一句话要点，访客可以在自己的浏览器里收藏、标记已读。

这是一个纯静态网站，用 GitHub Pages 免费托管，GitHub Actions 每天北京时间 8:30 抓取一次数据并重新发布。

## 部署（约 10 分钟）

1. 在 GitHub 新建一个公开仓库，例如 `nano-radar`。
2. 把本文件夹里的全部内容上传到仓库根目录（包括隐藏的 `.github` 文件夹和 `.nojekyll`）。网页上传会忽略以点开头的文件夹，建议用命令行：
   ```bash
   cd nano-radar-site
   git init -b main
   git add .
   git commit -m "init"
   git remote add origin https://github.com/<你的用户名>/nano-radar.git
   git push -u origin main
   ```
3. 仓库 Settings → Pages → Build and deployment → Source 选 **GitHub Actions**。
4. 仓库 Settings → Secrets and variables → Actions：
   - 在 Secrets 里新建 `ANTHROPIC_API_KEY`，填入你的 Claude API key（用于生成中文要点和主题判断）。
   - 可选：在 Variables 里新建 `CLAUDE_MODEL` 指定模型，默认 `claude-haiku-4-5`。
5. Actions 页面 → Update papers → Run workflow，手动跑第一次。完成后网站地址是 `https://<你的用户名>.github.io/nano-radar/`。

之后每天自动更新，不需要再操作。

## 费用与说明

- 不设置 `ANTHROPIC_API_KEY` 也能运行，只是没有中文要点，主题标签退回到关键词规则。
- 每篇论文只总结一次，结果缓存在 `summaries.json`。首次运行约 1000 篇，之后每天几十篇，用 Haiku 级别模型费用很低。
- 期刊摘要只作为模型输入，不会发布到网站上（版权属于出版社）；网站上展示的是标题、作者、中文要点和原文链接。arXiv 摘要会展示。
- GitHub Pages 在中国大陆通常可以访问，但速度不稳定。需要的话可以把同一个仓库再连接到 Cloudflare Pages 或 Vercel 做镜像，构建命令留空、输出目录填 `/` 即可（数据仍由 GitHub Actions 每天提交更新）。

## 自定义

- 期刊列表、arXiv 检索式、关键词规则和主题颜色都在 `config.json` 里，改完推送即可生效。
- 本地预览：`python3 scripts/update.py && python3 -m http.server`，然后打开 http://localhost:8000 。

## 页面

- `index.html`：文献列表（缩略图、通讯作者、中文要点、收藏/已读）。
- `explore.html`：趋势与探索（主题趋势、升温关键词、来源→主题→关键词流向图、关键词共现网络、自由输入关键词对比），用开源 Apache ECharts 绘制。
- 数据文件：`data.json`（近 31 天论文）、`trends.json`（逐日累积的主题/关键词计数）、`summaries.json`、`authors.json`、`thumbs.json`（缓存）。
