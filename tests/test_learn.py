import math
import random

from dota2bets.learn import LogReg, sigmoid, solve, metrics, calibration


def test_solve():
    x = solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
    assert abs(x[0] - 0.8) < 1e-9 and abs(x[1] - 1.4) < 1e-9


def test_logreg_recovers_coefficients():
    rnd = random.Random(1)
    true_w = [0.3, 1.2, -0.8]
    xs, ys = [], []
    for _ in range(6000):
        x = [rnd.gauss(0, 1), rnd.gauss(0, 1)]
        z = true_w[0] + true_w[1] * x[0] + true_w[2] * x[1]
        ys.append(1 if rnd.random() < sigmoid(z) else 0)
        xs.append(x)
    m = LogReg(["a", "b"], l2=0.01).fit(xs, ys)
    # признаки стандартизированы со std≈1, поэтому веса сравнимы напрямую
    assert abs(m.w[1] - 1.2) < 0.15
    assert abs(m.w[2] + 0.8) < 0.15
    assert abs(m.w[0] - 0.3) < 0.15
    p = m.predict([1.0, 0.0])
    assert 0.7 < p < 0.9
    d = m.to_dict()
    m2 = LogReg.from_dict(d)
    assert abs(m2.predict([1.0, 0.0]) - p) < 1e-12
    c = m2.contributions([1.0, 0.0])
    assert set(c) == {"intercept", "a", "b"}


def test_metrics_and_calibration():
    ps = [0.9, 0.2, 0.6, 0.4]
    ys = [1, 0, 0, 1]
    m = metrics(ps, ys)
    assert m["n"] == 4 and m["acc"] == 0.5
    assert abs(m["logloss"] - (-(math.log(0.9) + math.log(0.8) + math.log(0.4) + math.log(0.4)) / 4)) < 1e-3
    cal = calibration(ps, ys, bins=2)
    assert sum(c["n"] for c in cal) == 4
