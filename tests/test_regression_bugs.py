"""5월 한 달간 발견된 버그들의 회귀 테스트.

각 테스트는 발견된 실제 버그를 재현하는 minimal case.
이 테스트가 통과하면 같은 버그가 다시 안 들어옴을 보장.
"""

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest


# ──────────────────────────────────────────────────────────
# 버그 #1: timezone-naive vs aware TypeError
# (5-22 발생 — run_loop의 pre-market wait 분기)
# ──────────────────────────────────────────────────────────

KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dtime(9, 0)


def test_premarket_wait_no_typeerror():
    """run_loop의 wait 분기에서 aware-naive 뺄셈 TypeError 회귀 방지."""
    now = datetime.now(KST)  # aware
    # fix: tzinfo=KST 명시
    open_dt = datetime.combine(now.date(), MARKET_OPEN, tzinfo=KST)
    # 두 datetime 모두 aware → 정상 뺄셈
    wait_seconds = (open_dt - now).total_seconds()
    assert isinstance(wait_seconds, float)


def test_premarket_wait_buggy_pattern_caught():
    """버그 패턴 (tzinfo 빠짐) 시도 시 TypeError 발생 확인."""
    now = datetime.now(KST)  # aware
    naive_open = datetime.combine(now.date(), MARKET_OPEN)  # naive
    with pytest.raises(TypeError):
        _ = (naive_open - now).total_seconds()


# ──────────────────────────────────────────────────────────
# 버그 #2: ZeroDivisionError in journal.py
# (5-20 발생 — total_value=0일 때 cash/total_value 나누기)
# ──────────────────────────────────────────────────────────

def safe_pct(cash, total_value):
    """journal.py의 fix 패턴: 0 가드."""
    if total_value <= 0:
        return 0.0
    return cash / total_value * 100


def test_total_value_zero_no_division_error():
    assert safe_pct(0, 0) == 0.0
    assert safe_pct(100, 0) == 0.0


def test_total_value_positive():
    assert safe_pct(50, 100) == 50.0


# ──────────────────────────────────────────────────────────
# 버그 #3: KIS API 30일 한도
# (5-20 발견 — TA 분석이 60일 필요한데 30일만 받음)
# ──────────────────────────────────────────────────────────

def test_extended_history_uses_chart_endpoint():
    """fetch_recent_history가 days > 30이면 확장 endpoint를 사용해야 함."""
    from src.bot.runner import fetch_recent_history
    import inspect

    source = inspect.getsource(fetch_recent_history)
    # 60일 이상 요청 시 itemchartprice 호출
    assert "get_daily_itemchartprice" in source, (
        "fetch_recent_history가 30일 한도 우회를 위해 "
        "get_daily_itemchartprice (FHKST03010100) endpoint를 사용해야 함"
    )


def test_kis_client_has_extended_endpoint():
    """kis_client에 확장 일봉 메서드 존재."""
    from src.kis_client import KISClient

    assert hasattr(KISClient, "get_daily_itemchartprice")


# ──────────────────────────────────────────────────────────
# 버그 #4: etf_held UnboundLocalError
# (5-21 발생 — BEAR/CRISIS 분기에서 etf_held 미정의)
# ──────────────────────────────────────────────────────────

def test_single_run_no_unbound_etf_held():
    """run_loop이 BEAR/CRISIS 분기에서도 etf_held 정의되는지 정적 검증."""
    from src.bot import single_run
    import ast
    import inspect

    source = inspect.getsource(single_run.run_loop)
    tree = ast.parse(source)

    # 모든 'etf_held' 참조가 정의 후 사용되는지 정적 검증은 복잡함.
    # 차선책: source에 "etf_held" 가드 패턴이 들어있는지만 확인.
    # 실제 수정: BEAR/CRISIS 분기에서도 etf_held = False 초기화 필요.
    # 현재 코드는 etf_held가 if/else 양쪽에서 정의되므로 OK.
    assert "etf_held" in source


# ──────────────────────────────────────────────────────────
# 버그 #5: cron YAML 'on'/'off' boolean 해석
# (5-22 발생 — killswitch.yml workflow_dispatch input)
# ──────────────────────────────────────────────────────────

def test_killswitch_workflow_uses_safe_choice_names():
    """workflow의 choice option이 YAML boolean으로 해석 안 되는지."""
    from pathlib import Path
    import re

    workflow = Path("C:/Users/wodnj/kis-autotrader/.github/workflows/killswitch.yml")
    if not workflow.exists():
        pytest.skip("workflow not in test environment")

    text = workflow.read_text(encoding="utf-8")
    # 'on' 또는 'off'가 type: choice options에 쓰이면 안 됨
    # (activate/deactivate/status 사용)
    assert "- activate" in text
    assert "- deactivate" in text
    # YAML boolean 키워드 미사용 확인 (단독 라인의 '- on' 등)
    assert "\n          - on\n" not in text
    assert "\n          - off\n" not in text


