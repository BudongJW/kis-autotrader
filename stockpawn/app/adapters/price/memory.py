"""메모리 시세 소스 — 테스트와 리플레이 시뮬레이터용.

get_daily_bars(end=...)가 end 이후 봉을 절대 돌려주지 않으므로 리플레이에서
미래 시세를 엿보는(look-ahead) 실수를 구조적으로 막는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from app.adapters.price.base import DailyBar, PriceSource, PriceSourceError, StockInfo


class InMemoryPriceSource(PriceSource):
    def __init__(
        self,
        bars: dict[str, Iterable[DailyBar]] | None = None,
        infos: dict[str, StockInfo] | None = None,
    ) -> None:
        self._bars: dict[str, list[DailyBar]] = {}
        self._infos: dict[str, StockInfo] = dict(infos or {})
        for code, series in (bars or {}).items():
            self.set_bars(code, series)

    def set_bars(self, code: str, bars: Iterable[DailyBar]) -> None:
        self._bars[code] = sorted(bars, key=lambda b: b.date)

    def add_bar(self, code: str, bar: DailyBar) -> None:
        series = [b for b in self._bars.get(code, []) if b.date != bar.date]
        series.append(bar)
        self.set_bars(code, series)

    def set_info(self, info: StockInfo) -> None:
        self._infos[info.code] = info

    def get_daily_bars(self, code: str, end: date, count: int) -> list[DailyBar]:
        series = [b for b in self._bars.get(code, []) if b.date <= end]
        return series[-count:] if count > 0 else []

    def get_stock_info(self, code: str, as_of: date) -> StockInfo:
        try:
            return self._infos[code]
        except KeyError:
            raise PriceSourceError(f"{code}: 종목 정보 없음") from None
