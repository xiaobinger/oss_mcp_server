#!/usr/bin/env bash
# OSS MCP 服务管理脚本（Linux / macOS）
#
# 用法（在项目根目录执行）：
#   ./scripts/mcp_service.sh start     启动服务（后台运行，默认 streamable-http）
#   ./scripts/mcp_service.sh stop      停止服务
#   ./scripts/mcp_service.sh restart   重启服务
#   ./scripts/mcp_service.sh status    查看运行状态
#   ./scripts/mcp_service.sh logs      实时查看日志（Ctrl+C 退出）
#
# 首次使用：
#   cp config/oss_config.example.json config/oss_config.json    # 按需修改分组和 AccessKey
#   chmod +x scripts/mcp_service.sh
#
# 启动前会自动准备运行环境：项目虚拟环境不存在时用系统 Python 创建 .venv，
# 缺依赖时先升级 pip 再安装 requirements.txt，最后启动或重启服务。
#
# 可选环境变量：
#   PYTHON_BIN          指定 Python 解释器（严格模式：必须是 Python 3.10+ 且已安装
#                       oss2 / mcp / uvicorn，不会自动创建虚拟环境或安装依赖。
#                       Windows 为 .venv\Scripts\python.exe / venv\Scripts\python.exe，
#                       其他系统为 .venv/bin/python / venv/bin/python）
#   OSS_MCP_AUTO_INSTALL 设为 0 可禁止自动创建 .venv、升级 pip 和安装缺失依赖（默认 1）
#   OSS_MCP_CONFIG      配置文件路径（默认 <项目目录>/config/oss_config.json）
#   OSS_MCP_HOST        监听地址（默认 127.0.0.1）
#   OSS_MCP_PORT        监听端口（默认 8000）
#   OSS_MCP_TRANSPORT   streamable-http（默认）或 sse
#   OSS_MCP_BEARER_TOKEN HTTP/SSE 模式的 Bearer Token；监听非本机地址时必填

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_SCRIPT="$PROJECT_DIR/src/oss_mcp_server.py"
CONFIG_FILE="${OSS_MCP_CONFIG:-$PROJECT_DIR/config/oss_config.json}"
HOST="${OSS_MCP_HOST:-127.0.0.1}"
PORT="${OSS_MCP_PORT:-8000}"
TRANSPORT="${OSS_MCP_TRANSPORT:-streamable-http}"
LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/run"
PID_FILE="$RUN_DIR/mcp.pid"
LOG_FILE="$LOG_DIR/mcp.log"

die() {
    printf '错误：%s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
OSS MCP 服务管理脚本

用法（在项目根目录执行）：./scripts/$(basename "$0") {start|stop|restart|status|logs}

  start     启动服务（后台运行）
  stop      停止服务
  restart   重启服务
  status    查看运行状态
  logs      实时查看日志（Ctrl+C 退出）

当前配置：
  服务脚本  $SERVER_SCRIPT
  配置文件  $CONFIG_FILE
  监听地址  $HOST:$PORT
  传输方式  $TRANSPORT
  日志文件  $LOG_FILE

启动前会自动准备项目虚拟环境：不存在时创建 .venv，缺依赖时先升级 pip 再安装 requirements.txt。

可选环境变量：PYTHON_BIN、OSS_MCP_CONFIG、OSS_MCP_HOST、OSS_MCP_PORT、OSS_MCP_TRANSPORT、
              OSS_MCP_BEARER_TOKEN（监听非本机地址时必填）、OSS_MCP_AUTO_INSTALL（默认 1）
EOF
}

detect_platform() {
    case "$(uname -s 2>/dev/null || printf 'unknown')" in
        MINGW*|MSYS*|CYGWIN*) printf 'windows\n' ;;
        *) printf 'unix\n' ;;
    esac
}

python_candidates() {
    local project_dir="${1:-$PROJECT_DIR}"
    local platform="${2:-$(detect_platform)}"
    local candidate
    local -a venv_candidates

    if [ "$platform" = "windows" ]; then
        venv_candidates=(
            "$project_dir/.venv/Scripts/python.exe"
            "$project_dir/venv/Scripts/python.exe"
        )
    else
        venv_candidates=(
            "$project_dir/.venv/bin/python"
            "$project_dir/venv/bin/python"
        )
    fi

    for candidate in "${venv_candidates[@]}"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
        fi
    done
    system_python_candidates "$platform"
}

