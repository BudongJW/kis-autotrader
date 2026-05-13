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

        # CV로 성능 측정
        for train_idx, test_idx in tscv.split(features):
            X_tr = features.iloc[train_idx]
            X_te = features.iloc[test_idx]
            y_tr = target.iloc[train_idx]
            y_te = target.iloc[test_idx]

            tr_data = lgb.Dataset(X_tr, label=y_tr)
            va_data = lgb.Dataset(X_te, label=y_te, reference=tr_data)
            fold_model = lgb.train(
                params, tr_data, num_boost_round=200,
                valid_sets=[va_data],
                callbacks=[lgb.early_stopping(15), lgb.log_evaluation(0)],
            )
            fold_prob = fold_model.predict(X_te)
            try:
                cv_aucs.append(roc_auc_score(y_te, fold_prob))
            except ValueError:
                pass

        # 최종 모델: 마지막 20%를 validation으로 전체 학습
        split_idx = int(len(features) * (1 - test_ratio))
        X_train = features.iloc[:split_idx]
        X_test = features.iloc[split_idx:]
        y_train = target.iloc[:split_idx]
        y_test = target.iloc[split_idx:]

        train_data = lgb.Dataset(X_train, label=y_train)
        valid_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

        callbacks = [lgb.early_stopping(20), lgb.log_evaluation(0)]
        self.model = lgb.train(
            params,
            train_data,
            num_boost_round=300,
            valid_sets=[valid_data],
            callbacks=callbacks,
        )

        # 평가
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
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    train_data = lgb.Dataset(X_train, label=y_train)
    valid_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

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

    # warm-start: 기존 모델이 있으면 init_model로 사용
    init_model = predictor.model if predictor.model is not None else None
    predictor.model = lgb.train(
        params,
        train_data,
        num_boost_round=50,  # 추가 50라운드만 (빠른 갱신)
        valid_sets=[valid_data],
        callbacks=callbacks,
        init_model=init_model,
    )

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
            "mode": "daily_warm_start",
            "accuracy": round(accuracy, 4),
            "auc": round(auc, 4),
            "n_samples": len(X),
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

    try:
        if history is None:
            from src.bot.runner import fetch_recent_history
            history = fetch_recent_history(client, symbol, days=70)
        result = predictor.predict(history)
        return {
            "allow": result.signal != "BLOCK",
            "up_prob": result.up_prob,
            "reason": result.detail,
        }
    except Exception as e:
        return {"allow": True, "up_prob": 0.5, "reason": f"LGBM 예측 실패: {e}"}
