#!/bin/bash
# ─────────────────────────────────────────────────────────
# Oracle Cloud VM 초기 세팅 스크립트
# 대상: Ubuntu 22.04+ ARM (Ampere A1)
#
# 사용법:
#   1. OCI에서 Always Free ARM VM 생성
#   2. SSH 접속 후 이 스크립트 실행:
#      curl -sL https://raw.githubusercontent.com/BudongJW/kis-autotrader/main/scripts/oci-setup.sh | bash
#   또는 로컬에서:
#      scp scripts/oci-setup.sh ubuntu@<VM_IP>:~ && ssh ubuntu@<VM_IP> bash oci-setup.sh
# ─────────────────────────────────────────────────────────

set -euo pipefail

echo "========================================"
echo " KIS AutoTrader — OCI VM 세팅 시작"
echo "========================================"

# 1. 시스템 업데이트
echo "[1/6] 시스템 업데이트..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# 2. Python 3.12 설치
echo "[2/6] Python 3.12 설치..."
sudo apt-get install -y -qq software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -qq
sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev python3-pip

# pip 업그레이드
python3.12 -m pip install --upgrade pip 2>/dev/null || sudo apt-get install -y python3-pip

# 3. Node.js + PM2 설치 (프로세스 관리)
echo "[3/6] Node.js + PM2 설치..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y -qq nodejs
fi
sudo npm install -g pm2

# 4. 프로젝트 클론
echo "[4/6] 프로젝트 클론..."
cd ~
if [ -d "kis-autotrader" ]; then
    echo "  이미 존재. git pull..."
    cd kis-autotrader && git pull
else
    git clone https://github.com/BudongJW/kis-autotrader.git
    cd kis-autotrader
fi

# 5. 가상환경 + 의존성 설치
echo "[5/6] Python 가상환경 + 의존성..."
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install pykrx hmmlearn optuna lightgbm scikit-learn yfinance

# 6. 디렉토리 생성
echo "[6/6] 로그/설정 디렉토리..."
mkdir -p logs configs

echo ""
echo "========================================"
echo " 세팅 완료!"
echo ""
echo " 다음 단계:"
echo "   1. .env 파일 생성: cp .env.example .env && nano .env"
echo "   2. PM2 등록:       pm2 start ecosystem.config.js"
echo "   3. PM2 자동시작:   pm2 save && pm2 startup"
echo "========================================"
