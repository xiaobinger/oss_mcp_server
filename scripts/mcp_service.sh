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
#   python3 -m pip install -r requirements.txt
#   chmod +x scripts/mcp_service.sh
#
# 可选环境变量：
#   PYTHON_BIN          指定 Python 解释器（默认优先 .venv/bin/python，其次 python3 / python）
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

可选环境变量：PYTHON_BIN、OSS_MCP_CONFIG、OSS_MCP_HOST、OSS_MCP_PORT、OSS_MCP_TRANSPORT、
              OSS_MCP_BEARER_TOKEN（监听非本机地址时必填）
EOF
}

resolve_python() {
    if [ -n "${PYTHON_BIN:-}" ]; then
        printf '%s\n' "$PYTHON_BIN"
        return 0
    fi
    local candidate
    for candidate in "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/venv/bin/python"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi
    return 1
}

PYTHON="$(resolve_python || true)"

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
    [ -n "${PYTHON:-}" ] || return 1
    "$PYTHON" - "$1" "$2" <<'PY'
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
    [ -n "${PYTHON:-}" ] || die "未找到 Python，请先安装 Python 3，或通过 PYTHON_BIN 指定解释器。"
    [ -x "$PYTHON" ] || die "Python 不可执行：$PYTHON"
    [ -f "$CONFIG_FILE" ] || die "未找到配置文件：$CONFIG_FILE
  请先执行：cp \"$PROJECT_DIR/config/oss_config.example.json\" \"$CONFIG_FILE\"
  然后按需修改分组、bucket 和 AccessKey。"

    local pid
    if pid="$(find_running_pid)"; then
        printf '服务已在运行（PID %s）。\n' "$pid"
        print_url
        return 0
    fi

    rm -f "$PID_FILE"

    if port_open "$HOST" "$PORT"; then
        die "端口 $PORT 已被其他进程占用，请更换 OSS_MCP_PORT 或先释放端口。"
    fi

    if ! "$PYTHON" -c "import oss2, mcp" >/dev/null 2>&1; then
        die "当前 Python 缺少依赖：$PYTHON
  请执行：\"$PYTHON\" -m pip install oss2 mcp requests"
    fi

    mkdir -p "$LOG_DIR" "$RUN_DIR"
    {
        echo
        echo "===== $(date '+%Y-%m-%d %H:%M:%S') 启动服务 ====="
    } >>"$LOG_FILE"

    nohup "$PYTHON" "$SERVER_SCRIPT" \
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
