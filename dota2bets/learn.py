"""Логистическая регрессия на чистом Python (Ньютон / IRLS с L2) и метрики."""

import math

EPS = 1e-9


def sigmoid(z):
    if z < -30:
        return 1e-13
    if z > 30:
        return 1 - 1e-13
    return 1.0 / (1.0 + math.exp(-z))


def logit(p):
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def solve(a, b):
    """Решение системы a·x = b (Гаусс с выбором ведущего элемента). a — список списков, копируется."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            m[c][c] += 1e-6
        m[c], m[piv] = m[piv], m[c]
        inv = 1.0 / m[c][c]
        for r in range(n):
            if r != c and m[r][c] != 0.0:
                f = m[r][c] * inv
                rc = m[c]
                rr = m[r]
                for k in range(c, n + 1):
                    rr[k] -= f * rc[k]
    return [m[i][n] / m[i][i] for i in range(n)]


class Standardizer:
    def __init__(self, xs):
        d = len(xs[0])
        n = float(len(xs))
        self.mean = [sum(x[j] for x in xs) / n for j in range(d)]
        self.std = []
        for j in range(d):
            var = sum((x[j] - self.mean[j]) ** 2 for x in xs) / n
            self.std.append(math.sqrt(var) if var > 1e-12 else 1.0)

    def apply(self, x):
        return [(x[j] - self.mean[j]) / self.std[j] for j in range(len(x))]


class LogReg:
    """w[0] — свободный член. Признаки стандартизируются внутри."""

    def __init__(self, names, l2=1.0, iters=8):
        self.names = names
        self.l2 = l2
        self.iters = iters
        self.std = None
        self.w = None

    def fit(self, xs, ys):
        self.std = Standardizer(xs)
        X = [[1.0] + self.std.apply(x) for x in xs]
        d = len(X[0])
        w = [0.0] * d
        for _ in range(self.iters):
            g = [0.0] * d
            h = [[0.0] * d for _ in range(d)]
            for x, y in zip(X, ys):
                z = sum(wi * xi for wi, xi in zip(w, x))
                p = sigmoid(z)
                r = p - y
                s = p * (1 - p)
                for j in range(d):
                    xj = x[j]
                    g[j] += r * xj
                    sx = s * xj
                    hj = h[j]
                    for k in range(j, d):
                        hj[k] += sx * x[k]
            for j in range(1, d):
                g[j] += self.l2 * w[j]
                h[j][j] += self.l2
            for j in range(d):
                for k in range(j):
                    h[j][k] = h[k][j]
            step = solve(h, g)
            w = [wi - si for wi, si in zip(w, step)]
            if max(abs(s) for s in step) < 1e-6:
                break
        self.w = w
        return self

    def predict(self, x):
        z = self.w[0] + sum(wi * xi for wi, xi in zip(self.w[1:], self.std.apply(x)))
        return sigmoid(z)

    def contributions(self, x):
        """Вклад каждого признака в логит (после стандартизации)."""
        xs = self.std.apply(x)
        return {"intercept": self.w[0], **{n: wi * xi for n, wi, xi in zip(self.names, self.w[1:], xs)}}

    def to_dict(self):
        return {"names": self.names, "l2": self.l2, "w": self.w, "mean": self.std.mean, "std": self.std.std}

    @classmethod
    def from_dict(cls, d):
        m = cls(d["names"], d["l2"])
        m.w = d["w"]
        m.std = Standardizer.__new__(Standardizer)
        m.std.mean, m.std.std = d["mean"], d["std"]
        return m


# ---------------------------------------------------------------- метрики

def metrics(ps, ys):
    n = len(ys)
    if not n:
        return {"n": 0}
    ll = -sum(y * math.log(max(p, EPS)) + (1 - y) * math.log(max(1 - p, EPS)) for p, y in zip(ps, ys)) / n
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    acc = sum(1 for p, y in zip(ps, ys) if (p >= 0.5) == (y == 1)) / n
    return {"n": n, "logloss": round(ll, 4), "brier": round(brier, 4), "acc": round(acc, 4)}


def calibration(ps, ys, bins=10):
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, y) for p, y in zip(ps, ys) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if sel:
            out.append({"bin": "%.1f-%.1f" % (lo, hi), "n": len(sel),
                        "p_mean": round(sum(p for p, _ in sel) / len(sel), 3),
                        "y_mean": round(sum(y for _, y in sel) / len(sel), 3)})
    return out


# ---------------------------------------------------------------- линейная регрессия (тоталы)

class LinReg:
    """МНК с гребневой поправкой. w[0] — свободный член; sigma — std остатков на обучении."""

    def __init__(self, names, l2=1.0):
        self.names = names
        self.l2 = l2
        self.w = None
        self.sigma = None
        self.resid_q = None     # 201 квантиль остатков обучения: распределение скошено, нормальное не годится

    def fit(self, xs, ys):
        d = len(self.names) + 1
        a = [[0.0] * d for _ in range(d)]
        b = [0.0] * d
        for x, y in zip(xs, ys):
            row = [1.0] + list(x)
            for j in range(d):
                rj = row[j]
                b[j] += rj * y
                aj = a[j]
                for k in range(j, d):
                    aj[k] += rj * row[k]
        for j in range(d):
            for k in range(j):
                a[j][k] = a[k][j]
            if j:
                a[j][j] += self.l2
        self.w = solve(a, b)
        res = [y - self.predict(x) for x, y in zip(xs, ys)]
        self.sigma = math.sqrt(sum(r * r for r in res) / max(len(res), 1))
        res.sort()
        n = len(res)
        self.resid_q = [res[min(n - 1, int(i * (n - 1) / 200.0))] for i in range(201)] if n else None
        return self

    def prob_over(self, mu, line):
        """P(значение > line) по эмпирическому распределению остатков."""
        q = self.resid_q
        if not q:
            return p_over(mu, self.sigma, line)
        d = line - mu
        if d <= q[0]:
            return 0.995
        if d >= q[-1]:
            return 0.005
        lo, hi = 0, 200
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if q[mid] <= d:
                lo = mid
            else:
                hi = mid
        span = q[hi] - q[lo]
        rank = lo + ((d - q[lo]) / span if span > 0 else 0.5)
        return min(0.995, max(0.005, 1.0 - rank / 200.0))

    def predict(self, x):
        return self.w[0] + sum(wi * xi for wi, xi in zip(self.w[1:], x))

    def to_dict(self):
        return {"names": self.names, "l2": self.l2, "w": self.w, "sigma": self.sigma,
                "resid_q": [round(x, 2) for x in self.resid_q] if self.resid_q else None}

    @classmethod
    def from_dict(cls, d):
        m = cls(d["names"], d.get("l2", 1.0))
        m.w, m.sigma = d["w"], d["sigma"]
        m.resid_q = d.get("resid_q")
        return m


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def p_over(mu, sigma, line):
    """P(значение > line) при нормальном приближении."""
    if not sigma:
        return 1.0 if mu > line else 0.0
    return 1.0 - norm_cdf((line - mu) / sigma)
