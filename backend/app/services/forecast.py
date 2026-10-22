"""Forecasting: linear trend + weekly seasonality, backtested vs naive baseline (MAPE)."""
from datetime import timedelta
import math
from sqlalchemy.orm import Session
from .metrics import daily_series, _sum_orders


def _linreg(xs, ys):
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return b, a


def _predict_series(values, horizon):
    n = len(values)
    xs = list(range(n))
    b, a = _linreg(xs, values)
    trend = [a + b * x for x in xs]
    # weekly seasonality from detrended residuals
    resid = [v - t for v, t in zip(values, trend)]
    seas = [0.0] * 7
    cnt = [0] * 7
    for i, r in enumerate(resid):
        seas[i % 7] += r
        cnt[i % 7] += 1
    seas = [s / c if c else 0.0 for s, c in zip(seas, cnt)]
    out = []
    for h in range(1, horizon + 1):
        i = n + h - 1
        out.append(max(0.0, a + b * i + seas[i % 7]))
    return out, b


def _mape(actual, pred):
    pairs = [(a, p) for a, p in zip(actual, pred) if a > 0]
    if not pairs:
        return None
    return round(sum(abs(a - p) / a for a, p in pairs) / len(pairs) * 100, 2)


def _fitted_values(values):
    """In-sample fitted values (trend + weekly seasonality) for residual estimation."""
    n = len(values)
    xs = list(range(n))
    b, a = _linreg(xs, values)
    trend = [a + b * x for x in xs]
    resid = [v - t for v, t in zip(values, trend)]
    seas = [0.0] * 7
    cnt = [0] * 7
    for i, r in enumerate(resid):
        seas[i % 7] += r
        cnt[i % 7] += 1
    seas = [s / c if c else 0.0 for s, c in zip(seas, cnt)]
    return [trend[i] + seas[i % 7] for i in xs]


def forecast(db: Session, org_id: int, start, end, horizon: int = 30):
    series = daily_series(db, org_id, start, end)
    values = [p["revenue_cents"] for p in series]
    if len(values) < 21:
        return {"ok": False, "reason": "insufficient_data",
                "message": "Need at least 21 days of history for a reliable forecast.",
                "history_points": len(values)}
    # Backtest: train on all but last 7 days, predict those 7
    train = values[:-7]
    pred_test, _ = _predict_series(train, 7)
    mape = _mape(values[-7:], pred_test)
    naive = [train[-1]] * 7  # baseline
    baseline_mape = _mape(values[-7:], naive)

    preds, slope = _predict_series(values, horizon)
    # uncertainty interval grows with horizon
    fitted = _fitted_values(values)
    tail = min(14, len(values))
    resid_sd = math.sqrt(sum((v - f) ** 2 for v, f in zip(values[-tail:], fitted[-tail:])) / tail)
    last = end
    points = []
    for h, y in enumerate(preds, 1):
        d = last + timedelta(days=h)
        band = 1.28 * (resid_sd or y * 0.15) * math.sqrt(h / 7 + 1)
        points.append({"date": d.isoformat(), "yhat_cents": int(y),
                       "lower_cents": int(max(0, y - band)), "upper_cents": int(y + band)})
    return {"ok": True, "algorithm": "linear_trend_weekly_seasonality",
            "history": series[-60:], "forecast": points,
            "mape_pct": mape, "baseline_naive_mape_pct": baseline_mape,
            "beats_baseline": (mape is not None and baseline_mape is not None and mape <= baseline_mape),
            "trend_cents_per_day": round(slope, 2),
            "caveat": "Backtest on held-out last 7 days. Forecast assumes stable conditions."}