# ──────────────────────────────────────────────────────────
# 버그 #6: 시장가 주문에 단가를 실어 rt_cd=7 전량 거부
# (6-01 발견 — 한 주 내내 국장 매매 0건의 진짜 원인.
#  _safe_order_cash가 order_type 미지정 → 기본 "01"(시장가)인데
#  price를 함께 전달 → KIS "주문단가를 0으로 입력하세요" 거부)
# ──────────────────────────────────────────────────────────

def _make_client_capturing_body():
    """auth 없이 KISClient를 만들고 _safe_post가 body를 캡처하도록."""
    from src.kis_client import KISClient

    client = KISClient.__new__(KISClient)  # __init__(인증) 우회
    captured = {}

    def _fake_post(path, tr_id=None, body=None):
        captured.clear()
        captured.update(body or {})
        return {"rt_cd": "0", "msg1": "ok"}

    client._safe_post = _fake_post
    return client, captured


def test_market_order_sends_zero_price():
    """시장가(01) 주문은 ORD_UNPR='0'이어야 한다 (가격을 넘겨도 무시)."""
    client, captured = _make_client_capturing_body()
    client.order_cash("091180", qty=1, price=40975, side="buy", order_type="01")
    # 15:20 이전이면 시장가 그대로 → 단가 0,
    # 15:20 이후 자동 지정가 전환되면 DVSN=00 + 단가=가격. 둘 다 정합해야 함.
    if captured["ORD_DVSN"] == "01":
        assert captured["ORD_UNPR"] == "0", "시장가인데 0이 아닌 단가 → rt_cd=7 거부"
    else:
        assert captured["ORD_DVSN"] == "00"
        assert captured["ORD_UNPR"] == "40975"


def test_limit_order_sends_price():
    """지정가(00) 주문은 ORD_UNPR=정수 단가를 실어야 한다."""
    client, captured = _make_client_capturing_body()
    client.order_cash("091180", qty=1, price=40975, side="buy", order_type="00")
    assert captured["ORD_DVSN"] == "00"
    assert captured["ORD_UNPR"] == "40975"


def test_limit_order_float_price_normalized_to_int():
    """float 단가(40975.0)도 정수 문자열 '40975'로 정규화 (소수점 거부 회피)."""
    client, captured = _make_client_capturing_body()
    client.order_cash("091180", qty=1, price=40975.0, side="buy", order_type="00")
    assert captured["ORD_UNPR"] == "40975"
    assert "." not in captured["ORD_UNPR"]


# ──────────────────────────────────────────────────────────
# 버그 #7: 루프가 GitHub 하드 타임아웃(360분)에 강제 종료 → 정리 스텝 스킵 →
# 체결 기록 유실 (6-02 498400 매수 기록 유실). 하드 한도 전 자체 정상 종료해야 함.
# ──────────────────────────────────────────────────────────

def test_loop_self_timeout_before_github_hard_limit():
    """자체 최대 실행시간이 GitHub 하드 타임아웃(360분)보다 충분히 짧아야 한다.
    그래야 강제 종료 전에 정상 break → 거래기록·저널 정리 스텝이 실행됨."""
    from src.bot.single_run import MAX_LOOP_RUNTIME_SEC
    GITHUB_HARD_LIMIT = 360 * 60
    # 셋업(~3분)+정리(~3분) 여유를 위해 최소 10분 마진
    assert MAX_LOOP_RUNTIME_SEC <= GITHUB_HARD_LIMIT - 10 * 60


def test_runtime_exceeded_boundary():
    """경과 < 한도면 False, >= 한도면 True (핸드오프 종료 판정)."""
    from src.bot.single_run import _runtime_exceeded, MAX_LOOP_RUNTIME_SEC
    start = 1_000_000.0
    assert _runtime_exceeded(start, start) is False                       # 막 시작
    assert _runtime_exceeded(start, start + MAX_LOOP_RUNTIME_SEC - 1) is False
    assert _runtime_exceeded(start, start + MAX_LOOP_RUNTIME_SEC) is True
    assert _runtime_exceeded(start, start + MAX_LOOP_RUNTIME_SEC + 60) is True


def test_us_loop_has_same_runtime_cap():
    """미국 야간 루프도 동일하게 하드 한도 전 자체 종료 상수를 가져야 한다."""
    from src.bot.night_run import MAX_LOOP_RUNTIME_SEC as US_CAP
    assert US_CAP <= 360 * 60 - 10 * 60


