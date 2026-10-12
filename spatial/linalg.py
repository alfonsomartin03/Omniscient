"""Tiny dependency-free linear algebra and least squares for calibration (n <= ~10)."""
from __future__ import annotations

import math
from typing import Callable, Sequence

Matrix = list[list[float]]


def solve(a: Matrix, b: Sequence[float]) -> list[float]:
    n = len(a)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("singular matrix")
        m[col], m[pivot] = m[pivot], m[col]
        for row in range(col + 1, n):
            f = m[row][col] / m[col][col]
            for k in range(col, n + 1):
                m[row][k] -= f * m[col][k]
    x = [0.0] * n
    for row in range(n - 1, -1, -1):
        x[row] = (m[row][n] - sum(m[row][k] * x[k] for k in range(row + 1, n))) / m[row][row]
    return x


def invert(a: Matrix) -> Matrix:
    n = len(a)
    columns = [solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[columns[j][i] for j in range(n)] for i in range(n)]


def jacobi_eigen(sym: Matrix, sweeps: int = 60) -> tuple[list[float], Matrix]:
    """Eigenvalues and eigenvectors (as columns of the returned matrix) of a symmetric matrix."""
    n = len(sym)
    a = [row[:] for row in sym]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(sweeps):
        off = sum(a[i][j] ** 2 for i in range(n) for j in range(i + 1, n))
        if off < 1e-22:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-30:
                    continue
                theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
                t = (1 if theta >= 0 else -1) / (abs(theta) + math.sqrt(theta * theta + 1))
                c = 1 / math.sqrt(t * t + 1)
                s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p], v[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    return [a[i][i] for i in range(n)], v


def numeric_jacobian(fn: Callable[[Sequence[float]], list[float]], x: Sequence[float], h: float = 1e-6) -> Matrix:
    cols = []
    for i in range(len(x)):
        up, dn = list(x), list(x)
        up[i] += h
        dn[i] -= h
        fu, fd = fn(up), fn(dn)
        cols.append([(a - b) / (2 * h) for a, b in zip(fu, fd)])
    return [[cols[j][i] for j in range(len(x))] for i in range(len(cols[0]))]


def levenberg_marquardt(fn: Callable[[Sequence[float]], list[float]], x0: Sequence[float], iterations: int = 80):
    """Minimise sum(fn(x)^2). Returns (x, jacobian_at_solution, residuals)."""
    x = list(x0)
    r = fn(x)
    cost = sum(v * v for v in r)
    lam = 1e-3
    for _ in range(iterations):
        j = numeric_jacobian(fn, x)
        n = len(x)
        jtj = [[sum(j[k][a] * j[k][b] for k in range(len(r))) for b in range(n)] for a in range(n)]
        jtr = [sum(j[k][a] * r[k] for k in range(len(r))) for a in range(n)]
        improved = False
        for _try in range(10):
            damped = [[jtj[a][b] + (lam * (jtj[a][a] + 1e-9) if a == b else 0.0) for b in range(n)] for a in range(n)]
            try:
                step = solve(damped, [-v for v in jtr])
            except ValueError:
                lam *= 10
                continue
            trial = [xi + si for xi, si in zip(x, step)]
            rt = fn(trial)
            ct = sum(v * v for v in rt)
            if ct < cost:
                converged = cost - ct < 1e-12 * max(1.0, cost) and max(abs(s) for s in step) < 1e-9
                x, r, cost, lam, improved = trial, rt, ct, max(lam / 5, 1e-9), True
                if converged:
                    return x, numeric_jacobian(fn, x), r
                break
            lam *= 10
        if not improved:
            break
    return x, numeric_jacobian(fn, x), r


def covariance(jacobian: Matrix, residuals: Sequence[float], dof_floor: int = 1) -> tuple[Matrix, float]:
    """(J^T J)^-1 scaled by max(1, chi2/dof) so a poor fit reports inflated uncertainty. Raises ValueError if degenerate."""
    n = len(jacobian[0])
    jtj = [[sum(row[a] * row[b] for row in jacobian) for b in range(n)] for a in range(n)]
    inv = invert(jtj)
    dof = max(dof_floor, len(residuals) - n)
    scale = max(1.0, sum(v * v for v in residuals) / dof)
    return [[v * scale for v in row] for row in inv], scale
