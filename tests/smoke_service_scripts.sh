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
    printf '%s\n' "\$*" >>"$TMP_DIR/auto-pip.log"
    if [ "\${3:-}" = "install" ] && [ "\${4:-}" = "--upgrade" ]; then
        exit 0
    fi
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
assert_eq "$(sed -n '1p' "$TMP_DIR/auto-pip.log")" "-m pip install --upgrade pip"
case "$(sed -n '2p' "$TMP_DIR/auto-pip.log")" in
    "-m pip install -r "*) ;;
    *) fail "existing venv should upgrade pip before installing requirements" ;;
esac

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

# 9. 没有虚拟环境时自动创建 .venv，先升级 pip 再安装依赖
VENV_PROJECT="$TMP_DIR/venv-project"
VENV_BIN="$TMP_DIR/venv-bin"
mkdir -p "$VENV_PROJECT" "$VENV_BIN"
printf 'mcp\n' >"$VENV_PROJECT/requirements.txt"

cat >"$TMP_DIR/fake-venv-python" <<EOF
#!/usr/bin/env bash
log="$TMP_DIR/venv-pip.log"
if [ "\${1:-}" = "-c" ]; then
    [ -f "$TMP_DIR/venv-installed" ] && exit 0
    exit 1
fi
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "pip" ]; then
    printf '%s\n' "\$*" >>"\$log"
    if [ "\${3:-}" = "install" ] && [ "\${4:-}" = "--upgrade" ]; then
        exit 0
    fi
    touch "$TMP_DIR/venv-installed"
    exit 0
fi
exit 1
EOF
chmod +x "$TMP_DIR/fake-venv-python"

cat >"$VENV_BIN/python3" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "-c" ]; then exit 0; fi
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "venv" ]; then
    if [ "\${3:-}" = "--help" ]; then exit 0; fi
    target="\${3:-}"
    mkdir -p "\$target/bin"
    cp "$TMP_DIR/fake-venv-python" "\$target/bin/python"
    chmod +x "\$target/bin/python"
    exit 0
fi
exit 1
EOF
chmod +x "$VENV_BIN/python3"

PATH="$VENV_BIN:/usr/bin:/bin"
unset OSS_MCP_AUTO_INSTALL
VENV_RESULT="$(ensure_python "$VENV_PROJECT" unix 2>/dev/null || true)"
PATH="$OLD_PATH"
assert_eq "$VENV_RESULT" "$VENV_PROJECT/.venv/bin/python"
[ -d "$VENV_PROJECT/.venv" ] || fail "project venv was not created"
assert_eq "$(sed -n '1p' "$TMP_DIR/venv-pip.log")" "-m pip install --upgrade pip"
case "$(sed -n '2p' "$TMP_DIR/venv-pip.log")" in
    "-m pip install -r "*) ;;
    *) fail "requirements were not installed after pip upgrade" ;;
esac

# 10. 依赖齐全的项目虚拟环境不应重复升级 pip 或安装依赖
HEALTHY_PROJECT="$TMP_DIR/healthy-project"
HEALTHY_VENV="$HEALTHY_PROJECT/venv/Scripts/python.exe"
mkdir -p "$(dirname "$HEALTHY_VENV")"
cat >"$HEALTHY_VENV" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = "-c" ]; then exit 0; fi
if [ "\${1:-}" = "-m" ] && [ "\${2:-}" = "pip" ]; then
    touch "$TMP_DIR/healthy-pip-called"
    exit 0
fi
exit 1
EOF
chmod +x "$HEALTHY_VENV"
rm -f "$TMP_DIR/healthy-pip-called"
PATH="$TMP_DIR/no-python-bin:/usr/bin:/bin"
HEALTHY_RESULT="$(ensure_python "$HEALTHY_PROJECT" windows 2>/dev/null || true)"
PATH="$OLD_PATH"
assert_eq "$HEALTHY_RESULT" "$HEALTHY_VENV"
[ ! -f "$TMP_DIR/healthy-pip-called" ] || fail "healthy venv should not run pip"

# 11. 关闭自动安装时不应创建虚拟环境，可回退到 PATH 中依赖齐全的 Python
DISABLED_PROJECT="$TMP_DIR/disabled-project"
mkdir -p "$DISABLED_PROJECT"
PATH="$TMP_DIR/bin:$PATH"
DISABLED_CREATE_RESULT="$(OSS_MCP_AUTO_INSTALL=0 ensure_python "$DISABLED_PROJECT" windows 2>/dev/null || true)"
PATH="$OLD_PATH"
assert_eq "$DISABLED_CREATE_RESULT" "$TMP_DIR/bin/python"
[ ! -d "$DISABLED_PROJECT/.venv" ] || fail "auto install disabled should not create a venv"

# 12. PYTHON_BIN 是严格模式：依赖不合格时直接失败，不创建虚拟环境
STRICT_PROJECT="$TMP_DIR/strict-project"
mkdir -p "$STRICT_PROJECT"
PATH="$VENV_BIN:/usr/bin:/bin"
PYTHON_BIN="$TMP_DIR/bad-python"
if ensure_python "$STRICT_PROJECT" unix >/dev/null 2>&1; then
    PATH="$OLD_PATH"
    unset PYTHON_BIN
    fail "ensure_python should fail for unusable PYTHON_BIN"
fi
unset PYTHON_BIN
PATH="$OLD_PATH"
[ ! -d "$STRICT_PROJECT/.venv" ] || fail "PYTHON_BIN should not create a venv"

# 13. Windows PowerShell 脚本保持相同流程（存在 powershell.exe 时执行）
if command -v powershell.exe >/dev/null 2>&1; then
    PS_TEST="$TESTS_DIR/smoke_service_scripts.ps1"
    if command -v cygpath >/dev/null 2>&1; then
        PS_TEST="$(cygpath -w "$PS_TEST")"
    fi
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PS_TEST" \
        || fail "PowerShell service script tests failed"
fi

printf 'SERVICE SCRIPT TESTS PASSED\n'
