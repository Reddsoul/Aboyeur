"""Per-dish portion solver — decides HOW MUCH of each chosen dish to eat so a
DAY hits its whole nutrient target {kcal, protein, carb, fat, fiber}, not just
its calorie count.

engines/optimizer.py chooses WHICH dishes go in a menu (overlap, pantry, diet,
cuisine, no desserts); this module chooses the serving multiplier for each so
the day's totals land on target across every macro. Ported from the Macro
Kit's closed-form bowl solve (engines/kit.py): a small bounded least-squares
problem solved with an active-set loop and a plain Gaussian eliminator — no
scipy, same as the kit path.

Why per-dish and not one uniform day factor: uniform scaling is what produced
"3 servings of rice" — it can only trade the whole day up or down on calories
and lets protein/carb/fat/fiber fall wherever the dish mix leaves them. Moving
each dish independently (inside a sane 0.75–2.0x band) lets the solve push
protein up and carbohydrate down at the same time.
"""

from __future__ import annotations

from aboyeur.engines.optimizer import PORTION_MULT_MAX, PORTION_MULT_MIN

# The five dimensions a day is graded on. kcal first — it anchors the solve;
# the macros are the shape.
NUTRIENT_KEYS = ("kcal", "protein_g", "carb_g", "fat_g", "fiber_g")

# Relative importance of missing each dimension. Same priority the kit solver
# and resolve_macro_targets use: under-delivering protein is the miss that
# matters, a carbohydrate overshoot is not; fiber sits in the middle. kcal is
# weighted like protein so the day still lands on energy.
SOLVE_WEIGHTS = {
    "kcal": 3.0, "protein_g": 3.0, "fat_g": 2.0, "carb_g": 1.0, "fiber_g": 1.5,
}

# Pull every multiplier gently toward 1.0 (one normal serving). Small on
# purpose: enough to keep the solve well-posed when a dimension is missing
# (kcal-only targets are underdetermined for >1 dish) and to break ties
# toward normal servings, but not enough to fight a real calorie gap — a day
# that needs bigger portions should get them (up to the clamp).
_RIDGE = 0.02


def _dish_vector(macros: dict) -> dict[str, float]:
    """Per-serving nutrient vector for one dish, keyed by NUTRIENT_KEYS.
    Accepts either 'kcal' or 'calories' for energy."""
    kcal = macros.get("kcal")
    if kcal is None:
        kcal = macros.get("calories", 0)
    return {
        "kcal": float(kcal or 0),
        "protein_g": float(macros.get("protein_g") or 0),
        "carb_g": float(macros.get("carb_g") or 0),
        "fat_g": float(macros.get("fat_g") or 0),
        "fiber_g": float(macros.get("fiber_g") or 0),
    }


def _active_dims(vectors: list[dict], target: dict) -> list[str]:
    """Dimensions worth solving against: the target names them and at least
    one dish actually carries them. A kcal-only target (no macros passed)
    reduces the solve to calorie scaling, matching the old behavior."""
    dims = []
    for k in NUTRIENT_KEYS:
        if float(target.get(k) or 0) <= 0:
            continue
        if any(v[k] > 0 for v in vectors):
            dims.append(k)
    return dims


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a small dense system.
    Returns None if singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        inv = 1.0 / m[col][col]
        for j in range(col, n + 1):
            m[col][j] *= inv
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def _least_squares_multipliers(
    vectors: list[dict], target: dict, dims: list[str],
    lo: float, hi: float,
) -> list[float]:
    """Bounded weighted least-squares for the per-dish multipliers.

    minimize  sum_dims w_d / T_d^2 * (sum_i m_i v_i[d] - T_d)^2
              + _RIDGE * sum_i (m_i - 1)^2
    subject to lo <= m_i <= hi

    Active-set: solve unconstrained, clamp the worst violators to their bound,
    fix them, re-solve the rest. n is tiny (<= ~6 dishes/day) so this is a
    handful of small Gaussian solves.
    """
    n = len(vectors)
    if n == 0:
        return []
    fixed: dict[int, float] = {}
    for _ in range(n + 1):
        free = [i for i in range(n) if i not in fixed]
        if not free:
            break
        # Normal equations over the free multipliers.
        size = len(free)
        a = [[0.0] * size for _ in range(size)]
        rhs = [0.0] * size
        for di, i in enumerate(free):
            for dj, j in enumerate(free):
                s = 0.0
                for d in dims:
                    t = float(target[d])
                    s += SOLVE_WEIGHTS[d] / (t * t) * vectors[i][d] * vectors[j][d]
                if i == j:
                    s += _RIDGE
                a[di][dj] = s
            r = _RIDGE  # ridge pulls toward m = 1
            for d in dims:
                t = float(target[d])
                fixed_contrib = sum(fixed[k] * vectors[k][d] for k in fixed)
                r += SOLVE_WEIGHTS[d] / (t * t) * vectors[i][d] * (t - fixed_contrib)
            rhs[di] = r
        sol = _solve_linear(a, rhs)
        if sol is None:
            for i in free:
                fixed[i] = 1.0
            break
        # Clamp violators, fix them, iterate.
        violated = False
        for di, i in enumerate(free):
            if sol[di] < lo - 1e-9:
                fixed[i] = lo
                violated = True
            elif sol[di] > hi + 1e-9:
                fixed[i] = hi
                violated = True
        if not violated:
            for di, i in enumerate(free):
                fixed[i] = min(hi, max(lo, sol[di]))
            break
    return [round(fixed.get(i, 1.0), 2) for i in range(n)]


def solve_day_multipliers(
    dish_macros: list[dict], target: dict,
    lo: float = PORTION_MULT_MIN, hi: float = PORTION_MULT_MAX,
) -> list[float]:
    """Serving multiplier for each dish so the combined day totals best fit
    `target` (a subset of {kcal|calories, protein_g, carb_g, fat_g, fiber_g}),
    each multiplier clamped to [lo, hi]. Dishes with no energy data get 1.0.
    """
    vectors = [_dish_vector(m) for m in dish_macros]
    solvable = [i for i, v in enumerate(vectors) if v["kcal"] > 0]
    if not solvable:
        return [1.0] * len(vectors)
    dims = _active_dims([vectors[i] for i in solvable], target)
    if not dims:
        return [1.0] * len(vectors)
    sub = _least_squares_multipliers(
        [vectors[i] for i in solvable], target, dims, lo, hi
    )
    out = [1.0] * len(vectors)
    for idx, mult in zip(solvable, sub):
        out[idx] = mult
    return out


def combined_totals(dish_macros: list[dict], multipliers: list[float]) -> dict:
    """Day totals after applying the multipliers, keyed by NUTRIENT_KEYS."""
    totals = {k: 0.0 for k in NUTRIENT_KEYS}
    for m, mult in zip(dish_macros, multipliers):
        v = _dish_vector(m)
        for k in NUTRIENT_KEYS:
            totals[k] += v[k] * mult
    return totals


def residual(totals: dict, target: dict) -> float:
    """Weighted relative miss across the dimensions the target names. Lower is
    better; 0 is a perfect fit. Mirrors kit._selection_error."""
    err = weight = 0.0
    for k in NUTRIENT_KEYS:
        t = float(target.get(k) or 0)
        if t <= 0:
            continue
        w = SOLVE_WEIGHTS[k]
        err += w * abs(float(totals.get(k) or 0) - t) / t
        weight += w
    return err / weight if weight else 0.0
