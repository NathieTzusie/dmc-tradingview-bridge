# ─────────────────────────────────────────────────────────────
# DMC TradingView Bridge — 一键部署脚本
# 用法：在 CentOS/Debian 服务器上运行：
#   curl -sL https://raw.githubusercontent.com/NathieTzusie/dmc-tradingview-bridge/main/deploy/install.sh | bash
# ─────────────────────────────────────────────────────────────

set -euo pipefail

BRIDGE_DIR="/opt/dmc-bridge"
REPO_URL="https://github.com/NathieTzusie/dmc-tradingview-bridge.git"

echo "=== DMC TradingView Bridge — 一键部署 ==="
echo ""

# 检查 root
if [ "$EUID" -ne 0 ]; then
  echo "请以 root 身份运行：sudo bash install.sh"
  exit 1
fi

# 安装依赖
echo "[1/5] 安装系统依赖..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nginx certbot 2>/dev/null || \
yum install -y -q python3 git nginx certbot 2>/dev/null || true

# Clone repo
echo "[2/5] 下载源码..."
if [ -d "$BRIDGE_DIR" ]; then
  cd "$BRIDGE_DIR" && git pull
else
  git clone "$REPO_URL" "$BRIDGE_DIR"
fi
cd "$BRIDGE_DIR"

# 配置 .env
echo "[3/5] 配置..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "   ⚠️  请编辑 $BRIDGE_DIR/.env 填入你的 API key 和 Token"
  echo "   ⚠️  编辑完成后运行：sudo systemctl restart dmc-bridge"
fi

# 安装 Python 依赖
echo "[4/5] 安装 Python 依赖..."
python3 -m venv venv
source venv/bin/activate
pip install -q .

# 安装 systemd 服务
echo "[5/5] 安装 systemd 服务..."
cp deploy/bridge.service /etc/systemd/system/dmc-bridge.service
sed -i "s|/opt/sisie-bridge|$BRIDGE_DIR|g" /etc/systemd/system/dmc-bridge.service
sed -i "s|/etc/sisie-bridge.env|$BRIDGE_DIR/.env|g" /etc/systemd/system/dmc-bridge.service
systemctl daemon-reload
systemctl enable dmc-bridge
systemctl start dmc-bridge

echo ""
echo "=== 部署完成！==="
echo ""
echo "   Dashboard: http://$(curl -s ifconfig.me):8080/dashboard"
echo "   Webhook:   http://$(curl -s ifconfig.me):8080/webhook?token=<你的 Token>"
echo ""
echo "   配置 SSL 请运行："
echo "     certbot --nginx -d 你的域名"
echo ""
echo "   查看日志："
echo "     journalctl -u dmc-bridge -f"
echo ""
