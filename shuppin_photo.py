#!/usr/bin/env python3
"""
出品写真 自動加工ツール（ヤフオク / メルカリ / ラクマ用）

フォルダ内の写真をまとめて処理します:
  1. 背景を切り抜いて真っ白にする（AIで商品だけを残す）
  2. 商品が中央に来るように余白をそろえる
  3. 正方形（デフォルト 1200x1200）の JPEG で保存する

使い方:
  python shuppin_photo.py 入力フォルダ 出力フォルダ

オプション例:
  --size 1080          出力サイズ（正方形の一辺, px）
  --margin 0.08        商品のまわりの余白（一辺に対する割合）
  --ratio 4:3          正方形以外にしたい場合（横:縦）
  --keep-bg            背景は消さずに切り出し・リサイズだけ
  --shadow             商品の下にうっすら影をつける
  --model isnet-general-use   切り抜きAIモデル（下記参照）
  --pair               表・裏の2枚を横に並べて1枚にまとめる（長いパンツ等）
  --gap 0.04           --pair 時の2枚の間隔（一辺に対する割合）
  --no-refine          股下などのすき間に残った背景を消す処理をオフにする

--pair の組み合わせ方:
  ・ファイル名の末尾が 表/裏, front/back, _f/_b のものは同じ名前同士でペアにします
      例) pants01_表.jpg + pants01_裏.jpg → pants01.jpg
  ・それ以外はファイル名順に2枚ずつペアにします（1枚目+2枚目, 3枚目+4枚目…）
  ・余った1枚は通常どおり1枚で処理します

初回のみ切り抜き用AIモデル（約180MB）を自動ダウンロードします。

必要なもの:
  pip install onnxruntime pillow numpy scipy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}


def load_image(path: Path) -> Image.Image:
    if path.suffix.lower() == ".heic":
        try:
            from pillow_heif import register_heif_opener  # iPhone写真用
            register_heif_opener()
        except ImportError:
            raise RuntimeError("HEIC を読むには `pip install pillow-heif` が必要です")
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # スマホ写真の向きを補正
    return img.convert("RGB")


# ── 切り抜きAI（onnxruntime で直接実行） ──────────────────────────
MODELS = {
    # 名前: (URL, md5, 平均, 標準偏差, sigmoid を通すか, 目安サイズMB)
    "isnet-general-use": (
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
        "fc16ebd8b0c10d971d3513d564d01e29", (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), False, 180),
    "birefnet-general": (
        "https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-epoch_244.onnx",
        "7a35a0141cbbc80de11d9c9a28f52697", (0.485, 0.456, 0.406), (0.229, 0.224, 0.225), True, 970),
}
BUNDLED_MODEL_DIRS: list = []  # アプリ版が同梱モデルの場所を登録する


def model_home() -> Path:
    import os
    d = Path(os.getenv("SHUPPIN_MODEL_HOME", Path.home() / ".shuppin_photo" / "models"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(name: str, progress=None) -> Path:
    """モデルファイルの場所を返す。無ければダウンロードする"""
    import hashlib
    import urllib.request
    if name not in MODELS:
        raise ValueError(f"不明なモデル: {name}（使えるもの: {', '.join(MODELS)}）")
    url, md5, *_ = MODELS[name]
    fname = f"{name}.onnx"
    for d in [*BUNDLED_MODEL_DIRS, model_home(), Path.home() / ".rembg" / "models" / name]:
        f = Path(d) / fname
        if f.exists():
            return f
    dst = model_home() / fname
    tmp = dst.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, context=_ssl_context(), timeout=60) as r, open(tmp, "wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(1.0, done / total))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            "切り抜きAI（高精度）のダウンロードに失敗しました。インターネット接続を確認してください。"
            "会社のネットワークやセキュリティソフトが通信を止めている場合もあります。"
            "「標準」の精度ならダウンロードなしで使えます。\n（詳細: " + str(e) + "）") from None
    h = hashlib.md5()
    with open(tmp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != md5:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("モデルのダウンロードが壊れています。もう一度お試しください")
    tmp.replace(dst)
    return dst


def _ssl_context():
    """パソコンに登録されている証明書を使って安全に接続する（セキュリティソフト等にも対応）"""
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class Segmenter:
    """商品（前景）のマスクを推定する"""

    def __init__(self, name: str = "isnet-general-use", progress=None):
        import onnxruntime as ort
        self.name = name
        _, _, self.mean, self.std, self.sigmoid, _ = MODELS[name]
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(model_path(name, progress)), opts,
                                         providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name

    def predict_raw(self, img: Image.Image) -> np.ndarray:
        """AIの生の出力（1024x1024, 0-255）。キャッシュして使い回せる"""
        im = np.asarray(img.convert("RGB").resize((1024, 1024), Image.LANCZOS), dtype=np.float32)
        im = im / max(float(im.max()), 1e-6)
        im = (im - np.array(self.mean, np.float32)) / np.array(self.std, np.float32)
        x = im.transpose(2, 0, 1)[None].astype(np.float32)
        pred = self.sess.run(None, {self.input_name: x})[0][:, 0, :, :]
        if self.sigmoid:
            pred = 1 / (1 + np.exp(-pred))
        mi, ma = float(pred.min()), float(pred.max())
        pred = np.squeeze((pred - mi) / max(ma - mi, 1e-6))
        return (pred * 255).astype("uint8")

    def predict(self, img: Image.Image, raw: np.ndarray | None = None) -> Image.Image:
        if raw is None:
            raw = self.predict_raw(img)
        return Image.fromarray(raw, mode="L").resize(img.size, Image.LANCZOS)


def new_session(name: str = "isnet-general-use", progress=None) -> Segmenter:
    return Segmenter(name, progress)


WORK_SIDE = 1600  # 切り抜きの計算はこの大きさで行い、最後になめらかに拡大する


def _morph(op: str, m: np.ndarray, r: int) -> np.ndarray:
    """写真のふちで欠けないように、ふちを延長してから開閉処理する"""
    from scipy import ndimage
    pad = r + 2
    mp = np.pad(m, pad, mode="edge")
    f = ndimage.binary_opening if op == "open" else ndimage.binary_closing
    return f(mp, structure=_disk(r))[pad:-pad, pad:-pad]


def _disk(r: int) -> np.ndarray:
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def make_mask(img: Image.Image, session: Segmenter, raw: np.ndarray | None = None,
              refine: bool = True) -> Image.Image:
    """商品部分=255, 背景=0 のマスク（ふちは なめらか）を元の写真サイズで返す"""
    from scipy import ndimage
    work = img if max(img.size) <= WORK_SIDE else img.resize(_fit(img.size, WORK_SIDE), Image.LANCZOS)
    soft = np.asarray(session.predict(work, raw)).astype(np.float32)
    soft = ndimage.gaussian_filter(soft, sigma=1.0)
    # AIが確信している部分＋それにつながる「半分くらい自信がある部分」（タグ・ラベル等）
    sure = soft >= 127
    maybe = soft >= 55
    lab, _ = ndimage.label(maybe)
    ids = np.unique(lab[sure])
    hard = np.isin(lab, ids[ids > 0]) if sure.any() else sure
    hard = clean_mask(hard)
    if refine:
        refined = refine_gaps(work, hard, soft)
        removed = hard & ~refined
        if removed.any():
            # すき間を消したあと、ふちを軽く整える（消した背景は戻さない）
            sm = ndimage.gaussian_filter(refined.astype(np.float32), sigma=max(1.0, max(hard.shape) * 0.001))
            hard = (sm >= 0.5) & ~removed
    if refine:
        hard = edge_cleanup(work, hard, soft)
        hard = _morph("open", hard, 1)
    return upscale_mask(hard, img.size)


def clean_mask(hard: np.ndarray) -> np.ndarray:
    """ふちのギザギザ・点々のゴミ・小さな穴を取り除く（タグなど服につながった物は残す）"""
    from scipy import ndimage
    if not hard.any():
        return hard
    n = max(hard.shape)
    # 1) 離れた小さなゴミを消す（いちばん大きい部分の0.3%未満）
    lab, k = ndimage.label(hard)
    if k > 1:
        areas = ndimage.sum(hard, lab, index=np.arange(1, k + 1))
        keep = np.flatnonzero(areas >= areas.max() * 0.003) + 1
        hard = np.isin(lab, keep)
    # 2) 服の中の小さな穴（点々）を埋める
    holes = ndimage.binary_fill_holes(hard) & ~hard
    lab, k = ndimage.label(holes)
    if k:
        areas = ndimage.sum(holes, lab, index=np.arange(1, k + 1))
        small = np.flatnonzero(areas < hard.sum() * 0.003) + 1
        hard = hard | np.isin(lab, small)
    # 3) 輪郭をなめらかに（小さなトゲ・欠けをならす）
    r = max(1, round(n * 0.0025))
    hard = _morph("open", hard, r)
    hard = _morph("close", hard, r)
    # 4) さらに輪郭のガタつきをぼかして整える
    sm = ndimage.gaussian_filter(hard.astype(np.float32), sigma=max(1.0, n * 0.0015), mode="nearest")
    return sm >= 0.5


def upscale_mask(hard: np.ndarray, size) -> Image.Image:
    """作業サイズのマスクを、元の写真サイズへなめらかに拡大（ふちは1〜2pxだけ柔らかく）"""
    from scipy import ndimage
    m = Image.fromarray(hard.astype(np.uint8) * 255).resize(size, Image.BILINEAR)
    a = np.asarray(m).astype(np.float32)
    scale = max(size) / max(hard.shape)
    a = ndimage.gaussian_filter(a, sigma=max(0.6, scale * 0.5))
    a = np.clip((a - 128) * 1.6 + 128, 0, 255)  # ふちの柔らかさは細く保つ
    return Image.fromarray(a.astype(np.uint8))


def edge_cleanup(img: Image.Image, hard: np.ndarray, soft: np.ndarray | None = None) -> np.ndarray:
    """輪郭ぞいに残った「背景色の細い切れ端」（すき間の先端・ふちの影の帯など）を消す。
    近くの背景の色と近くの服の色を比べ、背景に近い“細い”部分だけを消す（タグなど大きな物は残す）"""
    from scipy import ndimage
    n = max(hard.shape)
    r = max(2, round(n * 0.003))
    arr = ndimage.gaussian_filter(np.asarray(img, dtype=np.float32), sigma=(1.0, 1.0, 0))
    out = ~hard
    ring_in = hard & ndimage.binary_dilation(out, structure=_disk(r * 4))
    if not ring_in.any() or out.sum() < 1000:
        return hard
    sig = n * 0.02

    def local_mean(sel):
        w = ndimage.gaussian_filter(sel.astype(np.float32), sig) + 1e-6
        return np.stack([ndimage.gaussian_filter(arr[..., c] * sel, sig) / w for c in range(3)], -1)

    bg_l = local_mean(out & ~ndimage.binary_dilation(hard, iterations=2))
    fg_l = local_mean(hard & ~ring_in)
    d_bg = np.linalg.norm(arr - bg_l, axis=2)
    d_fg = np.linalg.norm(arr - fg_l, axis=2)
    contrast = np.linalg.norm(bg_l - fg_l, axis=2)
    cand = ring_in & (d_bg < d_fg * 0.7) & (contrast > 30)
    # 細い部分だけ（太い塊＝タグ等は残す）
    thin = cand & ~_morph("open", cand, r * 2)
    # 外の背景とつながっているものだけ
    lab, _ = ndimage.label(thin | out)
    ids = np.unique(lab[out])
    thin &= np.isin(lab, ids[ids > 0])
    # 背景色っぽい部分が内側まで「かたまり」で続いている＝タグ等の物体。消さない
    if thin.any():
        bglike = hard & (d_bg < d_fg * 0.7) & (contrast > 30)
        core = _morph("open", bglike, r * 2)
        if core.any():
            lab, _ = ndimage.label(bglike)
            ids = np.unique(lab[core])
            thin &= ~np.isin(lab, ids[ids > 0])
    # AIが「商品の一部」と確信している所（タグ・ラベル等）は消さない
    if soft is not None and thin.any():
        lab, k = ndimage.label(thin)
        means = ndimage.mean(soft, lab, index=np.arange(1, k + 1))
        thin &= np.isin(lab, np.flatnonzero(np.asarray(means) < 215) + 1)
    return hard & ~thin


def refine_gaps(img: Image.Image, hard: np.ndarray, soft: np.ndarray | None = None) -> np.ndarray:
    """パンツの股下や、袖と胴のすき間など「AIが商品扱いした背景」を消す。
    ・背景とほぼ同じ色で、外の背景とつながっている部分だけが対象
    ・消すのは「服にはさまれたすき間」か「ふちの細い帯（影など）」だけ。
      タグのように外に張り出した物は、背景と同じ色でも消さない"""
    from scipy import ndimage
    arr = ndimage.gaussian_filter(np.asarray(img, dtype=np.float32), sigma=(1.5, 1.5, 0))
    n = max(hard.shape)
    outside = ~ndimage.binary_dilation(hard, iterations=2)
    if outside.sum() < 1000 or not hard.any():
        return hard
    # 服のすぐ外側（帯状）の色を「その場所の背景色」とする（床・板・マットが混ざっていてもOK）
    ring = outside & ndimage.binary_dilation(hard, structure=_disk(max(3, round(n * 0.02))))
    if ring.sum() < 500:
        return hard
    sig = n * 0.03
    w = ndimage.gaussian_filter(ring.astype(np.float32), sig) + 1e-6
    bg_l = np.stack([ndimage.gaussian_filter(arr[..., ch] * ring, sig) / w for ch in range(3)], -1)
    dist = np.linalg.norm(arr - bg_l, axis=2)
    noise = float(np.percentile(dist[ring], 75))
    thresh = float(np.clip(noise * 2.0, 12, 30))  # 厳しめ：背景とほぼ同じ色だけ
    cand = (dist < thresh) & hard
    # 商品自体が背景と似た色（白い服を白い床で撮った等）なら何もしない
    if cand.sum() > hard.sum() * 0.25:
        return hard
    n = max(hard.shape)
    lab, k = ndimage.label(cand)
    if not k:
        return hard
    garment = hard & ~cand
    out_touch = ndimage.binary_dilation(~hard, iterations=1)
    gar_touch = ndimage.binary_dilation(garment, iterations=1)
    remove = np.zeros_like(hard)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        # 少し広げた範囲で計算（速さのため）
        y0, y1 = max(0, sl[0].start - 3), min(hard.shape[0], sl[0].stop + 3)
        x0, x1 = max(0, sl[1].start - 3), min(hard.shape[1], sl[1].stop + 3)
        reg = lab[y0:y1, x0:x1] == i
        edge = ndimage.binary_dilation(reg, iterations=1) & ~reg
        t_out = (edge & out_touch[y0:y1, x0:x1] & ~hard[y0:y1, x0:x1]).sum()
        if t_out == 0:
            continue  # 外とつながっていない＝服の柄など。消さない
        t_gar = (edge & gar_touch[y0:y1, x0:x1] & garment[y0:y1, x0:x1]).sum()
        frac = t_gar / max(1, t_gar + t_out)
        thick = ndimage.distance_transform_edt(np.pad(reg, 1))[1:-1, 1:-1].max()
        is_gap = frac >= 0.6                 # 服にはさまれたすき間
        is_band = thick <= n * 0.006        # ふちに沿った細い帯
        # AIが「商品の一部」と確信している所（タグ・ラベル等）は消さない
        sure = soft is not None and float(soft[y0:y1, x0:x1][reg].mean()) >= 200
        if (is_gap or is_band) and not sure:
            remove[y0:y1, x0:x1] |= reg
    return hard & ~remove


def bbox_from_mask(mask: Image.Image, thresh: int = 20):
    arr = np.asarray(mask)
    ys, xs = np.where(arr > thresh)
    if len(xs) == 0:
        return None
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def parse_ratio(s: str):
    w, h = s.split(":")
    return float(w), float(h)


def rotate_img(img: Image.Image, rotation: int) -> Image.Image:
    """時計回りに rotation 度（0/90/180/270）回す"""
    r = rotation % 360
    if r == 90:
        return img.transpose(Image.ROTATE_270)
    if r == 180:
        return img.transpose(Image.ROTATE_180)
    if r == 270:
        return img.transpose(Image.ROTATE_90)
    return img


def rotate_raw(raw: np.ndarray, rotation: int) -> np.ndarray:
    return np.rot90(raw, k=-(rotation % 360) // 90).copy()


def capture_time(path: Path) -> float:
    """撮影日時（Exif）。無ければファイルの更新日時"""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            dt = ex.get_ifd(0x8769).get(36867) or ex.get(306)
            if dt:
                import datetime as _dt
                return _dt.datetime.strptime(str(dt).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def analyze(path: Path, session: Segmenter | None, auto_rotate: bool = True) -> dict:
    """一覧に表示する前の下調べ：AIマスク（キャッシュ用）、向き、サムネイル"""
    import orientation
    img = load_image(path)
    info = {"size": img.size, "raw": None, "rotation": 0, "kind": None, "confident": False, "closeup": False}
    if session is not None:
        raw = session.predict_raw(img)
        info["raw"] = raw
        small = img.resize(_fit(img.size, 600), Image.BILINEAR)
        m = np.asarray(make_mask(small, session, raw))
        info["closeup"] = is_closeup(m)
        if auto_rotate and not info["closeup"]:
            info.update(orientation.detect(m))
    thumb = img.copy()
    thumb.thumbnail((360, 360), Image.LANCZOS)
    info["thumb_orig"] = thumb.copy()
    if info["raw"] is not None:
        tm = Image.fromarray(info["raw"]).resize(thumb.size, Image.BILINEAR)
        thumb = Image.composite(thumb, Image.new("RGB", thumb.size, (255, 255, 255)), tm)
    info["thumb"] = thumb
    return info


def _fit(size, max_side):
    w, h = size
    s = max_side / max(w, h)
    return max(1, round(w * s)), max(1, round(h * s))


# ── アップ写真（素材感・ボタン・ジップなど） ─────────────────────────
def is_closeup(mask: np.ndarray) -> bool:
    """服が画面いっぱい（2辺以上のふちにかかる・大きく写る）ならアップ写真とみなす"""
    m = mask > 127 if mask.dtype != bool else mask
    area = float(m.mean())
    edges = [m[:3].mean(), m[-3:].mean(), m[:, :3].mean(), m[:, -3:].mean()]
    top, bottom, left, right = (e > 0.15 for e in edges)
    touching = top + bottom + left + right
    # 隣り合う2辺（角）にかかる＝寄って撮った写真。上下だけ・左右だけは長い服の全体写真
    corner = (top or bottom) and (left or right)
    return area > 0.6 or touching >= 3 or (corner and sum(edges) >= 0.6)


def default_crop() -> tuple:
    """(中心x, 中心y, 一辺) … いずれも写真に対する割合。一辺は短い辺に対する割合"""
    return (0.5, 0.5, 1.0)


def crop_box(size, crop) -> tuple:
    w, h = size
    cx, cy, sz = crop
    side = max(16, min(w, h) * min(1.0, max(0.05, sz)))
    x0 = min(max(0, cx * w - side / 2), w - side)
    y0 = min(max(0, cy * h - side / 2), h - side)
    return int(round(x0)), int(round(y0)), int(round(x0 + side)), int(round(y0 + side))


def whiten_board(img: Image.Image, protect: Image.Image | None = None) -> Image.Image:
    """写り込んだ白い板・白い床（写真のふちにつながっている部分）だけを白くする。
    タグ（色つきの紙）や服は残す"""
    from scipy import ndimage
    work = img if max(img.size) <= 1200 else img.resize(_fit(img.size, 1200), Image.LANCZOS)
    arr = ndimage.gaussian_filter(np.asarray(work, dtype=np.float32), sigma=(2, 2, 0))
    h, w = arr.shape[:2]
    L = arr.mean(axis=2)
    chroma = arr.max(axis=2) - arr.min(axis=2)
    bright_neutral = (L >= 170) & (chroma <= 25)
    b = max(3, int(min(h, w) * 0.02))
    border = np.zeros((h, w), bool)
    border[:b] = border[-b:] = True
    border[:, :b] = border[:, -b:] = True
    seed = border & bright_neutral
    if seed.sum() < border.sum() * 0.05:
        return img  # 白い板が写っていない
    board = np.median(arr[seed], axis=0)
    d = np.linalg.norm(arr - board, axis=2)
    tol = float(np.clip(np.percentile(d[seed], 90) * 1.5, 18, 45))
    board_L = float(board.mean())
    # 板そのもの＋影で少し暗くなった板（無彩色のまま明るさだけ下がった所）
    bg = (d < tol) | ((chroma <= 12) & (L >= board_L * 0.68))
    if protect is not None:  # AIが商品（服・タグ）と確信している所は白くしない
        bg &= ~(np.asarray(protect.resize((w, h), Image.BILINEAR)) >= 200)
    lab, _ = ndimage.label(bg)
    ids = np.unique(lab[seed])
    bg = np.isin(lab, ids[ids > 0])
    # 点々を作らないように整える
    r = max(2, round(max(h, w) * 0.004))
    bg = _morph("open", bg, r)
    bg = _morph("close", bg, r)
    lab, k = ndimage.label(bg)
    if k:
        areas = ndimage.sum(bg, lab, index=np.arange(1, k + 1))
        bg = np.isin(lab, np.flatnonzero(areas >= h * w * 0.002) + 1)
    if not bg.any():
        return img
    a = Image.fromarray(bg.astype(np.uint8) * 255).resize(img.size, Image.BILINEAR)
    a = np.asarray(a.filter(ImageFilter.GaussianBlur(max(1.0, img.size[0] / w * 1.2)))).astype(np.float32)[..., None] / 255
    out = np.asarray(img, dtype=np.float32) * (1 - a) + 255 * a
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def render_closeup(path: Path, args, rotation: int = 0, crop=None, whiten: bool = True,
                   raw: np.ndarray | None = None) -> Image.Image:
    """アップ写真：正方形に切り抜き（位置は crop）、白い板を白くして出力サイズにする"""
    img = rotate_img(load_image(path), rotation)
    box = crop_box(img.size, crop or default_crop())
    sq = img.crop(box)
    protect = None
    if raw is not None:
        protect = Image.fromarray(rotate_raw(raw, rotation)).resize(img.size, Image.BILINEAR).crop(box)
    if sq.size[0] > args.size * 1.5:
        sq = sq.resize((int(args.size * 1.5),) * 2, Image.LANCZOS)
    if whiten and not args.keep_bg:
        sq = whiten_board(sq, protect)
    return sq.resize((args.size, args.size), Image.LANCZOS)


def extract_subject(path: Path, args, session, rotation: int = 0, raw: np.ndarray | None = None):
    """商品部分だけを切り出す。(画像, マスク or None) を返す"""
    img = rotate_img(load_image(path), rotation)
    if raw is not None:
        raw = rotate_raw(raw, rotation)
    if args.keep_bg:
        return img, None
    mask = make_mask(img, session, raw, refine=not args.no_refine)
    box = bbox_from_mask(mask)
    if box is None:
        raise RuntimeError("商品を検出できませんでした")
    white = Image.new("RGB", img.size, (255, 255, 255))
    subject = Image.composite(img, white, mask)
    return subject.crop(box), mask.crop(box)


def compose(items, args) -> Image.Image:
    """切り出した商品（1つ or 複数）を白いキャンバスの中央に並べる"""
    rw, rh = parse_ratio(args.ratio)
    out_w = args.size
    out_h = round(args.size * rh / rw)
    pad = round(min(out_w, out_h) * args.margin)
    gap = round(min(out_w, out_h) * args.gap) if len(items) > 1 else 0
    avail_w, avail_h = out_w - pad * 2, out_h - pad * 2

    # 全アイテムを同じ倍率で縮小（表と裏の大きさがそろう）
    total_w = sum(im.width for im, _ in items)
    max_h = max(im.height for im, _ in items)
    scale = min((avail_w - gap * (len(items) - 1)) / total_w, avail_h / max_h)

    resized = []
    for im, m in items:
        size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        resized.append((im.resize(size, Image.LANCZOS),
                        m.resize(size, Image.LANCZOS) if m is not None else None))

    group_w = sum(im.width for im, _ in resized) + gap * (len(resized) - 1)
    x = (out_w - group_w) // 2
    canvas = Image.new("RGB", (out_w, out_h), (255, 255, 255))

    positions = []
    for im, m in resized:
        y = (out_h - im.height) // 2
        positions.append((im, m, x, y))
        x += im.width + gap

    if args.shadow:
        shadow = Image.new("L", (out_w, out_h), 0)
        for im, m, px, py in positions:
            if m is not None:
                shadow.paste(m.point(lambda v: int(v * 0.22)), (px, py + round(out_h * 0.012)))
        shadow = shadow.filter(ImageFilter.GaussianBlur(out_w * 0.012))
        canvas = Image.composite(Image.new("RGB", (out_w, out_h), (0, 0, 0)), canvas, shadow)

    for im, m, px, py in positions:
        canvas.paste(im, (px, py), m) if m is not None else canvas.paste(im, (px, py))
    return canvas


def save(canvas: Image.Image, out_dir: Path, stem: str, args, overwrite: bool = True) -> Path:
    out_path = out_dir / (stem + ".jpg")
    n = 2
    while not overwrite and out_path.exists():
        out_path = out_dir / f"{stem}_{n}.jpg"
        n += 1
    canvas.save(out_path, "JPEG", quality=args.quality, optimize=True, subsampling=0)
    return out_path


PAIR_SUFFIXES = [("表", "裏"), ("front", "back"), ("f", "b"), ("おもて", "うら")]


def split_pair_name(stem: str):
    """'pants01_表' → ('pants01', 0) / 'pants01_裏' → ('pants01', 1) / 該当なし → None"""
    low = stem.lower()
    for front, back in PAIR_SUFFIXES:
        for idx, suf in enumerate((front, back)):
            for sep in ("_", "-", " ", ""):
                tail = sep + suf
                if low.endswith(tail) and len(stem) > len(tail):
                    # 'f'/'b' は区切り文字ありのときだけ認識（誤判定防止）
                    if len(suf) == 1 and sep == "":
                        continue
                    return stem[: -len(tail)], idx
    return None


def make_groups(files, pair: bool):
    """処理単位（1枚 or 2枚）のリストと出力名を返す"""
    if not pair:
        return [([f], f.stem) for f in files]

    named, rest = {}, []
    for f in files:
        r = split_pair_name(f.stem)
        if r:
            named.setdefault(r[0], [None, None])[r[1]] = f
        else:
            rest.append(f)

    groups = []
    for base, (front, back) in named.items():
        if front and back:
            groups.append(([front, back], base))
        else:  # 片方しかない場合は名前順ペアの候補に回す
            rest.append(front or back)
    rest.sort()
    for i in range(0, len(rest) - 1, 2):
        a, b = rest[i], rest[i + 1]
        groups.append(([a, b], f"{a.stem}+{b.stem}"))
    if len(rest) % 2 == 1:
        groups.append(([rest[-1]], rest[-1].stem))
    groups.sort(key=lambda g: g[1])
    return groups


def main():
    p = argparse.ArgumentParser(description="出品写真の背景を白にして、正方形に整えます")
    p.add_argument("input", help="写真が入っているフォルダ（または1枚の画像）")
    p.add_argument("output", help="保存先フォルダ")
    p.add_argument("--size", type=int, default=1200, help="出力の横幅px（デフォルト1200）")
    p.add_argument("--ratio", default="1:1", help="縦横比 横:縦（デフォルト 1:1）")
    p.add_argument("--margin", type=float, default=0.06, help="余白の割合（デフォルト0.06）")
    p.add_argument("--quality", type=int, default=92, help="JPEG画質（デフォルト92）")
    p.add_argument("--keep-bg", action="store_true", help="背景を消さない")
    p.add_argument("--shadow", action="store_true", help="うっすら影をつける")
    p.add_argument("--pair", action="store_true", help="表・裏の2枚を横に並べて1枚にまとめる")
    p.add_argument("--gap", type=float, default=0.04, help="--pair 時の間隔の割合（デフォルト0.04）")
    p.add_argument("--no-refine", action="store_true", help="すき間の背景除去を行わない")
    p.add_argument("--model", default="isnet-general-use",
                   help="切り抜きモデル: isnet-general-use（標準）/ u2net / birefnet-general（高精度・重い）")
    args = p.parse_args()

    src = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = [src] if src.is_file() else sorted(
        f for f in src.iterdir() if f.suffix.lower() in EXTS and not f.name.startswith(".")
    )
    if not files:
        sys.exit("画像が見つかりませんでした")

    session = None
    if not args.keep_bg:
        print(f"切り抜きモデルを準備中: {args.model}（初回はダウンロードに少し時間がかかります）")
        session = new_session(args.model)

    groups = make_groups(files, args.pair)
    ok, ng = 0, []
    for i, (group, stem) in enumerate(groups, 1):
        names = " + ".join(f.name for f in group)
        try:
            items = [extract_subject(f, args, session) for f in group]
            out = save(compose(items, args), out_dir, stem, args)
            ok += 1
            print(f"[{i}/{len(groups)}] OK  {names} -> {out.name}")
        except Exception as e:
            ng.append((names, str(e)))
            print(f"[{i}/{len(groups)}] NG  {names}: {e}")

    print(f"\n完了: {ok}枚 成功 / {len(ng)}枚 失敗  保存先: {out_dir}")
    for name, err in ng:
        print(f"  - {name}: {err}")


if __name__ == "__main__":
    main()
