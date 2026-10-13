"""API 엔드투엔드: 계약 → 이자 → 상환/추가담보 → 마진콜 → 처분 → 정산.

시세는 InMemoryPriceSource, 시계는 FakeClock, 브로커는 MockBroker — 네트워크 없음.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.adapters.broker import MockBroker
from app.adapters.price import DailyBar, InMemoryPriceSource
from app.business_days import WeekdayCalendar
from app.db import make_engine
from app.main import create_app
from app.services import Deps
from factories import BIG_VALUE, FakeClock, bars, info, kst, zigzag

START = kst(2026, 3, 2)  # 월요일 10:00
BLUE = "005930"  # tier A, 종가 70,000
MID = "035720"  # tier B (KOSDAQ 1조)
SPAC = "123450"  # tier X


@dataclass
class Env:
    client: TestClient
    clock: FakeClock
    prices: InMemoryPriceSource

    def at(self, y: int, m: int, d: int, hh: int = 10, mm: int = 0) -> None:
        self.clock.set(kst(y, m, d, hh, mm))

    def close(self, code: str, day: date, price: int, volume: int = 1_000_000) -> None:
        self.prices.add_bar(code, DailyBar(day, Decimal(price), Decimal(volume), BIG_VALUE))

    def borrower(self, kyc: str = "VERIFIED") -> int:
        r = self.client.post("/borrowers", json={"name": "홍길동", "phone": "010-1234-5678",
                                                 "account_no": "12345678-01", "kyc_status": kyc})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def loan(self, amount: int = 40_000_000, qty: int = 1000, code: str = BLUE) -> dict:
        r = self.client.post("/loans", json={"borrower_id": self.borrower(),
                                             "collaterals": [{"code": code, "qty": qty}], "amount": str(amount)})
        assert r.status_code == 201, r.text
        return r.json()

    def get(self, loan_id: int) -> dict:
        r = self.client.get(f"/loans/{loan_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path: str, json: dict | None = None, expect: int = 200) -> dict:
        r = self.client.post(path, json=json)
        assert r.status_code == expect, r.text
        return r.json()

    def events(self, loan_id: int) -> list[str]:
        return [e["type"] for e in self.get(loan_id)["events"]]


@pytest.fixture
def env():
    clock = FakeClock(START)
    end = date(2026, 2, 27)
    prices = InMemoryPriceSource(
        bars={
            BLUE: bars(zigzag(70000, 71000, 61), end),
            MID: bars(zigzag(20000, 20200, 61), end),
            SPAC: bars(zigzag(2000, 2010, 61), end),
        },
        infos={
            BLUE: info(),
            MID: info(MID, "카카오", "KOSDAQ", Decimal("1000000000000")),
            SPAC: info(SPAC, "하나스팩10호", "KOSDAQ", Decimal("10000000000"), is_spac=True),
        },
    )
    deps = Deps(prices=prices, broker=MockBroker(prices, today=clock.today), calendar=WeekdayCalendar(),
                clock=clock)
    app = create_app(engine=make_engine("sqlite://"), deps=deps, scheduler_enabled=False)
    with TestClient(app) as client:
        yield Env(client, clock, prices)


D = Decimal


# ── 차주 · 평가 · 계약 ──

def test_kyc_gate(env):
    bid = env.borrower(kyc="PENDING")
    body = {"borrower_id": bid, "collaterals": [{"code": BLUE, "qty": 100}], "amount": "1000000"}
    r = env.client.post("/loans", json=body)
    assert r.status_code == 422 and "KYC" in r.json()["detail"]
    r = env.client.patch(f"/borrowers/{bid}/kyc", json={"kyc_status": "VERIFIED"})
    assert r.json()["kyc_status"] == "VERIFIED"
    assert env.client.post("/loans", json=body).status_code == 201


def test_valuation_endpoint(env):
    v = env.post("/valuations", {"code": BLUE, "qty": 1000})
    assert v["tier"] == "A" and v["ltv"] == "0.70" and v["haircut"] == "0.1136"
    assert v["raw_value"] == "70000000"
    assert v["collateral_value"] == "62048000"  # 7,000만 × (1 − 0.1136)
    assert v["max_loan"] == "43433600"  # × 0.70
    assert env.client.get("/admin/stocks").json()[0]["code"] == BLUE  # 종목 캐시
    assert env.client.post("/valuations", json={"code": "12", "qty": 1}).status_code == 422
    assert env.client.post("/valuations", json={"code": "999999", "qty": 1}).status_code == 503


def test_loan_rejections(env):
    bid = env.borrower()

    def create(collaterals, amount):
        return env.client.post("/loans", json={"borrower_id": bid, "collaterals": collaterals, "amount": amount})

    over = create([{"code": BLUE, "qty": 1000}], "43433601")
    assert over.status_code == 422 and "최대대출금" in over.json()["detail"]
    spac = create([{"code": BLUE, "qty": 1000}, {"code": SPAC, "qty": 100}], "1000000")
    assert spac.status_code == 422 and any("스팩" in r for r in spac.json()["reasons"])
    assert create([{"code": BLUE, "qty": 10}], "100.5").status_code == 422
    assert create([{"code": BLUE, "qty": 10}], "-1").status_code == 422
    r = env.client.post("/loans", json={"borrower_id": 999, "collaterals": [{"code": BLUE, "qty": 1}],
                                        "amount": "1"})
    assert r.status_code == 404


def test_create_loan(env):
    loan = env.loan(amount=40_000_000)
    assert loan["status"] == "NORMAL"
    assert loan["principal"] == "40000000" and loan["annual_rate"] == "0.20"
    assert loan["start_date"] == "2026-03-02" and loan["maturity_date"] == "2026-05-31"  # +90일
    assert loan["payoff_today"] == "40000000"  # 당일 상환이면 이자 0 (한편넣기)
    assert loan["latest_valuation"]["ratio"] == "1.7500"
    assert loan["collaterals"][0]["pledge_price"] == "70000"
    disbursed = next(e for e in loan["events"] if e["type"] == "DISBURSED")
    assert disbursed["payload"]["amount"] == "40000000"  # 선이자 공제 없음
    assert disbursed["ts"] == "2026-03-02T10:00:00+09:00"  # DB 왕복 후에도 KST


def test_duplicate_collateral_codes_are_merged(env):
    bid = env.borrower()
    r = env.client.post("/loans", json={"borrower_id": bid, "amount": "500000",
                                        "collaterals": [{"code": BLUE, "qty": 10}, {"code": BLUE, "qty": 5}]})
    assert r.status_code == 201 and r.json()["collaterals"][0]["qty"] == 15


# ── 이자 · 상환 ──

def test_interest_accrual_30_days(env):
    loan = env.loan(amount=10_000_000)
    env.at(2026, 4, 1, 0, 5)
    out = env.post("/admin/run-interest")
    assert out["through"] == "2026-03-31" and out["interest"] == "164383"
    again = env.post("/admin/run-interest")  # 같은 날 재실행 → 중복 적립 없음
    assert again["days_posted"] == 0
    detail = env.get(loan["id"])
    assert detail["accrued_interest"] == "164383"
    assert detail["interest"]["days_accrued"] == 30
    assert D(detail["interest"]["effective_annual_rate"]) <= D("0.20")
    assert D(detail["interest"]["total_charged"]) <= D(detail["interest"]["legal_max_interest"])
    assert env.client.post("/admin/run-interest", params={"through": "2026-04-01"}).status_code == 422


def test_repay_catches_up_interest_then_principal(env):
    loan = env.loan(amount=10_000_000)
    env.at(2026, 4, 1, 14)  # 스케줄러가 안 돌았어도 상환 시점에 어제까지 따라잡는다
    r = env.post(f"/loans/{loan['id']}/repay", {"amount": "200000"})
    assert (r["to_interest"], r["to_principal"]) == ("164383", "35617")
    assert r["loan"]["outstanding_principal"] == "9964383" and not r["fully_repaid"]

    payoff = env.get(loan["id"])["payoff_today"]
    assert payoff == "9964383"  # 오늘분 이자 없음, 수수료 없음
    r = env.post(f"/loans/{loan['id']}/repay", {"amount": payoff})
    assert r["fully_repaid"] and r["loan"]["status"] == "CLOSED"
    detail = env.get(loan["id"])
    assert detail["collaterals"][0]["released_qty"] == 1000 and detail["collaterals"][0]["active_qty"] == 0
    assert "COLLATERAL_RELEASED" in env.events(loan["id"]) and "CLOSED" in env.events(loan["id"])
    assert env.client.post(f"/loans/{loan['id']}/repay", json={"amount": "1"}).status_code == 409


def test_partial_principal_repayment_lowers_future_interest(env):
    loan = env.loan(amount=10_000_000)
    env.at(2026, 3, 12)  # 10일 경과
    env.post(f"/loans/{loan['id']}/repay", {"amount": str(5_000_000 + 54794)})  # 10일 이자 54,794 + 원금 500만
    env.at(2026, 4, 1, 0, 5)  # 이후 20일은 500만원 기준
    env.post("/admin/run-interest")
    detail = env.get(loan["id"])
    # 적수 = 1,000만×10 + 500만×20 = 2억 → 누적 109,589 중 54,794는 이미 수납
    assert D(detail["interest"]["total_charged"]) == D("109589")
    assert detail["accrued_interest"] == str(109589 - 54794)


def test_overpayment_is_rejected(env):
    loan = env.loan(amount=10_000_000)
    r = env.client.post(f"/loans/{loan['id']}/repay", json={"amount": "10000001"})
    assert r.status_code == 422


# ── 마진콜 · 추가담보 ──

def test_margin_call_then_cure_with_additional_collateral(env):
    loan = env.loan(amount=40_000_000)
    env.at(2026, 3, 3)
    env.close(BLUE, date(2026, 3, 3), 55000)  # 5,500만 / 4,000만 ≈ 137%
    env.post("/admin/run-valuation")
    detail = env.get(loan["id"])
    assert detail["status"] == "MARGIN_CALL" and detail["margin_call_deadline"] == "2026-03-05"
    assert detail["last_ratio"].startswith("1.37")

    r = env.post(f"/loans/{loan['id']}/collateral", {"collaterals": [{"code": MID, "qty": 500}]})
    assert r["status"] == "NORMAL"  # (5,500만 + 1,000만) / 4,000만 ≈ 162%
    assert "MARGIN_CALL_CURED" in [e["type"] for e in r["events"]]
    assert r["margin_call_deadline"] is None


def test_margin_call_cured_by_repayment(env):
    loan = env.loan(amount=40_000_000)
    env.at(2026, 3, 3)
    env.close(BLUE, date(2026, 3, 3), 55000)
    env.post("/admin/run-valuation")
    r = env.post(f"/loans/{loan['id']}/repay", {"amount": "5000000"})
    assert r["loan"]["status"] == "NORMAL"


def test_collateral_cannot_be_added_to_pending_loan(env):
    loan = env.loan()
    env.post(f"/loans/{loan['id']}/liquidate", {"note": "test"})
    r = env.client.post(f"/loans/{loan['id']}/collateral", json={"collaterals": [{"code": MID, "qty": 1}]})
    assert r.status_code == 409


# ── 처분 ──

def test_grace_expiry_then_partial_liquidation(env):
    loan = env.loan(amount=40_000_000)
    env.at(2026, 3, 6, 15, 40)  # 금요일 장후
    env.close(BLUE, date(2026, 3, 6), 54000)
    env.post("/admin/run-valuation")
    assert env.get(loan["id"])["margin_call_deadline"] == "2026-03-10"  # 금 + 2영업일 = 화

    env.at(2026, 3, 9, 15, 40)
    env.post("/admin/run-valuation")
    assert env.get(loan["id"])["status"] == "MARGIN_CALL"  # 아직 유예 중

    env.at(2026, 3, 10, 15, 40)
    env.close(BLUE, date(2026, 3, 10), 54000)
    env.post("/admin/run-valuation")
    detail = env.get(loan["id"])
    assert detail["status"] == "LIQUIDATION_PENDING"
    assert detail["liquidation_reasons"] == ["MARGIN_CALL_EXPIRED"]

    env.at(2026, 3, 11, 8, 45)  # 다음 거래일 동시호가
    out = env.post("/admin/run-liquidation")
    assert out["loans"][str(loan["id"])]["mode"] == "PARTIAL"
    detail = env.get(loan["id"])
    order = detail["liquidation_orders"][0]
    assert order["status"] == "FILLED" and order["fill_price"] == "54000"  # MockBroker: 전일 종가
    assert 0 < order["fill_qty"] < 1000
    assert detail["collaterals"][0]["liquidated_qty"] == order["fill_qty"]
    assert detail["status"] == "NORMAL" and D(detail["last_ratio"]) >= D("1.50")
    assert D(detail["repaid_principal"]) > 0
    assert {"LIQ_ORDER", "FILL"} <= set(env.events(loan["id"]))


def test_crash_full_liquidation_records_shortfall(env):
    loan = env.loan(amount=40_000_000)
    env.at(2026, 3, 3, 15, 40)
    env.close(BLUE, date(2026, 3, 3), 35000)  # 3,500만 / 4,000만 = 87.5%
    env.post("/admin/run-valuation")
    assert env.get(loan["id"])["liquidation_reasons"] == ["RATIO_BREACH"]

    env.at(2026, 3, 4, 8, 45)
    out = env.post("/admin/run-liquidation")
    assert out["loans"][str(loan["id"])]["mode"] == "FULL"
    detail = env.get(loan["id"])
    assert detail["status"] == "CLOSED"
    assert detail["liquidation_orders"][0]["fill_qty"] == 1000
    # 순매도대금 = 3,500만 − 매도비용 87,500 → 이자(2일) 먼저, 나머지 원금
    shortfall = D(detail["shortfall"])
    assert shortfall == D(40_000_000) + D(detail["repaid_interest"]) - (D(35_000_000) - D(87_500))
    assert "SHORTFALL_RECORDED" in env.events(loan["id"])
    dash = env.client.get("/dashboard").json()
    assert dash["totals"]["shortfall"] == str(shortfall)


def test_maturity_notice_then_expiry_payoff_with_surplus(env):
    loan = env.loan(amount=40_000_000)  # 만기 5/31
    env.at(2026, 5, 25, 15, 40)  # D-6 (D-7 당일 작업이 빠져도 만기 전이면 발송)
    env.post("/admin/run-valuation")
    assert env.events(loan["id"]).count("MATURITY_NOTICE") == 1
    notice = next(e for e in env.get(loan["id"])["events"] if e["type"] == "MATURITY_NOTICE")
    assert "41,972,602원" in notice["payload"]["message"]  # 원금 + 90일(3/2~5/30) 이자, 만기일 당일분 없음
    env.post("/admin/run-valuation")
    assert env.events(loan["id"]).count("MATURITY_NOTICE") == 1  # 중복 없음

    env.at(2026, 6, 7, 15, 40)  # 만기 + 7일 → 아직 유예
    env.post("/admin/run-valuation")
    assert env.get(loan["id"])["status"] == "NORMAL"
    env.at(2026, 6, 8, 15, 40)  # + 8일 → 처분 사유
    env.post("/admin/run-valuation")
    assert env.get(loan["id"])["liquidation_reasons"] == ["MATURITY_EXPIRED"]

    env.at(2026, 6, 9, 8, 45)
    out = env.post("/admin/run-liquidation")
    assert out["loans"][str(loan["id"])]["mode"] == "PAYOFF"
    detail = env.get(loan["id"])
    assert detail["status"] == "CLOSED" and detail["accrued_interest"] == "0"
    assert detail["repaid_principal"] == "40000000"
    # 3/2 ~ 6/8 (99일) 이자 전액 수납
    assert detail["repaid_interest"] == str(40_000_000 * 99 * 20 // 100 // 365)
    c = detail["collaterals"][0]
    assert c["liquidated_qty"] + c["released_qty"] == 1000 and c["released_qty"] > 0  # 남은 주식 반환
    assert D(detail["surplus_returned"]) >= 0
    assert "SURPLUS_RETURNED" in env.events(loan["id"])


def test_manual_liquidation(env):
    loan = env.loan(amount=20_000_000)
    r = env.post(f"/loans/{loan['id']}/liquidate", {"note": "차주 요청 처분"})
    assert r["status"] == "CLOSED" and r["liquidation_orders"][0]["mode"] == "PAYOFF"
    assert r["liquidation_reasons"] == ["MANUAL"]
    assert env.client.post(f"/loans/{loan['id']}/liquidate", json={}).status_code == 409


def test_operator_block_triggers_ineligible_liquidation(env):
    loan = env.loan()
    r = env.client.put(f"/admin/stocks/{BLUE}", json={"is_blocked": True})
    assert r.json()["tier"] == "X" and r.json()["is_blocked"]
    env.post("/admin/run-valuation", None)
    detail = env.get(loan["id"])
    assert detail["status"] == "LIQUIDATION_PENDING"
    assert detail["liquidation_reasons"] == ["INELIGIBLE_COLLATERAL"]
    listed = env.client.get("/loans", params={"status": "LIQUIDATION_PENDING"}).json()
    assert [x["id"] for x in listed] == [loan["id"]]


def test_tier_refresh_and_halted_stock_cannot_be_sold(env):
    loan = env.loan()
    env.prices.set_info(info(is_halted=True))
    env.at(2026, 3, 3, 15, 40)
    env.close(BLUE, date(2026, 3, 3), 70000, volume=0)
    env.client.post("/admin/run-valuation", params={"refresh_tiers": "true"})
    detail = env.get(loan["id"])
    assert detail["status"] == "LIQUIDATION_PENDING" and "TIER_CHANGED" in env.events(loan["id"])

    env.at(2026, 3, 4, 8, 45)
    env.post("/admin/run-liquidation")
    detail = env.get(loan["id"])
    assert detail["status"] == "LIQUIDATION_PENDING"  # 거래정지 → 주문 불가, 다음 거래일 재시도
    assert detail["liquidation_orders"] == [] and "ORDER_FAILED" in env.events(loan["id"])


def test_price_outage_does_not_change_state(env):
    loan = env.loan()
    env.prices.set_bars(BLUE, [])  # 시세 소스 장애
    out = env.post("/admin/run-valuation")
    assert out["skipped"] == 1 and env.get(loan["id"])["status"] == "NORMAL"


# ── 대시보드 · 보안 ──

def test_dashboard(env):
    healthy = env.loan(amount=10_000_000)
    risky = env.loan(amount=40_000_000)
    env.at(2026, 3, 3)
    env.close(BLUE, date(2026, 3, 3), 55000)
    env.post("/admin/run-valuation")  # risky만 마진콜 (healthy는 5,500만/1,000만)
    d = env.client.get("/dashboard").json()
    assert d["by_status"]["NORMAL"]["count"] == 1 and d["by_status"]["MARGIN_CALL"]["count"] == 1
    assert d["totals"]["outstanding_principal"] == "50000000"
    assert [r["loan_id"] for r in d["at_risk"]] == [risky["id"]]
    assert d["risk_bands"]["120~140%"]["count"] == 1 and d["risk_bands"]["≥200%"]["count"] == 1
    assert healthy["id"] != risky["id"]
    page = env.client.get("/")
    assert page.status_code == 200 and "StockPawn" in page.text


def test_api_key_protects_data_but_not_static_page(env, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    assert env.client.get("/dashboard").status_code == 401
    assert env.client.get("/dashboard", headers={"X-API-Key": "wrong"}).status_code == 401
    assert env.client.get("/dashboard", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert env.client.get("/").status_code == 200


def test_openapi_lists_spec_endpoints(env):
    paths = env.client.get("/openapi.json").json()["paths"]
    for p in ["/borrowers", "/valuations", "/loans", "/loans/{loan_id}", "/loans/{loan_id}/repay",
              "/loans/{loan_id}/collateral", "/loans/{loan_id}/liquidate", "/dashboard",
              "/admin/run-valuation", "/admin/run-interest"]:
        assert p in paths
