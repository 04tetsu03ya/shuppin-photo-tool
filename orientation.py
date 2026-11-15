"""
服の向きを、切り抜いた形（シルエット）から判定する。

  ・パンツ：脚が2本に分かれている側を下、ウエスト側を上
  ・トップス／ワンピース：裾（まっすぐな辺）を下、首まわり（くぼみ・襟）を上
  ・スカート：ウエスト（細くてまっすぐな辺）を上、広がった裾を下

判定できないものや、自信がないものは回転しない（rotation=0, confident=False）。
"""

from __future__ import annotations

import numpy as np
from PIL import Image

KIND_LABEL = {"pants": "パンツ", "top": "トップス", "skirt": "スカート", None: "?"}


def _crop(m: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(m)
    if len(xs) < 50:
        return None
    return m[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]


def _resize(m: np.ndarray, max_side: int) -> np.ndarray:
    h, w = m.shape
    s = max_side / max(h, w)
    img = Image.fromarray((m * 255).astype(np.uint8)).resize(
        (max(8, round(w * s)), max(8, round(h * s))), Image.BILINEAR)
    return np.asarray(img) > 127


def _deskew(m: np.ndarray) -> np.ndarray:
    """少し斜めに置かれた服をまっすぐにする（外接する四角形が最小になる角度を探す）"""
    img = Image.fromarray((m * 255).astype(np.uint8))
    best, best_area = m, None
    for ang in np.arange(-15, 15.5, 1.5):
        r = np.asarray(img.rotate(float(ang), resample=Image.BILINEAR, expand=True)) > 127
        c = _crop(r)
        if c is None:
            continue
        area = c.shape[0] * c.shape[1]
        if best_area is None or area < best_area - 1e-9:
            best, best_area = c, area
    return best


def _prep(mask: np.ndarray, max_side: int = 200) -> np.ndarray | None:
    """二値マスクを商品の範囲で切り出し、傾きを直して、判定用に小さくする"""
    m = mask > 127 if mask.dtype != bool else mask
    m = _crop(m)
    if m is None:
        return None
    m = _resize(m, max_side)
    m = _deskew(m)
    return _resize(m, max_side)


def _runs(row: np.ndarray, min_gap: int):
    """1行のマスクで、つながった区間（小さなすき間は無視）を返す"""
    idx = np.flatnonzero(row)
    if len(idx) == 0:
        return []
    runs, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > min_gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def _profiles(m: np.ndarray):
    """各列の上端・下端までの距離（高さに対する割合）"""
    h, w = m.shape
    has = m.any(axis=0)
    top = np.where(has, m.argmax(axis=0), h) / h
    bot = np.where(has, m[::-1].argmax(axis=0), h) / h
    return top, bot, has


def _flatness(prof: np.ndarray, has: np.ndarray, lo: float, hi: float, tol: float = 0.02) -> float:
    """指定範囲の輪郭がどれだけ「まっすぐ」か（傾いていても直線ならOK）"""
    w = len(prof)
    xs = np.arange(int(w * lo), max(int(w * lo) + 1, int(w * hi)))
    ok = has[xs]
    if ok.sum() < 5:
        return 0.0
    x, y = xs[ok] / w, prof[xs][ok]
    a, b = np.polyfit(x, y, 1)
    if abs(a) > 0.35:  # 急な斜め（肩のライン等）は直線の辺とみなさない
        return 0.0
    return float((np.abs(y - (a * x + b)) < tol).mean())


def _width_rows(m: np.ndarray):
    """各行の横幅（端から端まで, 幅に対する割合）"""
    h, w = m.shape
    has = m.any(axis=1)
    left = m.argmax(axis=1)
    right = w - 1 - m[:, ::-1].argmax(axis=1)
    return np.where(has, (right - left + 1) / w, 0.0)


def _edge_span(m: np.ndarray, depth: float):
    """上端から depth の深さの行で、商品がかかっている列の範囲"""
    h, w = m.shape
    row = m[min(h - 1, max(0, int(h * depth)))]
    xs = np.flatnonzero(row)
    if len(xs) == 0:
        return None
    return xs.min(), xs.max()


def edge_flat(m: np.ndarray, tol: float = 0.02) -> float:
    """上の辺がまっすぐな一直線か（ウエスト・裾など）。辺の幅が狭いものは対象外"""
    h, w = m.shape
    span = _edge_span(m, 0.06)
    if span is None or (span[1] - span[0]) < 0.3 * w:
        return 0.0
    a0, a1 = span
    lo, hi = a0 + 0.1 * (a1 - a0), a1 - 0.1 * (a1 - a0)
    top, _, has = _profiles(m)
    xs = np.arange(int(lo), int(hi) + 1)
    ok = has[xs]
    if ok.sum() < 5:
        return 0.0
    x, y = xs[ok] / w, top[xs][ok]
    a, b = np.polyfit(x, y, 1)
    if abs(a) > 0.35:
        return 0.0
    return float((np.abs(y - (a * x + b)) < tol).mean())


def shoulders(m: np.ndarray) -> float:
    """上の辺が「中央が高く、外側に向かって下がる」肩のラインか"""
    h, w = m.shape
    span = _edge_span(m, 0.15)
    if span is None or (span[1] - span[0]) < 0.4 * w:
        return 0.0
    a0, a1 = span
    top, _, has = _profiles(m)
    L = np.arange(int(a0 + 0.08 * (a1 - a0)), int(a0 + 0.38 * (a1 - a0)))
    R = np.arange(int(a0 + 0.62 * (a1 - a0)), int(a0 + 0.92 * (a1 - a0)))
    if has[L].sum() < 4 or has[R].sum() < 4:
        return 0.0
    aL = np.polyfit(L[has[L]] / w, top[L][has[L]], 1)[0]
    aR = np.polyfit(R[has[R]] / w, top[R][has[R]], 1)[0]
    if not (aL < -0.1 and aR > 0.1):
        return 0.0
    return float(np.clip(min(-aL, aR) / 0.4, 0, 1))


def pants_score(m: np.ndarray) -> float:
    """下側が2本の脚に分かれ、上側（ウエスト）が1つにつながっているか"""
    h, w = m.shape
    if h < 0.7 * w:
        return 0.0
    gap = max(2, int(w * 0.03))
    min_leg = w * 0.15

    def legs_split(y):
        legs = [(a, b) for a, b in _runs(m[y], gap) if b - a + 1 >= min_leg]
        if len(legs) < 2:
            return False
        legs.sort(key=lambda r: r[1] - r[0], reverse=True)
        (a1, b1), (a2, b2) = sorted(legs[:2])
        # 脚と脚のすき間は狭い（タンクトップの襟ぐりのような大きな開きは除外）
        return (a2 - b1) < 0.4 * w

    bottom = [legs_split(y) for y in range(int(h * 0.72), int(h * 0.95))]
    top = [len(_runs(m[y], gap)) == 1 and m[y].sum() >= 0.55 * w for y in range(int(h * 0.03), int(h * 0.2))]
    if not bottom or not top:
        return 0.0
    return float(np.mean(bottom) * np.mean(top))


def top_score(m: np.ndarray) -> float:
    h, w = m.shape
    flip = m[::-1]
    hem_flat = edge_flat(flip)
    top_flat = edge_flat(m)
    top, _, has = _profiles(m)
    c = np.median(top[int(w * 0.45): int(w * 0.55) + 1])
    sl = np.median(top[int(w * 0.27): int(w * 0.34) + 1])
    sr = np.median(top[int(w * 0.66): int(w * 0.73) + 1])
    notch = c - (sl + sr) / 2
    notch_s = float(np.clip((notch - 0.015) / 0.05, 0, 1)) if (notch < 0.5 and abs(sl - sr) < 0.06) else 0.0
    neck = max(notch_s, shoulders(m))
    # 袖：いちばん幅が広い行が上半分にある
    wr = _width_rows(m)
    upper, lower = wr[: h // 2].max(initial=0), wr[h // 2:].max(initial=0)
    sleeve = float(np.clip((upper - lower) / 0.15, -1, 1))
    return hem_flat - top_flat + 0.7 * neck + 0.4 * sleeve


def skirt_score(m: np.ndarray) -> float:
    h, w = m.shape
    wr = _width_rows(m)
    bottom_w = wr[int(h * 0.85): int(h * 0.97)].mean()
    top_w = wr[int(h * 0.03): int(h * 0.15)].mean()
    taper = bottom_w - top_w
    # スカートは裾がいちばん広い（途中に袖のような出っぱりがない）
    widest_at_hem = float(np.clip((bottom_w / max(wr.max(), 1e-6) - 0.75) / 0.15, 0, 1))
    return float(np.clip(taper / 0.15, 0, 1) * edge_flat(m) * widest_at_hem)


def detect(mask: np.ndarray) -> dict:
    """
    mask: 商品=255 の2値マスク（元写真の縦横比のまま）
    戻り値: rotation（時計回りに何度回すと正しい向きになるか）, kind, confident
    """
    m0 = _prep(mask)
    if m0 is None:
        return {"rotation": 0, "kind": None, "confident": False}
    frames = {r: np.rot90(m0, k=-r // 90) for r in (0, 90, 180, 270)}

    # 1) パンツ（脚の分かれ目がはっきりしているので最優先）
    ps = {r: pants_score(f) for r, f in frames.items()}
    r_best = max(ps, key=ps.get)
    if ps[r_best] >= 0.5:
        return {"rotation": r_best, "kind": "pants", "confident": True}

    # 2) トップス／スカート
    scores = {}
    for r, f in frames.items():
        t, s = top_score(f), skirt_score(f)
        prior = 0.15 if r == 0 else 0.0  # 迷ったら元の向きを優先
        scores[r] = (max(t, s) + prior, "top" if t >= s else "skirt")
    order = sorted(scores, key=lambda r: scores[r][0], reverse=True)
    best, second = scores[order[0]][0], scores[order[1]][0]
    if best >= 0.6 and best - second >= 0.25:
        return {"rotation": order[0], "kind": scores[order[0]][1], "confident": True}
    return {"rotation": 0, "kind": None, "confident": False}
