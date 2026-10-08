#!/usr/bin/env bash
set -euo pipefail

REPO_USER="keqin3"
REPO_NAME="aimili-vpngate"
BRANCH="main"
INSTALL_DIR="/opt/aimilivpn"
REPO_URL="https://github.com/${REPO_USER}/${REPO_NAME}.git"
RAW_INSTALL_URL="https://raw.githubusercontent.com/${REPO_USER}/${REPO_NAME}/${BRANCH}/install.sh"

if [ "$(id -u)" != "0" ]; then
    echo "错误: 请使用 root 用户运行，或在命令前加 sudo。" >&2
    exit 1
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
if [ -d "${INSTALL_DIR}" ]; then
    backup="/opt/aimilivpn-backup-${timestamp}.tar.gz"
    echo "[1/5] 备份当前安装到 ${backup}"
    tar -C /opt --exclude="aimilivpn/vpngate_data/*.log" --exclude="aimilivpn/vpngate_data/configs" -czf "${backup}" aimilivpn || true
    chmod 600 "${backup}"

    if [ ! -d "${INSTALL_DIR}/.git" ]; then
        echo "错误: ${INSTALL_DIR} 不是 Git 仓库。备份已经生成，未修改当前安装。" >&2
        exit 1
    fi

    echo "[2/5] 将更新源切换到 ${REPO_URL}"
    git -C "${INSTALL_DIR}" remote set-url origin "${REPO_URL}"
    rm -f "${INSTALL_DIR}/.local_dev"
else
    echo "[1/5] 未发现旧安装，将执行首次安装"
    echo "[2/5] 使用 ${REPO_URL} 作为安装源"
fi

echo "[3/5] 下载受控安装脚本"
tmp_script="$(mktemp)"
trap 'rm -f "${tmp_script}"' EXIT
curl -fsSL "${RAW_INSTALL_URL}" -o "${tmp_script}"

echo "[4/5] 安装或升级 AimiliVPN"
bash "${tmp_script}" "${REPO_USER}" "${REPO_NAME}"

# Older installs may pin the former three-source list in EnvironmentFile, which
# would otherwise override the new application default after upgrading.
env_file="/etc/default/aimilivpn"
if [ -f "${env_file}" ] && grep -q '^NODE_SOURCES=' "${env_file}" && ! grep '^NODE_SOURCES=' "${env_file}" | grep -q 'auto_ovpn'; then
    python3 - "${env_file}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
updated = []
for line in lines:
    if line.startswith("NODE_SOURCES="):
        value = line.split("=", 1)[1].strip()
        quote = value[:1] if value[:1] in {"'", '"'} and value[-1:] == value[:1] else ""
        raw = value[1:-1] if quote else value
        items = [item.strip() for item in raw.split(",") if item.strip()]
        if "auto_ovpn" not in items:
            items.append("auto_ovpn")
        value = ",".join(items)
        line = f"NODE_SOURCES={quote}{value}{quote}"
    updated.append(line)
path.write_text("\n".join(updated) + "\n", encoding="utf-8")
PY
fi

echo "[5/5] 验证服务"
if command -v systemctl >/dev/null 2>&1; then
    systemctl restart aimilivpn.service
    systemctl --no-pager --full status aimilivpn.service
elif command -v rc-service >/dev/null 2>&1; then
    rc-service aimilivpn restart
    rc-service aimilivpn status
else
    echo "警告: 未检测到 systemd/OpenRC，请手动启动 ${INSTALL_DIR}/vpngate_manager.py" >&2
fi

echo "升级完成。默认聚合 vpngate、ipspeed、vpngate_scraper、auto_ovpn；测速后低延迟优先，连续两轮失效节点自动清理。"
