"""평가 엔진 (스펙 2.1): 헤어컷·tier·유동성 + PykrxSource(모킹)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.adapters.price import PriceSourceError, PykrxSource
from app.engines import valuation as v
from app.engines.valuation import MarketData
from factories import bars, info, zigzag

END = date(2026, 3, 13)  # 금요일
LOG_STEP = (Decimal(71000) / Decimal(70000)).ln()
# zigzag 61개 종가 → 수익률 60개(+x, −x 교대, 평균 0) → 표본분산 = 60x²/59
ZIGZAG_SIGMA = (LOG_STEP * (Decimal(60) / Decimal(59)).sqrt() * Decimal(252).sqrt()).quantize(Decimal("0.000001"))


def market(closes=None, value=Decimal("1000000000000"), **info_kw) -> MarketData:
    closes = closes if closes is not None else zigzag(70000, 71000, 61)
    return MarketData(info=info(**info_kw), bars=bars(closes, END, trading_value=value))


# ── 변동성 · 헤어컷 ──

def test_volatility_of_zigzag_matches_closed_form():
    sigma, n = v.annualized_volatility(zigzag(70000, 71000, 61))
    assert n == 60
    assert sigma == ZIGZAG_SIGMA  # ≈ 22.7%


def test_volatility_uses_only_last_60_returns():
    noisy_history = zigzag(10000, 20000, 40) + zigzag(70000, 71000, 61)
    assert v.annualized_volatility(noisy_history)[0] == ZIGZAG_SIGMA


def test_constant_price_has_zero_volatility():
    assert v.annualized_volatility([Decimal(50000)] * 61) == (Decimal("0.000000"), 60)


@pytest.mark.parametrize(
    "sigma,expected",
    [("0", "0"), ("0.30", "0.15"), ("0.9", "0.45"), ("1.0", "0.5"), ("1.6", "0.5"), ("0.123457", "0.0618")],
)
def test_haircut(sigma, expected):
    assert v.haircut_for(Decimal(sigma)) == Decimal(expected)


def test_haircut_without_volatility_is_max():
    assert v.haircut_for(None) == Decimal("0.5")


# ── 등급 · 금액 ──

def test_tier_a_blue_chip_full_calculation():
    r = v.evaluate(market(), 1000)
    assert r.tier == "A" and r.ltv == Decimal("0.70")
    assert r.price == Decimal(70000)  # 61번째(마지막) 종가
    assert r.raw_value == Decimal(70_000_000)
    assert r.eligible_value == r.raw_value  # 유동성 충분
    assert r.haircut == v.haircut_for(ZIGZAG_SIGMA) == Decimal("0.1136")  # 0.113537 → 보수적 올림
    assert r.collateral_value == (r.raw_value * (1 - r.haircut)).to_integral_value(rounding="ROUND_FLOOR")
    assert r.max_loan == (r.collateral_value * Decimal("0.70")).to_integral_value(rounding="ROUND_FLOOR")
    assert any("헤어컷" in s for s in r.reasons)


@pytest.mark.parametrize(
    "mkt,cap,expected",
    [
        ("KOSPI", "5000000000000", "A"),  # 5조 경계 포함
        ("KOSPI", "4999999999999", "B"),
        ("KOSDAQ", "9000000000000", "B"),  # KOSDAQ은 시총이 커도 A 아님
        ("KOSDAQ", "500000000000", "B"),  # 5,000억 경계 포함
        ("KOSDAQ", "499999999999", "C"),
    ],
)
def test_tier_by_market_cap(mkt, cap, expected):
    assert v.evaluate(market(market=mkt, market_cap=Decimal(cap)), 10).tier == expected


def test_unknown_market_cap_is_capped_at_c():
    r = v.evaluate(market(market_cap=None), 10)
    assert r.tier == "C" and any("시가총액 미상" in s for s in r.reasons)


def test_unknown_market_is_capped_at_c():
    r = v.evaluate(market(market="UNKNOWN"), 10)
    assert r.tier == "C" and any("시장 구분 미확인" in s for s in r.reasons)


@pytest.mark.parametrize(
    "high,expected",
    [
        (72200, "B"),  # σ ≈ 49% → A 상한(40%) 초과 → 최고 B
        (73600, "C"),  # σ ≈ 81% → B 상한(60%) 초과 → 최고 C
        (76000, "X"),  # σ ≈ 133% → 변동성 과다
    ],
)
def test_high_volatility_downgrades_blue_chip(high, expected):
    r = v.evaluate(market(closes=zigzag(70000, high, 61)), 10)
    assert r.tier == expected
    if expected == "X":
        assert any("변동성 과다" in s for s in r.reasons)
    else:
        assert any("변동성 높음" in s for s in r.reasons)


@pytest.mark.parametrize(
    "flags,reason",
    [
        ({"is_administrative": True}, "관리종목"),
        ({"is_halted": True}, "거래정지"),
        ({"is_liquidation_trading": True}, "정리매매"),
        ({"is_spac": True}, "스팩"),
    ],
)
def test_ineligible_flags_give_tier_x_and_zero_loan(flags, reason):
    r = v.evaluate(market(**flags), 100)
    assert r.tier == "X" and r.max_loan == 0 and not r.eligible
    assert reason in r.reasons


def test_konex_is_not_eligible():
    r = v.evaluate(market(market="KONEX"), 100)
    assert r.tier == "X" and any("KONEX" in s for s in r.reasons)


def test_short_history_is_not_eligible():
    r = v.evaluate(market(closes=zigzag(70000, 71000, 15)), 100)
    assert r.tier == "X" and any("시세 이력 부족" in s for s in r.reasons)


def test_moderately_short_history_is_flagged_but_allowed():
    r = v.evaluate(market(closes=zigzag(70000, 71000, 31)), 100)
    assert r.tier == "A" and any("표본 30일" in s for s in r.reasons)


def test_no_bars_is_tier_x():
    r = v.evaluate(MarketData(info=info(), bars=[]), 10)
    assert r.tier == "X" and r.max_loan == 0 and "시세 없음" in r.reasons


def test_operator_block_and_tier_floor():
    blocked = MarketData(info=info(), bars=bars(zigzag(70000, 71000, 61), END), is_blocked=True)
    assert v.evaluate(blocked, 10).tier == "X"
    floored = MarketData(info=info(), bars=bars(zigzag(70000, 71000, 61), END), tier_floor="B")
    assert v.evaluate(floored, 10).tier == "B"
    # 운영자 지정은 보수적 방향으로만: C 종목을 A로 올릴 수 없다
    small = MarketData(info=info(market_cap=Decimal("1000000000")), bars=bars(zigzag(70000, 71000, 61), END),
                       tier_floor="A")
    assert v.evaluate(small, 10).tier == "C"


# ── 유동성 ──

def test_liquidity_cap_zeroes_the_excess():
    # 20일 평균 거래대금 1억 → 한도 1,000만원. 평가액 7,100만원 중 1,000만원만 인정
    r = v.evaluate(market(value=Decimal("100000000")), 1000)
    assert r.liquidity_cap == Decimal("10000000")
    assert r.raw_value > r.liquidity_cap
    assert r.eligible_value == Decimal("10000000")
    assert r.collateral_value == (Decimal("10000000") * (1 - r.haircut)).to_integral_value(rounding="ROUND_FLOOR")
    assert any("유동성 부족" in s for s in r.reasons)


def test_zero_trading_value_is_not_eligible():
    r = v.evaluate(market(value=Decimal(0)), 10)
    assert r.tier == "X" and r.max_loan == 0


def test_qty_must_be_positive():
    with pytest.raises(ValueError):
        v.evaluate(market(), 0)


def test_amounts_are_decimal_whole_won():
    r = v.evaluate(market(value=Decimal("123456789")), 777)
    for amount in (r.raw_value, r.collateral_value, r.max_loan, r.liquidity_cap):
        assert isinstance(amount, Decimal) and amount == amount.to_integral_value()


# ── PykrxSource (pykrx 호출은 fixture로 모킹 — 네트워크 금지) ──

@pytest.fixture
def fake_pykrx():
    idx = pd.to_datetime([f"2026-03-{d:02d}" for d in (9, 10, 11, 12, 13)])
    ohlcv = pd.DataFrame(
        {"시가": [1] * 5, "고가": [1] * 5, "저가": [1] * 5, "종가": [70000, 71000, 70500, 0, 72000],
         "거래량": [100, 200, 300, 0, 400], "거래대금": [7_000_000, 14_200_000, 21_150_000, 0, 28_800_000]},
        index=idx,
    )
    calls = []

    def get_market_ohlcv(fromdate, todate, ticker):
        calls.append(("ohlcv", fromdate, todate, ticker))
        if ticker == "999999":
            raise ConnectionError("network down")
        return ohlcv.copy()

    def get_market_cap(fromdate, todate, ticker):
        return pd.DataFrame({"시가총액": [400_000_000_000_000]}, index=idx[-1:])

    return SimpleNamespace(
        calls=calls,
        get_market_ohlcv=get_market_ohlcv,
        get_market_cap=get_market_cap,
        get_market_ticker_name=lambda code: {"005930": "삼성전자", "123450": "하나스팩10호"}.get(code, ""),
        get_market_ticker_list=lambda d, market: {"KOSPI": ["005930"], "KOSDAQ": ["123450"]}.get(market, []),
    )


def test_pykrx_bars_are_decimal_and_skip_zero_close(fake_pykrx):
    src = PykrxSource(fake_pykrx)
    out = src.get_daily_bars("005930", END, 10)
    assert [b.close for b in out] == [Decimal(70000), Decimal(71000), Decimal(70500), Decimal(72000)]
    assert all(isinstance(b.close, Decimal) and isinstance(b.trading_value, Decimal) for b in out)
    assert out[-1].trading_value == Decimal(28_800_000)
    assert src.get_daily_bars("005930", END, 2)[0].date == date(2026, 3, 11)
    assert fake_pykrx.calls[0][2] == "20260313"


def test_pykrx_trading_value_falls_back_to_close_times_volume(fake_pykrx):
    original = fake_pykrx.get_market_ohlcv
    fake_pykrx.get_market_ohlcv = lambda *a: original(*a).drop(columns=["거래대금"])
    last = PykrxSource(fake_pykrx).get_daily_bars("005930", END, 1)[-1]
    assert last.trading_value == Decimal(72000 * 400)


def test_pykrx_stock_info(fake_pykrx):
    src = PykrxSource(fake_pykrx)
    i = src.get_stock_info("005930", END)
    assert (i.name, i.market, i.market_cap, i.is_spac, i.is_halted) == (
        "삼성전자", "KOSPI", Decimal("400000000000000"), False, False)
    assert src.get_stock_info("123450", END).is_spac


def test_pykrx_errors_become_price_source_error(fake_pykrx):
    src = PykrxSource(fake_pykrx)
    with pytest.raises(PriceSourceError):
        src.get_daily_bars("999999", END, 5)
    with pytest.raises(PriceSourceError):
        src.get_stock_info("000000", END)  # 미상장


def test_pykrx_market_cap_failure_degrades_to_tier_c(fake_pykrx):
    def boom(*a):
        raise RuntimeError("KRX 로그인 필요")

    fake_pykrx.get_market_cap = boom
    src = PykrxSource(fake_pykrx)
    i = src.get_stock_info("005930", END)
    assert i.market_cap is None and any("KRX 로그인" in n for n in i.notes)
