#!/usr/bin/env bash
# 云端每日更新：抓取最新论文 -> 生成站点数据 -> 提交并推送 main。
# 私钥来自环境 secret SSH_PRIVATE_KEY；不要把私钥写进提交信息或日志。
set -euo pipefail

REPO=git@github.com:hectormeta/research_follow.git
WORK="${NR_WORK:-/tmp/research_follow}"
TZ=Asia/Shanghai
export TZ

# 1. 还原 SSH 私钥与配置（经 ssh.github.com:443）
install -m 700 -d "$HOME/.ssh"
if [ -n "${SSH_PRIVATE_KEY:-}" ]; then
  printf '%s\n' "$SSH_PRIVATE_KEY" > "$HOME/.ssh/id_research_follow"
  chmod 600 "$HOME/.ssh/id_research_follow"
fi
cat > "$HOME/.ssh/config" <<'EOF'
Host github.com
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/id_research_follow
  IdentitiesOnly yes
EOF
chmod 600 "$HOME/.ssh/config"
ssh-keyscan -p 443 ssh.github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true

# 2. 同步仓库（全新 clone 或重置到远端 main）
if [ -d "$WORK/.git" ]; then
  git -C "$WORK" fetch origin
  git -C "$WORK" reset --hard origin/main
  git -C "$WORK" clean -fdx -e .cache || true
else
  git clone "$REPO" "$WORK"
fi
cd "$WORK"

# 3. 抓取并生成数据（有 ANTHROPIC_API_KEY 时自动生成中文要点，否则关键词兜底）
python3 scripts/update.py
python3 scripts/update_news.py || true

# 4. 提交并推送
git config user.name  "hectormeta"
git config user.email "hectormeta@users.noreply.github.com"
git add data.json summaries.json trends.json authors.json thumbs.json archive x news/data.json
if ! git diff --cached --quiet; then
  git commit -m "Update papers $(date +%F) (cloud)"
  git pull --rebase -X theirs origin main || true
  git push origin main
  echo "push done"
else
  echo "no changes to commit"
fi
