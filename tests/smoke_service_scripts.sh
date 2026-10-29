#!/usr/bin/env bash
# 服务脚本解释器选择与依赖检测冒烟测试（不启动真实服务）
set -eu

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$TESTS_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

assert_eq() {
    [ "$1" = "$2" ] || fail "expected [$2], got [$1]"
}

make_python() {
    local path="$1"
    local exit_code="$2"
    mkdir -p "$(dirname "$path")"
    printf '#!/usr/bin/env bash\nexit %s\n' "$exit_code" >"$path"
    chmod +x "$path"
}

# 加载脚本但不要执行 start/status 等命令
# shellcheck disable=SC1090
source "$PROJECT_DIR/scripts/mcp_service.sh"

# 1. Windows 虚拟环境布局：venv\Scripts\python.exe
WIN_PROJECT="$TMP_DIR/win-project"
make_python "$WIN_PROJECT/venv/Scripts/python.exe" 0
assert_eq "$(resolve_python "$WIN_PROJECT" windows)" "$WIN_PROJECT/venv/Scripts/python.exe"

# 2. POSIX 虚拟环境布局：.venv/bin/python
UNIX_PROJECT="$TMP_DIR/unix-project"
make_python "$UNIX_PROJECT/.venv/bin/python" 0
assert_eq "$(resolve_python "$UNIX_PROJECT" unix)" "$UNIX_PROJECT/.venv/bin/python"

# 3. Windows 无虚拟环境时优先 python，避免命中应用商店的 python3 占位程序
EMPTY_PROJECT="$TMP_DIR/empty-project"
mkdir -p "$EMPTY_PROJECT" "$TMP_DIR/bin"
make_python "$TMP_DIR/bin/python" 0
make_python "$TMP_DIR/bin/python3" 1
OLD_PATH="$PATH"
PATH="$TMP_DIR/bin:$PATH"
assert_eq "$(resolve_python "$EMPTY_PROJECT" windows)" "$TMP_DIR/bin/python"
# POSIX 环境仍优先 python3
assert_eq "$(resolve_python "$EMPTY_PROJECT" unix)" "$TMP_DIR/bin/python3"
PATH="$OLD_PATH"

# 4. 依赖检测：能 import oss2/mcp/uvicorn 才算可用
make_python "$TMP_DIR/good-python" 0
make_python "$TMP_DIR/bad-python" 1
if ! python_has_dependencies "$TMP_DIR/good-python"; then
    fail "good-python should be treated as having dependencies"
fi
if python_has_dependencies "$TMP_DIR/bad-python"; then
    fail "bad-python should be treated as missing dependencies"
fi

# 5. 项目 venv 缺依赖时，自动跳过并回退到 PATH 中依赖齐全的 python
BROKEN_PROJECT="$TMP_DIR/broken-project"
make_python "$BROKEN_PROJECT/venv/Scripts/python.exe" 1
PATH="$TMP_DIR/bin:$PATH"
assert_eq "$(select_python "$BROKEN_PROJECT" windows 2>/dev/null)" "$TMP_DIR/bin/python"
PATH="$OLD_PATH"

# 6. 所有解释器都缺依赖时，自动为项目 venv 安装依赖后再使用
AUTO_PROJECT="$TMP_DIR/auto-project"
AUTO_VENV="$AUTO_PROJECT/venv/Scripts/python.exe"
mkdir -p "$(dirname "$AUTO_VENV")" "$TMP_DIR/no-python-bin"
printf 'mcp\n' >"$AUTO_PROJECT/requirements.txt"
cat >"$AUTO_VENV" <<EOF
#!/usr/bin/env bash
state="$TMP_DIR/auto-installed"
if [ "\${1:-}" = "-c" ]; then
    [ -f "\$state" ] && exit 0
    exit 1
fi
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "pip" ]; then
    touch "\$state"
    exit 0
fi
exit 1
EOF
chmod +x "$AUTO_VENV"
PATH="$TMP_DIR/no-python-bin:/usr/bin:/bin"
unset OSS_MCP_AUTO_INSTALL
AUTO_RESULT="$(ensure_python "$AUTO_PROJECT" windows 2>/dev/null || true)"
AUTO_INSTALLED=0
[ -f "$TMP_DIR/auto-installed" ] && AUTO_INSTALLED=1
PATH="$OLD_PATH"
assert_eq "$AUTO_RESULT" "$AUTO_VENV"
[ "$AUTO_INSTALLED" = "1" ] || fail "dependencies were not auto-installed"

# 7. 关闭自动安装时应报告失败，而不是偷偷安装
rm -f "$TMP_DIR/auto-installed"
PATH="$TMP_DIR/no-python-bin:/usr/bin:/bin"
if OSS_MCP_AUTO_INSTALL=0 ensure_python "$AUTO_PROJECT" windows >/dev/null 2>&1; then
    DISABLED_RESULT=1
else
    DISABLED_RESULT=0
fi
PATH="$OLD_PATH"
[ "$DISABLED_RESULT" = "0" ] || fail "ensure_python should fail when auto install is disabled"

# 8. PYTHON_BIN 支持直接写命令名（如 python），而不仅是绝对路径
PATH="$TMP_DIR/bin:$PATH"
PYTHON_BIN=python
COMMAND_RESULT="$(select_python "$EMPTY_PROJECT" windows 2>/dev/null || true)"
unset PYTHON_BIN
PATH="$OLD_PATH"
assert_eq "$COMMAND_RESULT" "$TMP_DIR/bin/python"

printf 'SERVICE SCRIPT TESTS PASSED\n'