system_python_candidates() {
    local platform="${1:-$(detect_platform)}"
    local candidate
    local -a command_candidates

    if [ "$platform" = "windows" ]; then
        # Windows 应用商店的 python3 常是无依赖的占位程序，优先使用 python
        command_candidates=(python python3)
    else
        command_candidates=(python3 python)
    fi
    for candidate in "${command_candidates[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
        fi
    done
}

resolve_python() {
    local project_dir="${1:-$PROJECT_DIR}"
    local platform="${2:-$(detect_platform)}"
    if [ -n "${PYTHON_BIN:-}" ]; then
        printf '%s\n' "$PYTHON_BIN"
        return 0
    fi
    local candidate
    candidate="$(python_candidates "$project_dir" "$platform" | head -n 1 || true)"
    [ -n "$candidate" ] || return 1
    printf '%s\n' "$candidate"
}

python_has_dependencies() {
    local python="${1:-}"
    [ -n "$python" ] || return 1
    if [ ! -x "$python" ]; then
        python="$(command -v "$python" 2>/dev/null || true)"
    fi
    [ -n "$python" ] && [ -x "$python" ] || return 1
    "$python" -c 'import sys, oss2, mcp, uvicorn; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

preferred_venv_python() {
    local project_dir="${1:-$PROJECT_DIR}"
    local platform="${2:-$(detect_platform)}"
    local candidate
    local -a venv_candidates

    if [ "$platform" = "windows" ]; then
        venv_candidates=(
            "$project_dir/.venv/Scripts/python.exe"
            "$project_dir/venv/Scripts/python.exe"
        )
    else
        venv_candidates=(
            "$project_dir/.venv/bin/python"
            "$project_dir/venv/bin/python"
        )
    fi
    for candidate in "${venv_candidates[@]}"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

python_supports_venv() {
    local python="${1:-}"
    [ -n "$python" ] || return 1
    if [ ! -x "$python" ]; then
        python="$(command -v "$python" 2>/dev/null || true)"
    fi
    [ -n "$python" ] && [ -x "$python" ] || return 1
    "$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || return 1
    "$python" -m venv --help >/dev/null 2>&1
}

create_project_venv() {
    local project_dir="${1:-$PROJECT_DIR}"
    local platform="${2:-$(detect_platform)}"
    local venv_dir="$project_dir/.venv"
    local venv_python candidate

    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        python_supports_venv "$candidate" || continue
        printf '未找到项目虚拟环境，正在使用 %s 创建 %s ...\n' "$candidate" "$venv_dir" >&2
        if "$candidate" -m venv "$venv_dir" >&2; then
            venv_python="$(preferred_venv_python "$project_dir" "$platform" || true)"
            if [ -n "$venv_python" ]; then
                printf '%s\n' "$venv_python"
                return 0
            fi
        fi
        printf '创建虚拟环境失败，继续尝试其他 Python。\n' >&2
    done < <(system_python_candidates "$platform")
    return 1
}

select_python() {
    local project_dir="${1:-$PROJECT_DIR}"
    local platform="${2:-$(detect_platform)}"
    local candidate

    if [ -n "${PYTHON_BIN:-}" ]; then
        local requested="$PYTHON_BIN"
        if [ ! -x "$requested" ]; then
            requested="$(command -v "$requested" 2>/dev/null || true)"
        fi
        if [ -n "$requested" ] && python_has_dependencies "$requested"; then
            printf '%s\n' "$requested"
            return 0
        fi
        printf '错误：PYTHON_BIN 指定的解释器不是 Python 3.10+ 或无法导入 oss2 / mcp / uvicorn：%s\n' "$PYTHON_BIN" >&2
        return 1
    fi

    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        if python_has_dependencies "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
        printf '提示：%s 不是 Python 3.10+ 或缺少依赖（oss2 / mcp / uvicorn），跳过。\n' "$candidate" >&2
    done < <(python_candidates "$project_dir" "$platform")
    return 1
}

upgrade_pip() {
    local python="$1"
    printf '正在升级 pip（%s）...\n' "$python" >&2
    "$python" -m pip install --upgrade pip >&2
}

install_python_requirements() {
    local python="$1"
    local requirements="${2:-$PROJECT_DIR/requirements.txt}"
    if [ ! -f "$requirements" ]; then
        printf '未找到依赖清单：%s\n' "$requirements" >&2
        return 1
    fi
    printf '正在为 %s 安装依赖（%s）...\n' "$python" "$requirements" >&2
    "$python" -m pip install -r "$requirements" >&2
}

ensure_python() {
    local project_dir="${1:-$PROJECT_DIR}"
    local platform="${2:-$(detect_platform)}"
    local auto_install="${OSS_MCP_AUTO_INSTALL:-1}"
    local python venv_python

    # PYTHON_BIN 是严格模式：只校验，不创建虚拟环境，也不修改其依赖
    if [ -n "${PYTHON_BIN:-}" ]; then
        if python="$(select_python "$project_dir" "$platform")"; then
            printf '%s\n' "$python"
            return 0
        fi
        return 1
    fi

    venv_python="$(preferred_venv_python "$project_dir" "$platform" || true)"
    if [ -z "$venv_python" ] && [ "$auto_install" != "0" ]; then
        venv_python="$(create_project_venv "$project_dir" "$platform" || true)"
    fi

    if [ -n "$venv_python" ]; then
        if python_has_dependencies "$venv_python"; then
            printf '%s\n' "$venv_python"
            return 0
        fi
        if [ "$auto_install" != "0" ]; then
            printf '提示：%s 缺少依赖，正在自动安装。\n' "$venv_python" >&2
            upgrade_pip "$venv_python" || printf '提示：pip 升级失败，继续安装依赖。\n' >&2
            if install_python_requirements "$venv_python" "$project_dir/requirements.txt" \
                && python_has_dependencies "$venv_python"; then
                printf '%s\n' "$venv_python"
                return 0
            fi
            printf '提示：项目虚拟环境依赖安装后仍不可用，尝试 PATH 中的其他 Python。\n' >&2
        fi
    fi

    if python="$(select_python "$project_dir" "$platform")"; then
        printf '%s\n' "$python"
        return 0
    fi
    return 1
}

PYTHON_ANY="$(resolve_python || true)"

check_transport() {
    case "$TRANSPORT" in
        streamable-http|sse) ;;
        stdio)
            die "stdio 模式由 MCP 客户端直接拉起，不能作为后台服务运行；请使用 streamable-http 或 sse。"
            ;;
        *)
            die "不支持的 transport：$TRANSPORT（可选 streamable-http / sse）"
            ;;
    esac
}

