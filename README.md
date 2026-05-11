# kis-autotrader

한국투자증권 KIS Open API 기반 주식 자동매매 시스템.

> ⚠️ **이 프로젝트는 학습·연구 목적입니다.** 실전 매매는 본인 책임. 백테스트 수익률 ≠ 실전 수익률.

---

## 🏠 집 컴퓨터에서 빠르게 시작하기

### 1) 클론
```bash
git clone https://github.com/BudongJW/kis-autotrader.git
cd kis-autotrader
```

### 2) Python 환경
```bash
# Python 3.11+ 권장
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Windows cmd
.venv\Scripts\activate.bat
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3) KIS Developers 가입 + 앱키 발급
1. https://apiportal.koreainvestment.com/intro 접속
2. 회원가입 → 앱 등록 → **앱키(APPKEY)** + **앱시크릿(APPSECRET)** 발급
3. **모의투자 계좌**도 별도 신청 (실전 투입 전 필수)
   - 한투 HTS/MTS의 모의투자 메뉴에서 신청
   - 계좌번호는 `12345678-01` 형식 (8자리 + 상품코드 2자리)

### 4) 환경변수 설정
```bash
cp .env.example .env
# .env 열어서 본인 값으로 채우기
```

채워야 할 값:
- `KIS_APPKEY`, `KIS_APPSECRET`: 실전용
- `KIS_VIRTUAL_APPKEY`, `KIS_VIRTUAL_APPSECRET`: 모의투자용 (별도 발급)
- `KIS_HTSID`: 본인 HTS ID
- `KIS_ACCOUNT_NO`, `KIS_ACCOUNT_PROD_CODE`: 계좌번호 8자리 + 상품코드 2자리
- `MODE`: `paper` (모의) 또는 `live` (실전) — **기본 `paper`**

### 5) Claude Code 실행
```bash
# 프로젝트 루트에서
claude
```
Claude Code가 자동으로 `CLAUDE.md`를 읽고 프로젝트 컨텍스트를 파악합니다.

### 6) 첫 시세 조회 (인증 확인)
```bash
python scripts/check_auth.py
# 정상: "✅ 모의투자 토큰 발급 성공, 삼성전자 현재가: 71,500원"
```

또는 노트북으로:
```bash
jupyter notebook notebooks/01_first_quote.ipynb
```

---

## 📂 디렉토리 구조

```
kis-autotrader/
├── CLAUDE.md              # Claude Code 컨텍스트 (먼저 읽힘)
├── README.md              # 이 파일
├── .env.example           # 환경변수 템플릿
├── requirements.txt       # pip 의존성
├── pyproject.toml         # 패키징 + 의존성
├── configs/
│   └── strategy.yaml      # 전략 파라미터
├── src/
│   ├── config.py          # 환경 설정 로드
│   ├── kis_auth.py        # KIS 인증 (토큰)
│   ├── kis_client.py      # REST 호출 래퍼
│   ├── strategies/        # 매매 전략
│   ├── backtest/          # 백테스트 엔진
│   ├── bot/               # 실시간 봇
│   └── utils/             # 로깅, rate limit
├── tests/                 # pytest
├── notebooks/             # Jupyter 노트북
├── docs/                  # 설계 문서
└── scripts/               # 일회성 스크립트
```

## 🚀 사용 예시

### 백테스트
```bash
python -m src.backtest.runner --strategy golden_cross --symbol 005930 --from 2023-01-01 --to 2024-12-31
```

### 모의투자 봇 실행
```bash
python -m src.bot.runner --strategy golden_cross --symbol 005930
```

### 실전 모드 (모의에서 검증 후만)
```bash
# MODE=live가 .env에 설정되어 있어야 함
python -m src.bot.runner --strategy golden_cross --symbol 005930 --live
```

## 🧪 테스트
```bash
pytest tests/
```

## 📚 참고 문서

프로젝트 내부:
- [CLAUDE.md](CLAUDE.md) — Claude Code 컨텍스트 (가장 먼저 읽을 것)
- [docs/00_setup.md](docs/00_setup.md) — 상세 셋업
- [docs/01_kis_api_basics.md](docs/01_kis_api_basics.md) — KIS API 기본
- [docs/02_strategy_design.md](docs/02_strategy_design.md) — 전략 설계 원칙

외부:
- [KIS Developers 공식](https://apiportal.koreainvestment.com/intro)
- [공식 GitHub (open-trading-api)](https://github.com/koreainvestment/open-trading-api)
- [python-kis 라이브러리](https://github.com/Soju06/python-kis)
- [WikiDocs 튜토리얼](https://wikidocs.net/159296)

## ⚠️ 주의사항

1. **앱키·앱시크릿은 절대 커밋하지 말 것.** `.gitignore`에 `.env` 명시됨.
2. **실전 모드 전에 모의투자 1주일 이상 운영 + 백테스트와 결과 괴리 < 30% 확인.**
3. **Rate limit**: 실전 초당 20건, 모의 초당 2건. 초과 시 5분 쿨다운.
4. **세금**: 분리과세 22% (해외주식). 거래 기록 보관 필수.
5. **백테스트는 과적합되기 쉽다.** out-of-sample 검증 필수.

## 📝 라이선스

Private repository — 사용자 본인 학습·운영 목적.
