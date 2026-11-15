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

    def hook(blocks, bsize, total):
        if progress and total > 0:
            progress(min(1.0, blocks * bsize / total))

    urllib.request.urlretrieve(url, tmp, reporthook=hook)
    h = hashlib.md5()
    with open(tmp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != md5:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("モデルのダウンロードが壊れています。もう一度お試しください")
    tmp.replace(dst)
    return dst


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


def make_mask(img: Image.Image, session: Segmenter, raw: np.ndarray | None = None) -> Image.Image:
    """商品部分=255, 背景=0 のマスクを返す"""
    from scipy import ndimage
    m = np.asarray(session.predict(img, raw))
    # 境界をなめらかにして2値化（小さなノイズも除去）
    cross = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)
    m = ndimage.grey_opening(m, footprint=cross)
    m = ndimage.gaussian_filter(m.astype(np.float32), sigma=2)
    m = np.where(m < 127, 0, 255).astype(np.uint8)
    alpha = Image.fromarray(m).filter(ImageFilter.MedianFilter(3))
    return alpha


def refine_gaps(img: Image.Image, mask: Image.Image) -> Image.Image:
    """パンツの股下や袖と胴のすき間など、AIが商品扱いした「背景色の部分」を消す。
    背景とつながっていて、背景とほぼ同じ色の領域だけを対象にする。"""
    from scipy import ndimage
    arr = np.asarray(img, dtype=np.float32)
    m = np.asarray(mask)
    outside = m < 10
    if outside.sum() < 1000:
        return mask
    bg = np.median(arr[outside], axis=0)
    dist = np.linalg.norm(arr - bg, axis=2)
    thresh = max(14.0, float(np.percentile(dist[outside], 90)) * 1.3)
    cand = dist < thresh
    inside = m >= 128
    # 商品自体が背景と似た色（白い服を白い床で撮った等）なら誤消去を避けて何もしない
    if cand[inside].mean() > 0.25:
        return mask
    labels, _ = ndimage.label(cand)
    bg_ids = np.unique(labels[outside & cand])
    bg_ids = bg_ids[bg_ids > 0]
    remove = np.isin(labels, bg_ids) & (m >= 10)
    remove = ndimage.binary_opening(remove, iterations=1)  # 細かいノイズは消さない
    out = m.copy()
    out[remove] = 0
    return Image.fromarray(out).filter(ImageFilter.MedianFilter(3))


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
    info = {"size": img.size, "raw": None, "rotation": 0, "kind": None, "confident": False}
    if session is not None:
        raw = session.predict_raw(img)
        info["raw"] = raw
        if auto_rotate:
            # 判定は縮小画像で（股下のすき間もきれいにしてから）
            small = img.resize(_fit(img.size, 600), Image.BILINEAR)
            m = refine_gaps(small, make_mask(small, session, raw))
            info.update(orientation.detect(np.asarray(m)))
    thumb = img.copy()
    thumb.thumbnail((360, 360), Image.LANCZOS)
    if info["raw"] is not None:
        tm = Image.fromarray(info["raw"]).resize(thumb.size, Image.BILINEAR)
        thumb = Image.composite(thumb, Image.new("RGB", thumb.size, (255, 255, 255)), tm)
    info["thumb"] = thumb
    return info


def _fit(size, max_side):
    w, h = size
    s = max_side / max(w, h)
    return max(1, round(w * s)), max(1, round(h * s))


def extract_subject(path: Path, args, session, rotation: int = 0, raw: np.ndarray | None = None):
    """商品部分だけを切り出す。(画像, マスク or None) を返す"""
    img = rotate_img(load_image(path), rotation)
    if raw is not None:
        raw = rotate_raw(raw, rotation)
    if args.keep_bg:
        return img, None
    mask = make_mask(img, session, raw)
    if not args.no_refine:
        mask = refine_gaps(img, mask)
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