port_open() {
    [ -n "${PYTHON_ANY:-}" ] || return 1
    "$PYTHON_ANY" - "$1" "$2" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
if host in ("", "0.0.0.0"):
    host = "127.0.0.1"
elif host == "::":
    host = "::1"
family = socket.AF_INET6 if ":" in host else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
sock.settimeout(0.5)
try:
    sock.connect((host, port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

process_args() {
    local pid="$1"
    # GNU / BSD ps；Git Bash 等环境的 ps 不支持 -o，退回 -f 取完整命令行
    ps -p "$pid" -o args= 2>/dev/null && return 0
    ps -p "$pid" -f 2>/dev/null | tail -n +2
}

service_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null || return 1
    local args
    args="$(process_args "$pid" || true)"
    case "$args" in
        *oss_mcp_server.py*) printf '%s\n' "$pid"; return 0 ;;
    esac
    return 1
}

find_running_pid() {
    local pid
    if pid="$(service_pid)"; then
        printf '%s\n' "$pid"
        return 0
    fi
    if command -v pgrep >/dev/null 2>&1; then
        pid="$(pgrep -f "oss_mcp_server\.py.*--port[ =]$PORT" 2>/dev/null | head -n 1 || true)"
        if [ -n "$pid" ]; then
            printf '%s\n' "$pid"
            return 0
        fi
    fi
    return 1
}

print_url() {
    local path="/mcp" shown="$HOST"
    [ "$TRANSPORT" = "sse" ] && path="/sse"
    [ "$HOST" = "0.0.0.0" ] && shown="127.0.0.1"
    printf '地址：http://%s:%s%s（监听 %s，transport=%s）\n' "$shown" "$PORT" "$path" "$HOST" "$TRANSPORT"
}

start_service() {
    [ -f "$SERVER_SCRIPT" ] || die "未找到服务脚本：$SERVER_SCRIPT"
    [ -f "$CONFIG_FILE" ] || die "未找到配置文件：$CONFIG_FILE
  请先执行：cp \"$PROJECT_DIR/config/oss_config.example.json\" \"$CONFIG_FILE\"
  然后按需修改分组、bucket 和 AccessKey。"

    local pid python_bin
    if pid="$(find_running_pid)"; then
        printf '服务已在运行（PID %s）。\n' "$pid"
        print_url
        return 0
    fi

    rm -f "$PID_FILE"

    if port_open "$HOST" "$PORT"; then
        die "端口 $PORT 已被其他进程占用，请更换 OSS_MCP_PORT 或先释放端口。"
    fi

    python_bin="$(ensure_python || true)"
    if [ -z "$python_bin" ]; then
        die "未找到依赖齐全的 Python（需要 Python 3.10+ 及 oss2、mcp、uvicorn）。
  已尝试创建 .venv、升级 pip 并安装 requirements.txt，也检查了 PATH 中的 Python。
  如仍失败，请检查网络后手动执行：
    \"$PROJECT_DIR/.venv/bin/python\" -m pip install -r \"$PROJECT_DIR/requirements.txt\"
  或通过 PYTHON_BIN 指定已安装依赖的解释器。"
    fi

    mkdir -p "$LOG_DIR" "$RUN_DIR"
    {
        echo
        echo "===== $(date '+%Y-%m-%d %H:%M:%S') 启动服务 ====="
    } >>"$LOG_FILE"

    nohup "$python_bin" "$SERVER_SCRIPT" \
        --config "$CONFIG_FILE" \
        --transport "$TRANSPORT" \
        --host "$HOST" \
        --port "$PORT" >>"$LOG_FILE" 2>&1 &
    pid=$!
    echo "$pid" >"$PID_FILE"

    local i
    for i in $(seq 1 40); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "启动失败，最近日志：" >&2
            tail -n 30 "$LOG_FILE" >&2 2>/dev/null || true
            return 1
        fi
        if port_open "$HOST" "$PORT"; then
            printf '服务已启动（PID %s）。\n' "$pid"
            print_url
            return 0
        fi
        sleep 0.5
    done

    echo "进程已启动（PID $pid），但端口 $PORT 在 20 秒内未就绪。" >&2
    echo "请执行 $(basename "$0") logs 查看日志。" >&2
    return 1
}

stop_service() {
    local pid=""
    if pid="$(service_pid)"; then
        :
    elif pid="$(find_running_pid)"; then
        :
    else
        rm -f "$PID_FILE"
        if port_open "$HOST" "$PORT"; then
            echo "未找到本服务的进程记录，但端口 $PORT 仍被占用，请手动检查（ss -ltnp | grep :$PORT）。" >&2
            return 1
        fi
        echo "服务未运行。"
        return 0
    fi

    printf '正在停止服务（PID %s）...\n' "$pid"
    kill "$pid" 2>/dev/null || true

    local i
    for i in $(seq 1 20); do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "进程未在 10 秒内退出，强制结束。" >&2
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    echo "服务已停止。"
    return 0
}

status_service() {
    local pid=""
    if pid="$(find_running_pid)"; then
        echo "状态：运行中"
        printf 'PID ：%s\n' "$pid"
        print_url
        printf '配置：%s\n' "$CONFIG_FILE"
        printf '日志：%s\n' "$LOG_FILE"
        return 0
    fi

    echo "状态：未运行"
    if port_open "$HOST" "$PORT"; then
        echo "注意：端口 $PORT 被其他进程占用。"
    fi
    printf '配置：%s\n' "$CONFIG_FILE"
    printf '日志：%s\n' "$LOG_FILE"
    return 1
}

logs_service() {
    [ -f "$LOG_FILE" ] || die "暂无日志文件：$LOG_FILE（服务可能尚未启动）"
    tail -n 50 -f "$LOG_FILE"
}

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    check_transport

    COMMAND="${1:-}"
    case "$COMMAND" in
        start)
            start_service
            ;;
        stop)
            stop_service
            ;;
        restart)
            stop_service && start_service
            ;;
        status)
            status_service
            ;;
        logs)
            logs_service
            ;;
        help|-h|--help|"")
            usage
            ;;
        *)
            usage >&2
            exit 1
            ;;
    esac
fi
