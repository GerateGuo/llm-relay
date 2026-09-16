#!/bin/bash
# rollback.sh —— Hindsight 集成示例：一键把 Hindsight 切回 StepFun
#
# 这是 examples/hindsight 里的**示例脚本**，默认不启用；启用方式见同目录 README.md。
# 它只依赖 integrations.upstream 里配置的 start_script（默认见下方 TARGET），与中继本体解耦。
#
# 做的事：
#   1. 把 ~/.hermes/scripts/hindsight-mac-start.sh 还原成切换前的备份（.bak.<时间戳>，默认取最新一个）
#   2. launchctl kickstart -k gui/$(id -u)/com.user.hindsight-mac
#   3. 等 /health 200（最多 60s），并打印恢复后的 LLM 四行（**不含 key 明文**）
#
# 用法：
#   examples/hindsight/rollback.sh                  # 还原最新备份并重启 Hindsight
#   examples/hindsight/rollback.sh <备份文件>        # 还原指定备份
#   examples/hindsight/rollback.sh --dry-run        # 只看会做什么，不动文件
#   examples/hindsight/rollback.sh --no-restart     # 只还原文件，不重启 Hindsight
#   HINDSIGHT_START_SH=/path/to/start.sh ...        # 指向别的脚本（自测用）
set -u

TARGET="${HINDSIGHT_START_SH:-$HOME/.hermes/scripts/hindsight-mac-start.sh}"
TARGET_DIR="$(cd "$(dirname "$TARGET")" && pwd)"
TARGET_NAME="$(basename "$TARGET")"
DRY_RUN=0
DO_RESTART=1
EXPLICIT_BACKUP=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-restart) DO_RESTART=0 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) EXPLICIT_BACKUP="$arg" ;;
  esac
done

if [ -n "$EXPLICIT_BACKUP" ]; then
  BACKUP="$EXPLICIT_BACKUP"
else
  BACKUP="$(ls -1t "$TARGET_DIR/$TARGET_NAME".bak.* 2>/dev/null | head -1)"
fi

if [ -z "${BACKUP:-}" ] || [ ! -f "$BACKUP" ]; then
  echo "❌ 没找到可用备份（$TARGET_DIR/$TARGET_NAME.bak.*）"
  echo "   请手动指定：$0 <备份文件>"
  exit 1
fi

echo "目标脚本：$TARGET"
echo "还原备份：$BACKUP"
if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] 会执行：cp -p \"$BACKUP\" \"$TARGET\" && chmod 711 \"$TARGET\""
  [ "$DO_RESTART" = "1" ] && echo "[dry-run] 会执行：launchctl kickstart -k gui/$(id -u)/com.user.hindsight-mac"
  echo "[dry-run] 备份里的 LLM 四行（不含 key 明文）："
  grep -E "HINDSIGHT_API_LLM_(PROVIDER|MODEL|BASE_URL|API_KEY)=" "$BACKUP" \
    | sed -E 's/(sk-|nvapi-)[A-Za-z0-9_-]+/\1***/g' | sed 's/^/    /'
  exit 0
fi

# 还原前把当前版本也留一份（不删任何既有备份）
cp -p "$TARGET" "$TARGET.bak.before-rollback.$(date +%Y%m%d%H%M%S)"
cp -p "$BACKUP" "$TARGET"
chmod 711 "$TARGET"
echo "✅ 已还原 $TARGET_NAME"

if [ "$DO_RESTART" = "1" ]; then
  launchctl kickstart -k "gui/$(id -u)/com.user.hindsight-mac"
  echo "🔄 已 kickstart com.user.hindsight-mac，等待 /health 就绪（最多 60s）..."
  ok=0
  for i in $(seq 1 60); do
    if curl -sf --noproxy '*' -o /dev/null -m 2 http://127.0.0.1:8988/health; then
      echo "✅ Hindsight /health 200（等待 ${i}s）"
      ok=1
      break
    fi
    sleep 1
  done
  [ "$ok" = "1" ] || echo "⚠️  60s 内 /health 未就绪，请查 ~/.hermes/hindsight-data/launchd-stdout.log"
fi

echo "恢复后的 LLM 四行（不含 key 明文）："
grep -E "HINDSIGHT_API_LLM_(PROVIDER|MODEL|BASE_URL|API_KEY)=" "$TARGET" \
  | sed -E 's/(sk-|nvapi-)[A-Za-z0-9_-]+/\1***/g' | sed 's/^/    /'