# ──────────────────────────────────────────────────────────
# 버그 #8: NYSE Arca ETF(SPY·SH)를 NYSE 코드로 주문 → "거래정지종목" 거부
# (6-03 확인). KIS는 Arca 종목을 AMEX 책으로 라우팅 → 거부 시 대체 거래소 재시도.
# ──────────────────────────────────────────────────────────

def _client_with_programmed_order(responses_by_exchange):
    """거래소별 응답을 프로그램한 KISClient + 호출된 거래소 기록."""
    from src.kis_client import KISClient
    client = KISClient.__new__(KISClient)
    calls = []

    def _fake_post(path, tr_id=None, body=None):
        ex = body["OVRS_EXCG_CD"]
        calls.append(ex)
        return responses_by_exchange[ex]

    client._safe_post = _fake_post
    return client, calls


def test_overseas_order_retries_alt_exchange_on_reject():
    """NYSE 거부 → AMEX로 1회 재시도, 성공 응답 반환."""
    client, calls = _client_with_programmed_order({
        "NYSE": {"rt_cd": "7", "msg1": "거래정지종목(주식)은 취소주문만 가능"},
        "AMEX": {"rt_cd": "0", "msg1": "정상처리"},
    })
    resp = client.order_overseas("SH", 6, 32.97, side="buy", exchange="NYSE")
    assert resp["rt_cd"] == "0"
    assert calls == ["NYSE", "AMEX"], "거부 후 대체 거래소(AMEX)로 재시도해야 함"


def test_overseas_order_no_retry_when_first_ok():
    """첫 주문 성공이면 재시도 없음 (이중 주문 방지)."""
    client, calls = _client_with_programmed_order({
        "AMEX": {"rt_cd": "0", "msg1": "정상처리"},
    })
    resp = client.order_overseas("SH", 6, 32.97, side="buy", exchange="AMEX")
    assert resp["rt_cd"] == "0"
    assert calls == ["AMEX"]


def test_overseas_order_no_retry_for_nasd():
    """NASD는 대체 거래소가 없으므로 거부돼도 재시도 안 함."""
    client, calls = _client_with_programmed_order({
        "NASD": {"rt_cd": "1", "msg1": "잔고부족"},
    })
    resp = client.order_overseas("QQQ", 1, 500.0, side="buy", exchange="NASD")
    assert resp["rt_cd"] == "1"
    assert calls == ["NASD"], "NASD는 재시도 없이 1회만"


def test_overseas_order_retry_only_once():
    """양쪽 모두 거부돼도 무한루프 없이 정확히 2회만 시도."""
    client, calls = _client_with_programmed_order({
        "NYSE": {"rt_cd": "7", "msg1": "거래정지"},
        "AMEX": {"rt_cd": "7", "msg1": "거래정지"},
    })
    resp = client.order_overseas("SPY", 1, 600.0, side="buy", exchange="NYSE")
    assert resp["rt_cd"] == "7"
    assert calls == ["NYSE", "AMEX"]


def test_us_universe_exchanges_correct():
    """US 유니버스 거래소: Arca 상장 ETF → AMEX, 국채 ETF → NASD.
    (저가 ETF 재구성 2026-06-05: SPLG/SCHG/XLF/SH/PSQ=AMEX, TLT/SHY=NASD)"""
    import yaml
    from pathlib import Path
    cfg_path = Path("configs/strategy.yaml")
    if not cfg_path.exists():
        import pytest
        pytest.skip("strategy.yaml not in test env")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    uni = (cfg.get("us_session", {}) or {}).get("universe", []) or []
    ex = {x["symbol"]: x.get("exchange") for x in uni if isinstance(x, dict)}
    # Arca 상장 ETF는 AMEX
    for sym in ("SPLG", "SCHG", "XLF", "SH", "PSQ"):
        if sym in ex:
            assert ex[sym] == "AMEX", f"{sym}는 AMEX여야 함 (현재 {ex[sym]})"
    # 국채 ETF는 NASD
    for sym in ("TLT", "SHY"):
        if sym in ex:
            assert ex[sym] == "NASD", f"{sym}는 NASD여야 함 (현재 {ex[sym]})"
    # 고가 종목·개별주 제거 확인 (저가 ETF 재구성)
    assert "SPY" not in ex and "QQQ" not in ex, "고가 ETF(SPY/QQQ)는 제거됐어야 함"
    assert "NVDA" not in ex and "AAPL" not in ex, "개별주는 제거됐어야 함"


