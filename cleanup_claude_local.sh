#!/bin/bash
# Claude Code 旧账号本地缓存清理脚本
# 用法：
#   ./cleanup_claude_local.sh          — 干跑，只看不删
#   ./cleanup_claude_local.sh --run    — 真删

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

DRY_RUN=true
[[ "${1:-}" == "--run" ]] && DRY_RUN=false

deleted=0
skipped=0

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
found() { echo -e "${GREEN}[FOUND]${NC} $1"; }
skip()  { echo -e "${YELLOW}[SKIP]${NC} $1"; skipped=$((skipped+1)); }
del()   { echo -e "${RED}[DELETE]${NC} $1"; deleted=$((deleted+1)); }

preflight_check() {
    local blocked=false

    if pgrep -f "claude" > /dev/null 2>&1; then
        warn "检测到 claude 相关进程还在跑："
        pgrep -af "claude" 2>/dev/null || true
        warn "建议先退出 Claude Code Desktop 和 VS Code Claude 扩展"
        blocked=true
    fi

    if pgrep -f "Google Chrome" > /dev/null 2>&1; then
        warn "Chrome 还在运行，IndexedDB 可能删不干净"
        warn "建议关掉所有 claude.ai 标签页，或者直接退出 Chrome"
    fi

    if $blocked && ! $DRY_RUN; then
        echo ""
        read -p "Claude 进程还在跑，确定继续吗？(y/N) " confirm
        [[ "$confirm" != "y" && "$confirm" != "Y" ]] && { echo "已取消"; exit 0; }
    fi
}

remove_target() {
    local path="$1"
    local desc="$2"
    local expanded
    expanded=$(eval echo "$path")

    if [[ -e "$expanded" ]]; then
        if [[ -d "$expanded" ]]; then
            local count
            count=$(find "$expanded" -type f 2>/dev/null | wc -l | tr -d ' ')
            found "$desc: $expanded ($count 个文件)"
        else
            local size
            size=$(du -h "$expanded" 2>/dev/null | cut -f1)
            found "$desc: $expanded ($size)"
        fi
        if $DRY_RUN; then
            info "  → 干跑模式，不删除"
        else
            rm -rf "$expanded"
            del "$desc: $expanded"
        fi
    else
        skip "$desc: $expanded（不存在）"
    fi
}

remove_glob() {
    local pattern="$1"
    local desc="$2"
    local expanded
    expanded=$(eval echo "$pattern")

    local matches=()
    for f in $expanded; do
        [[ -e "$f" ]] && matches+=("$f")
    done

    if [[ ${#matches[@]} -eq 0 ]]; then
        skip "$desc（未找到匹配: $pattern）"
        return
    fi

    for f in "${matches[@]}"; do
        if [[ -d "$f" ]]; then
            local count
            count=$(find "$f" -type f 2>/dev/null | wc -l | tr -d ' ')
            found "$desc: $f ($count 个文件)"
        else
            found "$desc: $f"
        fi
        if $DRY_RUN; then
            info "  → 干跑模式，不删除"
        else
            rm -rf "$f"
            del "$desc: $f"
        fi
    done
}

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Claude Code 旧账号痕迹清理"
if $DRY_RUN; then
    echo -e " 模式: ${YELLOW}干跑（只看不删）${NC}"
    echo " 确认无误后加 --run 参数真正执行"
else
    echo -e " 模式: ${RED}正式执行（不可恢复）${NC}"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

preflight_check

echo ""
echo "── 1. 账号身份文件 ──"
remove_target "~/.claude.json" "账号身份文件（userID/设备指纹）"

echo ""
echo "── 2. Claude 数据目录 ──"
remove_target "~/.claude/usage-data" "使用统计（token用量）"
remove_target "~/.claude/session-env" "会话环境快照"
remove_target "~/.claude/debug" "调试日志"
remove_target "~/.claude/file-history" "文件编辑记录"
remove_target "~/.claude/projects" "项目缓存"
remove_target "~/.claude/plans" "执行计划"
remove_target "~/.claude/history.jsonl" "命令历史"
remove_target "~/.claude/statsig" "分析数据"
remove_target "~/.claude/todos" "待办事项"
remove_target "~/.claude/credentials.json" "凭证文件"

echo ""
echo "── 3. Chrome IndexedDB（claude.ai 登录缓存）──"
for chrome_profile in ~/Library/Application\ Support/Google/Chrome/Default ~/Library/Application\ Support/Google/Chrome/Profile\ *; do
    if [[ -d "$chrome_profile" ]]; then
        idb_path="$chrome_profile/IndexedDB/https_claude.ai_0.indexeddb.leveldb"
        if [[ -d "$idb_path" ]]; then
            remove_target "\"$idb_path\"" "Chrome IndexedDB（claude.ai）"
        fi
    fi
done
# 兜底用glob
remove_glob "~/Library/Application\\ Support/Google/Chrome/*/IndexedDB/https_claude.ai_0.indexeddb.leveldb" "Chrome IndexedDB（claude.ai）"

echo ""
echo "── 4. VS Code Claude 扩展日志 ──"
remove_glob "~/Library/Application\\ Support/Code/logs/*/Anthropic.claude-code" "VS Code Claude 扩展日志"

echo ""
echo "── 5. VS Code 扩展 VSIX 缓存 ──"
remove_glob "~/.vscode/extensions/anthropic.claude-code-*" "VS Code Claude 扩展安装"

echo ""
echo "── 6. CLI 运行缓存 ──"
remove_target "~/Library/Caches/claude-cli-nodejs" "CLI Node.js 缓存"

echo ""
echo "── 7. 其他可能的残留 ──"
remove_target "~/.claude/settings.json" "settings.json（确认已备份再删）"
remove_target "~/.claude/settings.local.json" "settings.local.json"
remove_glob "/tmp/claude-*" "/tmp 下的 claude 临时文件"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " 清理完成"
echo -e " 已删除: ${RED}$deleted${NC} 项"
echo -e " 已跳过: ${YELLOW}$skipped${NC} 项（不存在）"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if $DRY_RUN; then
    echo ""
    echo "这是干跑结果。确认没问题后运行："
    echo "  ./cleanup_claude_local.sh --run"
fi

if ! $DRY_RUN && [[ -d ~/.claude ]]; then
    remaining=$(find ~/.claude -type f 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$remaining" -gt 0 ]]; then
        warn "~/.claude/ 下还剩 $remaining 个文件"
        warn "如果要彻底删除整个目录: rm -rf ~/.claude"
    fi
fi
