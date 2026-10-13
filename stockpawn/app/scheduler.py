"""APScheduler 잡 (KST).

  00:05 매일           전일분 이자 적립 (주말·공휴일에도 이자는 달력일 기준으로 쌓인다)
  08:45 거래일          LIQUIDATION_PENDING 처분 주문 (장 시작 동시호가 08:30~09:00 접수)
  09:00~15:30 10분마다  담보비율 평가 (거래일만)
  15:40 거래일          장 마감 후 평가 + 담보종목 등급 재산정

잡 함수는 테스트할 수 있게 실행 결과 문자열/요약을 돌려준다.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app import config, services
from app.clock import KST

log = logging.getLogger("stockpawn.scheduler")


def job_accrue_interest(engine: Engine, deps: services.Deps) -> dict:
    with Session(engine) as session:
        result = services.accrue_all(session, deps)
    log.info("accrue_interest %s", result)
    return result


def job_intraday_valuation(engine: Engine, deps: services.Deps) -> dict | str:
    now = deps.now()
    if not deps.calendar.is_business_day(now.date()):
        return "skip:holiday"
    if not (config.MARKET_OPEN <= now.time() <= config.MARKET_CLOSE):
        return "skip:market_closed"
    with Session(engine) as session:
        result = services.evaluate_all(session, deps, now)
    log.info("intraday_valuation %s", result)
    return result


def job_post_close_valuation(engine: Engine, deps: services.Deps) -> dict | str:
    now = deps.now()
    if not deps.calendar.is_business_day(now.date()):
        return "skip:holiday"
    with Session(engine) as session:
        result = services.evaluate_all(session, deps, now, refresh_tiers=True)
    log.info("post_close_valuation %s", result)
    return result


def job_liquidation_orders(engine: Engine, deps: services.Deps) -> dict | str:
    now = deps.now()
    if not deps.calendar.is_business_day(now.date()):
        return "skip:holiday"
    with Session(engine) as session:
        result = services.liquidate_pending(session, deps, now)
    log.info("liquidation_orders %s", result)
    return result


def build_scheduler(engine: Engine, deps: services.Deps) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=KST)
    common = {"args": [engine, deps], "max_instances": 1, "coalesce": True, "misfire_grace_time": 600}
    it, lt, pt = config.INTEREST_JOB_TIME, config.LIQUIDATION_JOB_TIME, config.POST_CLOSE_JOB_TIME
    sched.add_job(job_accrue_interest, CronTrigger(hour=it.hour, minute=it.minute, timezone=KST),
                  id="accrue_interest", **{**common, "misfire_grace_time": 6 * 3600})
    sched.add_job(job_liquidation_orders,
                  CronTrigger(day_of_week="mon-fri", hour=lt.hour, minute=lt.minute, timezone=KST),
                  id="liquidation_orders", **common)
    sched.add_job(job_intraday_valuation,
                  CronTrigger(day_of_week="mon-fri", hour=f"{config.MARKET_OPEN.hour}-{config.MARKET_CLOSE.hour}",
                              minute=f"*/{config.MONITOR_INTERVAL_MINUTES}", timezone=KST),
                  id="intraday_valuation", **common)
    sched.add_job(job_post_close_valuation,
                  CronTrigger(day_of_week="mon-fri", hour=pt.hour, minute=pt.minute, timezone=KST),
                  id="post_close_valuation", **common)
    return sched
