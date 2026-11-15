"""向き判定のテスト：服のシルエットを4方向＋少しの傾き・ノイズで回して判定させる"""
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orientation  # noqa: E402

W = H = 600


def poly(pts, extra=None):
    im = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(im)
    d.polygon(pts, fill=255)
    if extra:
        extra(d)
    return im


def tshirt():
    def neck(d): d.ellipse((260, 70, 340, 140), fill=0)
    return poly([(170, 100), (260, 80), (340, 80), (430, 100), (520, 220), (450, 260), (420, 220),
                 (420, 520), (180, 520), (180, 220), (150, 260), (80, 220)], neck)


def longsleeve():
    def neck(d): d.ellipse((262, 60, 338, 120), fill=0)
    return poly([(200, 90), (262, 70), (338, 70), (400, 90), (470, 140), (520, 500), (470, 510),
                 (420, 250), (415, 540), (185, 540), (180, 250), (130, 510), (80, 500), (130, 140)], neck)


def collar_shirt():
    return poly([(270, 60), (300, 75), (330, 60), (420, 100), (510, 230), (445, 265), (415, 220),
                 (420, 540), (180, 540), (185, 220), (155, 265), (90, 230), (180, 100)])


def tank():
    def neck(d): d.ellipse((240, 40, 360, 200), fill=0)
    return poly([(230, 60), (260, 60), (340, 60), (370, 60), (400, 200), (410, 540), (190, 540), (200, 200)], neck)


def pants():
    return poly([(200, 40), (400, 40), (420, 300), (440, 570), (330, 570), (300, 260), (270, 570),
                 (160, 570), (180, 300)])


def shorts():
    return poly([(170, 150), (430, 150), (460, 330), (470, 440), (320, 450), (300, 330), (280, 450),
                 (130, 440), (140, 330)])


def skirt():
    return poly([(230, 80), (370, 80), (390, 150), (480, 520), (120, 520), (210, 150)])


def dress():
    def neck(d): d.ellipse((255, 30, 345, 120), fill=0)
    return poly([(225, 40), (375, 40), (390, 110), (380, 260), (470, 570), (130, 570), (220, 260), (210, 110)], neck)


SHAPES = {"Tシャツ": (tshirt, "top"), "長袖": (longsleeve, "top"), "襟付きシャツ": (collar_shirt, "top"),
          "タンクトップ": (tank, "top"), "パンツ": (pants, "pants"), "ショートパンツ": (shorts, "pants"),
          "スカート": (skirt, "skirt"), "ワンピース": (dress, "top")}


def jitter(im: Image.Image, rnd: random.Random) -> Image.Image:
    im = im.rotate(rnd.uniform(-8, 8), resample=Image.BILINEAR, expand=True)
    im = im.filter(ImageFilter.GaussianBlur(rnd.uniform(0.5, 3)))
    a = np.asarray(im).astype(np.float32) + np.asarray(
        Image.effect_noise(im.size, 40)).astype(np.float32) - 128
    return Image.fromarray(np.where(a > 127, 255, 0).astype(np.uint8))


def main():
    rnd = random.Random(0)
    total = ok = wrong = unsure = 0
    for name, (fn, kind) in SHAPES.items():
        res = []
        for photo_rot in (0, 90, 180, 270):  # 写真が時計回りに何度ずれて撮られたか
            for trial in range(3):
                m = fn().rotate(-photo_rot, expand=True)  # PIL は反時計回り
                m = jitter(m, rnd)
                # 横長・縦長の写真に入っている状態を再現
                r = orientation.detect(np.asarray(m))
                want = (360 - photo_rot) % 360
                total += 1
                if not r["confident"]:
                    unsure += 1; res.append("?")
                elif r["rotation"] == want:
                    ok += 1; res.append("○")
                else:
                    wrong += 1; res.append("×")
        print(f"{name:<8} {''.join(res)}")
    print(f"\n正解 {ok}/{total}  判定保留 {unsure}  間違い {wrong}")
    return wrong


if __name__ == "__main__":
    sys.exit(0 if main() <= 5 else 1)  # 間違いが5件を超えたら失敗