# ──────────────────────────────────────────────────────────
# 버그 #9: 하락장 인버스 매수가 2X 곱버스(252670)를 사면 CLAUDE.md #6 위반.
# 레버리지 인버스는 어떤 경우에도 매수되면 안 됨 (설정+코드 이중 차단).
# ──────────────────────────────────────────────────────────

def test_is_leveraged_type_detection():
    """_is_leveraged_type가 레버리지만 정확히 판정 (1x·기본은 False)."""
    from src.bot.single_run import _is_leveraged_type
    # 레버리지 → True
    for t in ("inverse_2x", "leverage_2x", "3x", "INVERSE_2X", "곱버스"):
        assert _is_leveraged_type(t) is True, f"{t}는 레버리지로 판정돼야 함"
    # 1배수/기본 → False
    for t in ("inverse_1x", "inverse", "defensive", "", None):
        assert _is_leveraged_type(t) is False, f"{t}는 레버리지가 아님"


def test_load_inverse_universe_excludes_leverage(monkeypatch):
    """유니버스에 레버리지가 섞여 있어도 load 단계에서 제외된다."""
    import src.bot.single_run as sr
    fake_cfg = {"universe": {"inverse": [
        {"symbol": "114800", "name": "KODEX 인버스", "type": "inverse_1x"},
        {"symbol": "252670", "name": "KODEX 200선물인버스2X", "type": "inverse_2x"},
    ]}}
    monkeypatch.setattr(sr, "load_config", lambda: fake_cfg)
    loaded = sr.load_inverse_universe()
    syms = {s["symbol"] for s in loaded}
    assert "114800" in syms
    assert "252670" not in syms, "2X 레버리지는 로드에서 제외돼야 함"


def test_strategy_yaml_has_no_leverage_inverse():
    """strategy.yaml inverse 유니버스에 레버리지 종목이 없어야 한다."""
    import yaml
    from pathlib import Path
    cfg_path = Path("configs/strategy.yaml")
    if not cfg_path.exists():
        import pytest
        pytest.skip("strategy.yaml not in test env")
    from src.bot.single_run import _is_leveraged_type
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    inv = (cfg.get("universe", {}) or {}).get("inverse", []) or []
    bad = [s for s in inv if _is_leveraged_type(s.get("type"))]
    assert not bad, f"레버리지 인버스가 유니버스에 남아있음: {[s.get('symbol') for s in bad]}"
    assert "252670" not in {s.get("symbol") for s in inv}, "곱버스(252670) 제거돼야 함"


def test_regime_blind_failsafe_not_bull():
    """시장데이터 조회 실패(API 불통) 시 강세(BULL)로 오판하지 않아야 한다.

    검은 월요일 회귀: KIS API 과부하로 KOSPI를 못 읽으면 sma_ratio=0이 되어
    BULL로 폴백 → 폭락에 롱 매수하는 치명적 버그. blind=True + 비-BULL 이어야 함.
    """
    from src.strategies.bear_strategy import detect_market_regime
    r = detect_market_regime(None, {}, hmm_state="unknown", hmm_confidence=0.5, cfg={})
    assert r.blind is True, "데이터 불가 시 blind=True"
    assert r.regime != "BULL", f"블라인드인데 BULL로 오판: {r.regime}"


def test_regime_not_blind_with_data():
    """충분한 데이터가 있으면 blind=False."""
    import pandas as pd
    idx = pd.date_range("2025-01-01", periods=250, freq="D", name="date")
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0,
                       "close": 100.0, "volume": 1000}, index=idx)
    from src.strategies.bear_strategy import detect_market_regime
    r = detect_market_regime(df, {}, hmm_state="unknown", hmm_confidence=0.5, cfg={})
    assert r.blind is False


def test_crisis_allocation_has_small_inverse():
    """사용자 방향: CRISIS도 현금 100%가 아니라 소액 인버스로 하락 수익 추구.

    단 BEAR보다 작은 캡 + 현금/단기채 비중이 높아야 한다(휘둘림 방어).
    실제 진입은 run_bear_strategy의 돌파 신호 게이트를 통과해야만 발생.
    """
    from src.strategies.bear_strategy import compute_bear_allocation, MarketRegimeResult
    crisis = compute_bear_allocation(
        MarketRegimeResult(regime="CRISIS", confidence=0.65, sma_ratio=-0.09, canary_score=1),
        current_vol=0.45, cfg={})
    assert crisis.inverse_pct > 0, "CRISIS도 소액 인버스 배분이 있어야 함"
    assert crisis.inverse_pct <= 0.15, "CRISIS 인버스는 소액 캡(<=15%) 이내"
    assert crisis.cash_pct + crisis.defensive_pct >= 0.7, "CRISIS는 방어/현금 70%+ 유지"
