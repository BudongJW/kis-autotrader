"""장마감 선별 청산 테스트 — ETF 추세·방어는 보유, 급등주만 청산.

검은월요일 후 관찰(6-09~11): 봇이 매 마감 전량 청산하는 데이트레이딩 구조라
수수료에 갇혀 net 보합. selective로 ETF는 다음날 보유해 churn을 줄인다.
"""
from __future__ import annotations

import src.bot.single_run as sr


def _patch_universe(monkeypatch):
    # ETF 유니버스 = {395160 롱, 114800 인버스, 152380·214980 방어}
    monkeypatch.setattr(sr, "load_universe", lambda: [{"symbol": "395160"}])
    monkeypatch.setattr(sr, "load_inverse_universe", lambda: [{"symbol": "114800"}])
    monkeypatch.setattr(sr, "load_defensive_universe",
                        lambda: [{"symbol": "152380"}, {"symbol": "214980"}])
    monkeypatch.setattr(sr, "load_income_universe", lambda: [{"symbol": "498400"}])
    monkeypatch.setattr(sr, "load_leveraged_config", lambda: {"universe": []})


def test_selective_keeps_etf_liquidates_surge(monkeypatch):
    _patch_universe(monkeypatch)
    holdings = {"214980": 1, "395160": 2, "005930": 10}  # 단기채·ETF·급등주(삼성=비유니버스)
    liq = sr.eod_liquidation_targets(holdings, cfg={"kr_eod_liquidation": "selective"})
    assert liq == {"005930": 10}            # 급등주만 청산
    assert "214980" not in liq and "395160" not in liq  # ETF·방어는 보유


def test_all_mode_liquidates_everything(monkeypatch):
    _patch_universe(monkeypatch)
    holdings = {"214980": 1, "395160": 2, "005930": 10}
    liq = sr.eod_liquidation_targets(holdings, cfg={"kr_eod_liquidation": "all"})
    assert liq == holdings                  # 전량 청산(기존 동작)


def test_default_is_selective(monkeypatch):
    _patch_universe(monkeypatch)
    # cfg 미지정 시 load_config 기본값 사용 — selective여야 (config에 명시)
    monkeypatch.setattr(sr, "load_config", lambda: {"kr_eod_liquidation": "selective"})
    liq = sr.eod_liquidation_targets({"214980": 1, "005930": 10})
    assert liq == {"005930": 10}


def test_defensive_bond_held_overnight(monkeypatch):
    """6-10 사례: 단기채(214980)가 마감청산되던 문제 — 이제 보유."""
    _patch_universe(monkeypatch)
    liq = sr.eod_liquidation_targets({"214980": 1}, cfg={"kr_eod_liquidation": "selective"})
    assert liq == {}                        # 방어자산은 청산 안 함
