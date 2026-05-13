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
    features["body_ratio"] = (close - df["open"].astype(float)) / (high - low).replace(0, np.nan)
    features["upper_shadow"] = (high - close.clip(lower=df["open"].astype(float))) / (high - low).replace(0, np.nan)

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

        # 시계열 분할 (마지막 test_ratio를 테스트로)
        split_idx = int(len(features) * (1 - test_ratio))
        X_train = features.iloc[:split_idx]
        X_test = features.iloc[split_idx:]
        y_train = target.iloc[:split_idx]
        y_test = target.iloc[split_idx:]

        # LightGBM 학습
        train_data = lgb.Dataset(X_train, label=y_train)
        valid_data = lgb.Dataset(X_test, label=y_test, reference=train_data)

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
                "n_samples": len(features),
                "feature_importance": {k: round(v, 2) for k, v in sorted_imp[:20]},
            }, f, ensure_ascii=False, indent=2)

        print(f"  [LGBM] 학습 완료: accuracy={accuracy:.1%}, AUC={auc:.3f}")
        print(f"  [LGBM] 학습 데이터: {len(X_train)}건, 테스트: {len(X_test)}건")
        print(f"  [LGBM] 상위 피처: " +
              ", ".join(f"{k}({v:.0f})" for k, v in sorted_imp[:5]))

        return {
            "accuracy": float(accuracy),
            "auc": float(auc),
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


def get_prediction_filter(client, symbol: str) -> dict:
    """매수 판단 시 LGBM 필터를 적용.

    Returns:
        {"allow": bool, "up_prob": float, "reason": str}
    """
    from src.bot.runner import fetch_recent_history

    predictor = LGBMPredictor()
    if predictor.model is None:
        return {"allow": True, "up_prob": 0.5, "reason": "LGBM 모델 없음 — 필터 미적용"}

    try:
        history = fetch_recent_history(client, symbol, days=70)
        result = predictor.predict(history)
        return {
            "allow": result.signal != "BLOCK",
            "up_prob": result.up_prob,
            "reason": result.detail,
        }
    except Exception as e:
        return {"allow": True, "up_prob": 0.5, "reason": f"LGBM 예측 실패: {e}"}
