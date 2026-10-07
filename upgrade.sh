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
    tar -C /opt -czf "${backup}" aimilivpn
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

echo "升级完成。默认聚合 vpngate、ipspeed、vpngate_scraper；MAX_SCAN_ROWS=0 表示 VPNGate 主快照不限条数。"
