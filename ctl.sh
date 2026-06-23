#!/bin/bash
# openwebui-lite 一键管理
# 端口:8899
set -e

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
PYTHON="${PYTHON:-$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)}"
PORT="${PORT:-8899}"
LOG="$HOME/logs/openwebui-lite.log"
SERVICE_DIR="$HOME/.config/systemd/user"

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

is_port_open() {
  if has_cmd ss; then
    ss -tln 2>/dev/null | grep -q ":$PORT "
    return $?
  fi
  return 1
}

running_pid() {
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "$APP_DIR/server.py" | head -1
    return 0
  fi
  return 1
}

port_status() {
  if command -v ss >/dev/null 2>&1; then
    ss -tlnp 2>/dev/null | grep ":$PORT " | head -3 || echo "  (无)"
  else
    echo "  ss not available"
  fi
}

start() {
  echo "▶ 启动 openwebui-lite ..."
  if is_port_open; then
    echo "  ⚠ 端口 $PORT 已被占用,先停掉"
    stop
    sleep 1
  fi
  mkdir -p "$HOME/logs"
  nohup "$PYTHON" -u "$APP_DIR/server.py" > "$LOG" 2>&1 &
  sleep 2
  if is_port_open; then
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$PORT/health" || echo "ERR")
    echo "  ✓ 启动成功 · 健康检查 HTTP $code · 日志 $LOG"
  else
    echo "  ✗ 启动失败,看日志: tail -30 $LOG"
  fi
}

stop() {
  echo "■ 停止 ..."
  if command -v pkill >/dev/null 2>&1; then
    pkill -f "$APP_DIR/server.py" 2>/dev/null && echo "  ✓ 已停" || echo "  · 没在跑"
  else
    echo "  · pkill not available"
  fi
  sleep 1
}

status() {
  echo "≡ 状态:"
  pid=$(running_pid || true)
  if [ -n "$pid" ]; then
    echo "  ● 运行中 [PID $pid]"
  else
    echo "  ○ 未运行"
  fi
  echo ""
  echo "≡ 端口:"
  port_status
  echo ""
  echo "≡ 健康:"
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:$PORT/health" 2>/dev/null || echo "ERR")
  echo "  http://127.0.0.1:$PORT/health  →  HTTP $code"
  echo ""
  echo "≡ 后端 LiteLLM 状态:"
  code2=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:4000/health/liveliness 2>/dev/null || echo "ERR")
  echo "  http://127.0.0.1:4000/health/liveliness  →  HTTP $code2"
  echo ""
  echo "≡ 模型数量:"
  if has_cmd python3; then
    curl -s --max-time 5 "http://127.0.0.1:$PORT/api/models" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  {} 个'.format(d.get('count', 0)))" 2>/dev/null || echo "  (无法获取)"
  else
    echo "  python3 not available"
  fi
  echo ""
  echo "≡ 开机自启:"
  if [ -f "$SERVICE_DIR/openwebui-lite.service" ] && has_cmd systemctl; then
    enabled=$(systemctl --user is-enabled openwebui-lite 2>/dev/null || echo "disabled")
    echo "  $enabled"
  else
    echo "  (未配置)"
  fi
}

enable() {
  echo "▶ 启用开机自启 ..."
  mkdir -p "$SERVICE_DIR"
  cat > "$SERVICE_DIR/openwebui-lite.service" <<EOF
[Unit]
Description=openwebui-lite (8899)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON -u $APP_DIR/server.py
Restart=always
RestartSec=3
StandardOutput=append:$HOME/logs/openwebui-lite.log
StandardError=append:$HOME/logs/openwebui-lite.log

[Install]
WantedBy=default.target
EOF
  if has_cmd systemctl; then
    systemctl --user daemon-reload
    systemctl --user enable openwebui-lite.service
    echo "  ✓ 已 enable"
  else
    echo "  · systemctl not available"
  fi
}

disable() {
  if has_cmd systemctl; then
    systemctl --user disable openwebui-lite.service 2>/dev/null && echo "  ✓ 已 disable" || echo "  · 没在 enable 状态"
  else
    echo "  · systemctl not available"
  fi
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  enable) enable ;;
  disable) disable ;;
  *) echo "用法: $0 {start|stop|restart|status|enable|disable}"; exit 1 ;;
esac
