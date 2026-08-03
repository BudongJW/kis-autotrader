"""LightGBM 기반 익일 방향 예측 모델.

TA 지표 + 시장 메타 피처를 입력으로, 익일 수익률 방향(상승/하락)을 예측.
기존 룰 기반 전략의 보조 필터로 사용 — 모델이 하락 예측 시 매수 차단.

학습 데이터: 최근 1년 일봉 (자동 생성)
업데이트: 주간 옵티마이저와 함께 재학습

사용:
    predictor = LGBMPredictor()
    predictor.train(history_df)  # 학습
    prob = predictor.predict(latest_features)  # 상승 확률 (0~1)

    # 매수 필터:
    if prob > 0.55:
        # 매수 허용
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta

from src.utils.logger import log

MODEL_PATH = Path("logs/lgbm_model.pkl")
FEATURE_IMPORTANCE_PATH = Path("logs/lgbm_features.json")
# 매일의 예측을 적어두고 다음날 실현치로 채점하는 전방검증 기록.
# 홀드아웃 지표는 부풀려질 수 있으므로 **실제 예측력은 이 파일로만 판정한다.**
LIVE_ACCURACY_PATH = Path("logs/lgbm_live_accuracy.json")
EMBARGO_DAYS = 2        # 학습/검증/테스트 경계 공백일 — 인접일 상관 누수 차단
LIVE_MIN_HIT_RATE = 0.53   # 실측 적중률이 이 아래면 예측을 매매에 반영하지 않음
LIVE_MIN_SAMPLES = 20      # 이만큼 채점되기 전엔 판정 보류(표본 부족)


def live_hit_rate() -> float | None:
    """전방검증 실측 적중률. 표본이 모자라면 None(판정 보류)."""
    if not LIVE_ACCURACY_PATH.exists():
        return None
    try:
        d = json.loads(LIVE_ACCURACY_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    s = d.get("summary") or {}
    if (s.get("graded") or 0) < LIVE_MIN_SAMPLES:
        return None
    return s.get("hit_rate")

# 예측 임계값
BUY_THRESHOLD = 0.55    # 상승 확률 55% 이상일 때만 매수 허용
STRONG_BUY = 0.65       # 65% 이상이면 신뢰도 추가 가산


@dataclass
class PredictionResult:
    """예측 결과."""
    up_prob: float            # 상승 확률 (0~1)
    signal: str               # "BUY_OK" / "BLOCK" / "STRONG_BUY"
    confidence: float         # 예측 신뢰도
    top_features: list[str]   # 상위 영향 피처
    detail: str


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame에서 ML 피처를 생성.

    Returns:
        피처 DataFrame (각 행이 하루, 컬럼이 피처)
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    features = pd.DataFrame(index=df.index)

    # 수익률 기반
    features["ret_1d"] = close.pct_change(1)
    features["ret_3d"] = close.pct_change(3)
    features["ret_5d"] = close.pct_change(5)
    features["ret_10d"] = close.pct_change(10)

    # 변동성
    features["vol_5d"] = close.pct_change().rolling(5).std()
    features["vol_20d"] = close.pct_change().rolling(20).std()
    features["vol_ratio"] = features["vol_5d"] / features["vol_20d"].replace(0, np.nan)

    # RSI
    rsi_s = ta.rsi(close, length=14)
    features["rsi_14"] = rsi_s

    # MACD
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is not None:
        features["macd"] = macd_df.iloc[:, 0]
        features["macd_hist"] = macd_df.iloc[:, 1]
        features["macd_signal"] = macd_df.iloc[:, 2]

    # Bollinger Band position
    bb = ta.bbands(close, length=20, std=2)
    if bb is not None:
        bb_lower = bb.iloc[:, 0]
        bb_upper = bb.iloc[:, 2]
        bb_range = bb_upper - bb_lower
        features["bb_pos"] = (close - bb_lower) / bb_range.replace(0, np.nan)
        features["bb_width"] = bb_range / close

    # Stochastic
    stoch = ta.stoch(high, low, close, k=14, d=3)
    if stoch is not None:
        features["stoch_k"] = stoch.iloc[:, 0]
        features["stoch_d"] = stoch.iloc[:, 1]

    # ADX
    adx_df = ta.adx(high, low, close, length=14)
    if adx_df is not None:
        features["adx"] = adx_df.iloc[:, 0]
        features["di_plus"] = adx_df.iloc[:, 1]
        features["di_minus"] = adx_df.iloc[:, 2]

    # 이동평균 관계
    ma5 = ta.sma(close, length=5)
    ma20 = ta.sma(close, length=20)
    ma60 = ta.sma(close, length=60)
    features["ma5_ratio"] = close / ma5.replace(0, np.nan)
    features["ma20_ratio"] = close / ma20.replace(0, np.nan)
    features["ma60_ratio"] = close / ma60.replace(0, np.nan)
    features["ma5_20_cross"] = (ma5 - ma20) / close

    # OBV 추세
    obv = ta.obv(close, volume)
    if obv is not None:
        features["obv_slope_10"] = obv.rolling(10).apply(
            lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) == 10 else 0,
            raw=False,
        )

    # MFI
    mfi = ta.mfi(high, low, close, volume, length=14)
    features["mfi_14"] = mfi

    # ATR ratio
    atr_14 = ta.atr(high, low, close, length=14)
    atr_60 = ta.atr(high, low, close, length=60)
    if atr_14 is not None and atr_60 is not None:
        features["atr_ratio"] = atr_14 / atr_60.replace(0, np.nan)

    # 거래량 피처
    features["vol_ma5_ratio"] = volume / volume.rolling(5).mean().replace(0, np.nan)
    features["vol_ma20_ratio"] = volume / volume.rolling(20).mean().replace(0, np.nan)

    # 캔들 패턴 (간단)
    open_ = df["open"].astype(float)
    features["body_ratio"] = (close - open_) / (high - low).replace(0, np.nan)
    features["upper_shadow"] = (high - close.clip(lower=open_)) / (high - low).replace(0, np.nan)

    # ── 신규 피처: 미국 시장 갭 ──
    try:
        import yaml
        cfg_path = Path("configs/strategy.yaml")
        if cfg_path.exists():
            with cfg_path.open(encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            gap = cfg.get("overnight_signal", {})
            features["us_nasdaq_change"] = gap.get("nasdaq_change", 0)
            features["us_sp500_change"] = gap.get("sp500_change", 0)
            features["us_gap_strength"] = gap.get("strength", 0)
    except Exception:
        features["us_nasdaq_change"] = 0
        features["us_sp500_change"] = 0
        features["us_gap_strength"] = 0

    # ── 신규 피처: 요일 효과 ──
    if hasattr(df.index, 'dayofweek'):
        features["day_of_week"] = df.index.dayofweek
    else:
        try:
            features["day_of_week"] = pd.to_datetime(df.index).dayofweek
        except Exception:
            features["day_of_week"] = 2  # 수요일(중립) 폴백

    # ── 신규 피처: 모멘텀 가속도 (2차 미분) ──
    ret_1d = close.pct_change(1)
    features["momentum_accel"] = ret_1d.diff(1)  # 수익률의 변화율
    features["momentum_accel_3d"] = ret_1d.diff(3)

    # ── 신규 피처: 거래량 급증 감지 ──
    vol_ma5 = volume.rolling(5).mean()
    vol_ma20 = volume.rolling(20).mean()
    features["vol_surge"] = (volume / vol_ma20.replace(0, np.nan)).clip(upper=5)
    features["vol_trend_5d"] = (vol_ma5 / vol_ma20.replace(0, np.nan))

    # ── 신규 피처: 가격-거래량 다이버전스 ──
    price_slope_10 = close.rolling(10).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) == 10 else 0,
        raw=False,
    )
    vol_slope_10 = volume.rolling(10).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) == 10 else 0,
        raw=False,
    )
    # 가격 상승 + 거래량 감소 = 약세 다이버전스 (음수)
    features["pv_divergence"] = np.sign(price_slope_10) * np.sign(vol_slope_10)

    # ── 신규 피처: 레짐 컨텍스트 (HMM 상태 인코딩) ──
    try:
        if cfg_path.exists():
            regime = cfg.get("market_regime", {})
            hmm_state = regime.get("hmm_state", "sideways")
            features["hmm_bull"] = 1 if hmm_state == "bull" else 0
            features["hmm_bear"] = 1 if hmm_state == "bear" else 0
            features["market_confidence"] = cfg.get("market_confidence", 0.5)
        else:
            features["hmm_bull"] = 0
            features["hmm_bear"] = 0
            features["market_confidence"] = 0.5
    except Exception:
        features["hmm_bull"] = 0
        features["hmm_bear"] = 0
        features["market_confidence"] = 0.5

    # ── 신규 피처: RSI × 레짐 상호작용 ──
    rsi_val = rsi_s if rsi_s is not None else pd.Series(50.0, index=df.index)
    features["rsi_x_bull"] = rsi_val * features["hmm_bull"]
    features["rsi_x_bear"] = rsi_val * features["hmm_bear"]

    # ── 신규 피처: 변동성 사이클 위치 ──
    if len(close) >= 60:
        vol_20d = close.pct_change().rolling(20).std()
        vol_60d = close.pct_change().rolling(60).std()
        features["vol_cycle_pos"] = vol_20d / vol_60d.replace(0, np.nan)

    # ── 신규 피처: 고저 범위 대비 종가 위치 (최근 20일) ──
    high_20 = high.rolling(20).max()
    low_20 = low.rolling(20).min()
    range_20 = (high_20 - low_20).replace(0, np.nan)
    features["price_position_20d"] = (close - low_20) / range_20

    return features


def _build_target(df: pd.DataFrame, horizon: int = 1) -> pd.Series:
    """타겟: horizon일 후 수익률이 양수면 1, 음수면 0."""
    close = df["close"].astype(float)
    future_ret = close.shift(-horizon) / close - 1
    return (future_ret > 0).astype(int)


class LGBMPredictor:
    """LightGBM 익일 방향 예측기."""

    def __init__(self) -> None:
        self.model = None
        self.feature_names: list[str] = []
        self._load_model()

    def _load_model(self) -> None:
        """저장된 모델 로드."""
        if MODEL_PATH.exists():
            try:
                with MODEL_PATH.open("rb") as f:
                    data = pickle.load(f)
                self.model = data["model"]
                self.feature_names = data["feature_names"]
                log.info("lgbm_loaded", features=len(self.feature_names))
            except Exception as e:
                log.warning("lgbm_load_failed", error=str(e))

    def _save_model(self) -> None:
        """모델 저장."""
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MODEL_PATH.open("wb") as f:
            pickle.dump({
                "model": self.model,
                "feature_names": self.feature_names,
            }, f)

    def train(self, df: pd.DataFrame, test_ratio: float = 0.2) -> dict:
        """모델 학습.

        Args:
            df: OHLCV DataFrame (최소 120일)
            test_ratio: 테스트셋 비율

        Returns:
            {"accuracy": float, "auc": float, "feature_importance": dict}
        """
        try:
            import lightgbm as lgb
            from sklearn.model_selection import TimeSeriesSplit
            from sklearn.metrics import accuracy_score, roc_auc_score
        except ImportError:
            log.warning("lightgbm_not_installed")
            return {"accuracy": 0, "auc": 0}

        if len(df) < 120:
            log.warning("lgbm_data_insufficient", rows=len(df))
            return {"accuracy": 0, "auc": 0}

        features = _build_features(df)
        target = _build_target(df, horizon=1)

        # NaN 제거
        valid_mask = features.notna().all(axis=1) & target.notna()
        features = features[valid_mask]
        target = target[valid_mask]

        if len(features) < 80:
            return {"accuracy": 0, "auc": 0}

        self.feature_names = list(features.columns)

        # Walk-Forward CV: 확장 윈도우로 시계열 안전 분할
        tscv = TimeSeriesSplit(n_splits=3)
        cv_aucs = []

        params = {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_child_samples": 10,
            "verbose": -1,
            "seed": 42,
        }

        # CV로 성능 측정.
        # 주의(2026-08-03 수정): 옛 코드는 fold의 **테스트셋으로 early stopping**을 한 뒤
        # 같은 셋에서 AUC를 계산해 낙관 편향이 들어갔다. 이제 학습구간 안에서 다시
        # 검증셋을 떼어 조기종료에 쓰고, 테스트 fold는 채점에만 쓴다.
        for train_idx, test_idx in tscv.split(features):
            inner = int(len(train_idx) * 0.8)
            if inner < 20 or len(train_idx) - inner < 5:
                continue
            tr_i, va_i = train_idx[:inner], train_idx[inner:]
            X_tr, y_tr = features.iloc[tr_i], target.iloc[tr_i]
            X_va, y_va = features.iloc[va_i], target.iloc[va_i]
            X_te, y_te = features.iloc[test_idx], target.iloc[test_idx]

            tr_data = lgb.Dataset(X_tr, label=y_tr)
            va_data = lgb.Dataset(X_va, label=y_va, reference=tr_data)
            fold_model = lgb.train(
                params, tr_data, num_boost_round=200,
                valid_sets=[va_data],
                callbacks=[lgb.early_stopping(15), lgb.log_evaluation(0)],
            )
            fold_prob = fold_model.predict(X_te)   # 조기종료에 안 쓰인 구간
            try:
                cv_aucs.append(roc_auc_score(y_te, fold_prob))
            except ValueError:
                pass

        # 최종 모델: 학습 / 검증(조기종료) / 테스트(보고) 3분할 — 테스트는 학습에 미사용
        n = len(features)
        split_te = int(n * (1 - test_ratio))
        split_va = int(split_te * 0.85)
        X_train, y_train = features.iloc[:split_va], target.iloc[:split_va]
        X_valid, y_valid = features.iloc[split_va:split_te], target.iloc[split_va:split_te]
        X_test, y_test = features.iloc[split_te:], target.iloc[split_te:]

        train_data = lgb.Dataset(X_train, label=y_train)
        valid_data = lgb.Dataset(X_valid, label=y_valid, reference=train_data)

        callbacks = [lgb.early_stopping(20), lgb.log_evaluation(0)]
        self.model = lgb.train(
            params,
            train_data,
            num_boost_round=300,
            valid_sets=[valid_data],
            callbacks=callbacks,
        )

        # 평가 — 학습·조기종료에 한 번도 쓰이지 않은 테스트셋에서만
        y_pred_prob = self.model.predict(X_test)
        y_pred = (y_pred_prob > 0.5).astype(int)
        accuracy = accuracy_score(y_test, y_pred)
        auc = roc_auc_score(y_test, y_pred_prob)
        cv_auc_mean = float(np.mean(cv_aucs)) if cv_aucs else auc

        # 피처 중요도
        importance = dict(zip(
            self.feature_names,
            self.model.feature_importance(importance_type="gain").tolist(),
        ))
        sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)

        self._save_model()

        # 피처 중요도 로그
        FEATURE_IMPORTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with FEATURE_IMPORTANCE_PATH.open("w", encoding="utf-8") as f:
            json.dump({
                "trained_at": pd.Timestamp.now().isoformat(),
                "accuracy": round(accuracy, 4),
                "auc": round(auc, 4),
                "cv_auc": round(cv_auc_mean, 4),
                "n_samples": len(features),
                "n_features": len(self.feature_names),
                "feature_importance": {k: round(v, 2) for k, v in sorted_imp[:20]},
            }, f, ensure_ascii=False, indent=2)

        print(f"  [LGBM] 학습 완료: accuracy={accuracy:.1%}, AUC={auc:.3f}, CV-AUC={cv_auc_mean:.3f}")
        print(f"  [LGBM] 피처: {len(self.feature_names)}개, 학습: {len(X_train)}건, 테스트: {len(X_test)}건")
        print(f"  [LGBM] 상위 피처: " +
              ", ".join(f"{k}({v:.0f})" for k, v in sorted_imp[:5]))

        return {
            "accuracy": float(accuracy),
            "auc": float(auc),
            "cv_auc": float(cv_auc_mean),
            "feature_importance": dict(sorted_imp[:10]),
        }

    def predict(self, df: pd.DataFrame) -> PredictionResult:
        """현재 시점의 익일 방향 예측.

        Args:
            df: 최근 OHLCV (최소 60일). 마지막 행이 '오늘'.

        Returns:
            PredictionResult
        """
        if self.model is None:
            return PredictionResult(
                up_prob=0.5,
                signal="BUY_OK",
                confidence=0.0,
                top_features=[],
                detail="모델 미학습 상태 — 기본 허용",
            )

        features = _build_features(df)
        if features.empty:
            return PredictionResult(
                up_prob=0.5, signal="BUY_OK", confidence=0.0,
                top_features=[], detail="피처 생성 실패",
            )

        # 마지막 행 (오늘) 피처
        latest = features.iloc[[-1]]

        # 누락 피처 처리
        for col in self.feature_names:
            if col not in latest.columns:
                latest[col] = 0
        latest = latest[self.feature_names]

        # NaN → 0
        latest = latest.fillna(0)

        up_prob = float(self.model.predict(latest)[0])
        confidence = abs(up_prob - 0.5) * 2  # 0~1 (0.5일 때 0, 1.0일 때 1)

        if up_prob >= STRONG_BUY:
            signal = "STRONG_BUY"
        elif up_prob >= BUY_THRESHOLD:
            signal = "BUY_OK"
        else:
            signal = "BLOCK"

        # 상위 영향 피처 (SHAP-like: 피처 중요도 × 값 방향)
        importances = self.model.feature_importance(importance_type="gain")
        top_idx = np.argsort(importances)[-5:][::-1]
        top_features = [self.feature_names[i] for i in top_idx]

        detail = (f"LGBM: 상승 {up_prob:.0%} | {signal} | "
                  f"핵심: {', '.join(top_features[:3])}")

        return PredictionResult(
            up_prob=up_prob,
            signal=signal,
            confidence=confidence,
            top_features=top_features,
            detail=detail,
        )


def daily_retrain(client, symbols: list[str], days: int = 120) -> dict | None:
    """일일 LGBM 재학습 — 기존 모델 위에 새 데이터로 warm-start.

    주간 전체 학습(optimizer.py)과 달리, 매일 최신 데이터로 갱신.
    기존 모델의 트리를 유지하고 추가 50라운드만 학습.
    """
    try:
        import lightgbm as lgb
        from sklearn.metrics import accuracy_score, roc_auc_score
    except ImportError:
        return None

    from src.bot.runner import fetch_recent_history

    predictor = LGBMPredictor()

    # 학습 데이터 수집
    all_features = []
    all_targets = []
    for sym in symbols[:3]:
        try:
            hist = fetch_recent_history(client, sym, days=days)
            if len(hist) < 60:
                continue
            features = _build_features(hist)
            target = _build_target(hist, horizon=1)
            valid = features.notna().all(axis=1) & target.notna()
            all_features.append(features[valid])
            all_targets.append(target[valid])
        except Exception:
            continue

    if not all_features:
        return None

    X = pd.concat(all_features)
    y = pd.concat(all_targets)

    if len(X) < 60:
        return None

    predictor.feature_names = list(X.columns)

    # ── 시계열 분할 (2026-08-03 누수 수정) ─────────────────────────────────
    # 옛 코드는 종목별 데이터를 세로로 이어붙인 뒤 행 인덱스 80%로 잘랐다. 그러면
    # 테스트셋은 사실상 '마지막 종목'이고, 학습셋에 **같은 날짜의 다른 종목**이 들어간다.
    # 069500과 114800은 서로 역방향 ETF라 이건 라벨을 그대로 알려주는 누수다.
    # → 날짜 기준으로 자르고, 경계에 embargo(공백일)를 둬 인접일 상관까지 끊는다.
    dates = pd.to_datetime(pd.Series(X.index, index=X.index), errors="coerce")
    if dates.isna().all():
        return None
    uniq = sorted(dates.dropna().unique())
    if len(uniq) < 20:
        return None
    cut_tr = uniq[int(len(uniq) * 0.65)]      # 학습 65%
    cut_va = uniq[int(len(uniq) * 0.80)]      # 검증 15% (early stopping 전용)
    embargo = pd.Timedelta(days=EMBARGO_DAYS)  # 경계 공백 — 인접일 정보 누수 차단

    tr_mask = dates <= cut_tr
    va_mask = (dates > cut_tr + embargo) & (dates <= cut_va)
    te_mask = dates > cut_va + embargo        # 테스트 20% — 보고 전용, 학습·조기종료에 미사용

    X_train, y_train = X[tr_mask], y[tr_mask]
    X_valid, y_valid = X[va_mask], y[va_mask]
    X_test, y_test = X[te_mask], y[te_mask]
    if len(X_train) < 40 or len(X_valid) < 10 or len(X_test) < 10:
        return None
    if y_test.nunique() < 2:                  # 한쪽 라벨뿐이면 AUC 계산 불가
        return None

    train_data = lgb.Dataset(X_train, label=y_train)
    # early stopping은 **검증셋**으로. 옛 코드는 테스트셋으로 조기종료한 뒤 같은 셋에
    # 정확도를 보고해 낙관 편향이 들어갔다(보고값 86%, AUC 0.95).
    valid_data = lgb.Dataset(X_valid, label=y_valid, reference=train_data)

    params = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "num_leaves": 31,
        "learning_rate": 0.03,  # 일일 학습은 더 낮은 LR
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_child_samples": 10,
        "verbose": -1,
        "seed": 42,
    }

    callbacks = [lgb.early_stopping(10), lgb.log_evaluation(0)]

    # warm-start 제거(2026-08-03). 120일 롤링 윈도는 매일 99% 겹치므로, 어제 학습에 쓴
    # 행이 오늘 테스트셋이 된다. warm-start로 그 모델을 이어받으면 '이미 본 데이터'를
    # 평가하는 셈이라 지표가 부풀려진다. 표본이 작아(수백 행) 매일 새로 학습해도 저렴하다.
    predictor.model = lgb.train(
        params,
        train_data,
        num_boost_round=200,
        valid_sets=[valid_data],
        callbacks=callbacks,
    )

    # 보고 지표는 **테스트셋 전용**(학습·조기종료에 한 번도 안 쓰인 구간)
    y_pred_prob = predictor.model.predict(X_test)
    y_pred = (y_pred_prob > 0.5).astype(int)
    accuracy = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_pred_prob)

    predictor._save_model()

    # 피처 중요도 저장
    importance = dict(zip(
        predictor.feature_names,
        predictor.model.feature_importance(importance_type="gain").tolist(),
    ))
    sorted_imp = sorted(importance.items(), key=lambda x: x[1], reverse=True)
    FEATURE_IMPORTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FEATURE_IMPORTANCE_PATH.open("w", encoding="utf-8") as f:
        json.dump({
            "trained_at": pd.Timestamp.now().isoformat(),
            "mode": "daily_timesplit_embargo",   # 구 daily_warm_start(누수) 대체
            "accuracy": round(accuracy, 4),
            "auc": round(auc, 4),
            "n_samples": len(X),
            "n_train": len(X_train),
            "n_valid": len(X_valid),
            "n_test": len(X_test),
            "embargo_days": EMBARGO_DAYS,
            # 주의: 이 값은 홀드아웃 지표일 뿐 실거래 성적이 아니다. 실제 예측력은
            # live_accuracy.json(매일 예측 -> 다음날 실현 대조)으로만 판단한다.
            "feature_importance": {k: round(v, 2) for k, v in sorted_imp[:20]},
        }, f, ensure_ascii=False, indent=2)

    result = {"accuracy": float(accuracy), "auc": float(auc), "n_samples": len(X)}
    print(f"  [LGBM 일일학습] accuracy={accuracy:.1%}, AUC={auc:.3f}, 데이터={len(X)}건")
    return result


def get_prediction_filter(client, symbol: str, history=None) -> dict:
    """매수 판단 시 LGBM 필터를 적용.

    Args:
        client: KISClient
        symbol: 종목코드
        history: 이미 조회한 OHLCV DataFrame. None이면 내부에서 조회.

    Returns:
        {"allow": bool, "up_prob": float, "reason": str}
    """
    predictor = LGBMPredictor()
    if predictor.model is None:
        return {"allow": True, "up_prob": 0.5, "reason": "LGBM 모델 없음 — 필터 미적용"}

    # 실측 적중률이 동전던지기 수준이면 예측을 매매에 반영하지 않는다(2026-08-03).
    # 홀드아웃 지표는 부풀려질 수 있으므로, 전방검증(live_accuracy)만 신뢰한다.
    live = live_hit_rate()
    if live is not None and live < LIVE_MIN_HIT_RATE:
        return {"allow": True, "up_prob": 0.5,
                "reason": f"LGBM 실측 적중률 {live:.0%} < {LIVE_MIN_HIT_RATE:.0%} — 예측 미반영"}

    try:
        if history is None:
            from src.bot.runner import fetch_recent_history
            history = fetch_recent_history(client, symbol, days=70)
        result = predictor.predict(history)
        try:
            record_live_prediction(symbol, result.up_prob, history)
        except Exception:  # noqa: BLE001 — 기록 실패가 매매를 막으면 안 됨
            pass
        return {
            "allow": result.signal != "BLOCK",
            "up_prob": result.up_prob,
            "reason": result.detail,
        }
    except Exception as e:
        return {"allow": True, "up_prob": 0.5, "reason": f"LGBM 예측 실패: {e}"}


def record_live_prediction(symbol: str, up_prob: float, history) -> None:
    """오늘의 예측을 적어두고, 지난 예측들을 실현치로 채점한다(전방검증).

    홀드아웃 지표(accuracy/auc)는 분할·조기종료 설계에 따라 부풀려질 수 있다.
    반면 '예측 시점 이후 실제로 올랐나'는 조작이 불가능하다. 이 기록이 모델을
    믿을지 말지의 유일한 근거다. 하루 한 종목당 1건만 남긴다(중복 방지).
    """
    if history is None or len(history) < 2:
        return
    closes = history["close"] if "close" in history else history.iloc[:, -1]
    today = str(pd.to_datetime(history.index[-1]).date())
    last_close = float(closes.iloc[-1])

    data = {"records": []}
    if LIVE_ACCURACY_PATH.exists():
        try:
            data = json.loads(LIVE_ACCURACY_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {"records": []}
    recs = data.get("records", [])

    # 1) 미채점 예측 채점: 예측일보다 뒤의 종가가 확보됐으면 상승 여부 확정
    idx_by_date = {str(pd.to_datetime(d).date()): i for i, d in enumerate(history.index)}
    for r in recs:
        if r.get("realized") is not None or r.get("symbol") != symbol:
            continue
        i = idx_by_date.get(r.get("date", ""))
        if i is None or i + 1 >= len(closes):
            continue
        nxt = float(closes.iloc[i + 1])
        r["next_close"] = nxt
        r["realized"] = 1 if nxt > r["close"] else 0
        r["hit"] = int((r["up_prob"] > 0.5) == (r["realized"] == 1))

    # 2) 오늘 예측 기록(같은 날 중복 방지)
    if not any(r.get("date") == today and r.get("symbol") == symbol for r in recs):
        recs.append({"date": today, "symbol": symbol, "up_prob": round(float(up_prob), 4),
                     "close": last_close, "realized": None})

    graded = [r for r in recs if r.get("hit") is not None]
    hits = sum(r["hit"] for r in graded)
    confident = [r for r in graded if abs(r["up_prob"] - 0.5) >= 0.10]
    conf_hits = sum(r["hit"] for r in confident)
    data["records"] = recs[-500:]
    data["summary"] = {
        "graded": len(graded),
        "hit_rate": round(hits / len(graded), 4) if graded else None,
        "confident_graded": len(confident),
        "confident_hit_rate": round(conf_hits / len(confident), 4) if confident else None,
        "note": "이 값이 0.5 근처면 예측력 없음 — 사이즈·게이트에 반영할 것",
    }
    LIVE_ACCURACY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIVE_ACCURACY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
