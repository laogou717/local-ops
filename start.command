#!/bin/bash
set -u
umask 077
cd "$(dirname "$0")"

# 与 总控台.app launcher 一致的 Python 发现顺序（Finder/双击不读 shell 配置）。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"

python_is_ok() {
  local bin="$1"
  [ -n "$bin" ] && [ -x "$bin" ] || return 1
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

pick_mise_install_python() {
  local root="${MISE_DATA_DIR:-$HOME/.local/share/mise}/installs/python"
  local best="" best_key="" cand ver key
  [ -d "$root" ] || return 1
  for cand in "$root"/*/bin/python3; do
    [ -x "$cand" ] || continue
    python_is_ok "$cand" || continue
    ver="$("$cand" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || continue
    key="$(printf '%s' "$ver" | awk -F. '{printf "%03d.%03d.%03d", $1+0, $2+0, $3+0}')"
    if [ -z "$best_key" ] || [[ "$key" > "$best_key" ]]; then
      best="$cand"
      best_key="$key"
    fi
  done
  [ -n "$best" ] || return 1
  printf '%s\n' "$best"
}

pick_python() {
  local cand

  if [ -n "${CONSOLE_PYTHON:-}" ]; then
    if python_is_ok "$CONSOLE_PYTHON"; then
      printf '%s\n' "$CONSOLE_PYTHON"
      return 0
    fi
  fi

  for cand in \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3
  do
    if python_is_ok "$cand"; then
      printf '%s\n' "$cand"
      return 0
    fi
  done

  if cand="$(pick_mise_install_python)"; then
    printf '%s\n' "$cand"
    return 0
  fi

  for cand in \
    "${MISE_DATA_DIR:-$HOME/.local/share/mise}/shims/python3" \
    "$(command -v python3 2>/dev/null || true)"
  do
    if python_is_ok "$cand"; then
      printf '%s\n' "$cand"
      return 0
    fi
  done

  return 1
}

PYTHON_BIN="$(pick_python || true)"
if [ -z "${PYTHON_BIN:-}" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：未找到 Python 3，请先安装 Python 3.12 或更高版本。也可设置 CONSOLE_PYTHON。" >&2
    exit 127
  fi
  echo "错误：总控台需要 Python 3.12 或更高版本。也可设置 CONSOLE_PYTHON 指向合格解释器。" >&2
  exit 126
fi
exec "$PYTHON_BIN" server.py --launcher
