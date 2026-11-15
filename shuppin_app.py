#!/usr/bin/env python3
"""
出品写真ツール（Windows / Mac 共通アプリ）

写真（何枚でも・フォルダでも）をウィンドウにドラッグ＆ドロップ
→ 一覧で順番と向きを確認 → 「加工スタート」で、
背景を白くして正方形にそろえた出品用写真を作ります。
"""

from __future__ import annotations

import json
import os
import platform
import queue
import subprocess
import sys
import threading
import traceback
from argparse import Namespace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))


def user_data_dir() -> Path:
    """設定やAIモデルを置く、ユーザーが書き込めるフォルダ"""
    if sys.platform.startswith("win"):
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "ShuppinPhoto"
    d.mkdir(parents=True, exist_ok=True)
    return d


import shuppin_photo as core  # noqa: E402

os.environ.setdefault("SHUPPIN_MODEL_HOME", str(user_data_dir() / "models"))
core.BUNDLED_MODEL_DIRS.append(HERE / "models")

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    HAS_DND = False

from PIL import Image, ImageTk  # noqa: E402

APP_TITLE = "出品写真ツール"
MODELS = {"標準（速い）": "isnet-general-use", "高精度（遅い）": "birefnet-general"}
ANALYZE_MODEL = "isnet-general-use"
SIZES = ["1200", "1080", "1500", "1000", "800"]
CONFIG_PATH = user_data_dir() / "config.json"

PAIR_COLORS = ["#e3efff", "#fff0dc"]  # ペアごとの背景色（交互）
ACCENT = "#2a6fdb"


def default_output_dir() -> Path:
    for d in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop",
              Path.home() / "OneDrive" / "デスクトップ", Path.home() / "Pictures"):
        if d.is_dir():
            return d / "出品写真_加工済み"
    return Path.home() / "出品写真_加工済み"


def load_config() -> dict:
    cfg = {"output_dir": str(default_output_dir()), "pair": False, "auto_rotate": True,
           "shadow": True, "keep_bg": False, "size": "1200", "model": list(MODELS)[0], "margin": 6}
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def open_folder(path: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def enable_sharp_text():
    """Windows の高解像度画面で文字がぼやけないようにする（Tk を作る前に呼ぶ）"""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def setup_fonts(root: tk.Tk) -> tuple:
    families = set(tkfont.families(root))
    if sys.platform.startswith("win"):
        fam = next((f for f in ("Yu Gothic UI", "Meiryo UI", "Meiryo") if f in families), None)
        size = 10
    elif sys.platform == "darwin":
        fam = next((f for f in ("Hiragino Sans", "Hiragino Kaku Gothic ProN") if f in families), None)
        size = 13
    else:
        fam = next((f for f in ("Noto Sans CJK JP", "Noto Sans JP", "IPAGothic") if f in families), None)
        size = 10
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        try:
            f = tkfont.nametofont(name)
            f.configure(size=size, **({"family": fam} if fam else {}))
        except tk.TclError:
            pass
    base = tkfont.nametofont("TkDefaultFont").actual()
    return base["family"], size


def expand_paths(paths) -> list[Path]:
    """ドロップ／選択されたもの（ファイル・フォルダ混在OK）を画像ファイルの一覧にする"""
    out = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in core.EXTS
                    and not f.name.startswith(".")]
        elif p.is_file() and p.suffix.lower() in core.EXTS:
            out.append(p)
    # 1回に入れた分は撮影日時順に並べる
    return sorted(out, key=lambda f: (core.capture_time(f), f.name))


class Item:
    _next = 0

    def __init__(self, path: Path):
        Item._next += 1
        self.id = Item._next
        self.path = path
        self.state = "wait"  # wait / ready / error
        self.rotation = 0
        self.auto_rotation = 0
        self.link_next = False  # 次の写真と表・裏ペアにする
        self.kind = None
        self.confident = False
        self.raw = None
        self.thumb: Image.Image | None = None
        self.error = ""


def make_groups(items: list) -> list[list]:
    """一覧の順に、つながっている2枚はペア、それ以外は1枚ずつ"""
    groups, i = [], 0
    while i < len(items):
        if items[i].link_next and i + 1 < len(items):
            groups.append([items[i], items[i + 1]])
            i += 2
        else:
            groups.append([items[i]])
            i += 1
    return groups


def normalize_links(items: list):
    """1枚が2つのペアに入らないように整える"""
    for g in make_groups(items):
        g[-1].link_next = False
        if len(g) == 1:
            g[0].link_next = False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.items: list[Item] = []
        self.ui_q: "queue.Queue[tuple]" = queue.Queue()
        self.analyze_q: "queue.Queue[Item]" = queue.Queue()
        self.sessions: dict = {}
        self.session_lock = threading.Lock()
        self.processing = False
        self.photos: dict = {}  # PhotoImage のキャッシュ
        self.drag = None

        self.fam, self.fsize = setup_fonts(root)
        self.sf = max(1.0, root.winfo_fpixels("1i") / 96)  # 画面の拡大率
        self.tile_w = int(150 * self.sf)
        self.tile_h = int(222 * self.sf)
        self.thumb_box = int(128 * self.sf)
        self.gap = int(34 * self.sf)

        root.title(APP_TITLE)
        root.geometry(f"{int(860 * self.sf)}x{int(720 * self.sf)}")
        root.minsize(int(620 * self.sf), int(520 * self.sf))
        style = ttk.Style()
        style.configure("Big.TButton", font=(self.fam, self.fsize + 2, "bold"), padding=int(8 * self.sf))
        pad = {"padx": int(12 * self.sf), "pady": int(5 * self.sf)}

        # ── 保存先（固定・いつでも変更可） ──
        f_out = ttk.LabelFrame(root, text="保存先（変更するまで固定されます）")
        f_out.pack(fill="x", **pad)
        self.out_var = tk.StringVar(value=self.cfg["output_dir"])
        ttk.Label(f_out, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=8, pady=6)
        ttk.Button(f_out, text="開く", command=self.open_output).pack(side="right", padx=(4, 8))
        ttk.Button(f_out, text="変更…", command=self.pick_output).pack(side="right")

        # ── 設定 ──
        f_opt = ttk.LabelFrame(root, text="設定")
        f_opt.pack(fill="x", **pad)
        self.rot_var = tk.BooleanVar(value=self.cfg["auto_rotate"])
        self.shadow_var = tk.BooleanVar(value=self.cfg["shadow"])
        self.keepbg_var = tk.BooleanVar(value=self.cfg["keep_bg"])
        r2 = ttk.Frame(f_opt); r2.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Checkbutton(r2, text="向きを自動で直す（トップス・パンツ・スカート）", variable=self.rot_var,
                        command=self.on_auto_rotate).pack(side="left")
        ttk.Checkbutton(r2, text="うっすら影", variable=self.shadow_var,
                        command=self.on_option).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(r2, text="背景は消さない", variable=self.keepbg_var,
                        command=self.on_option).pack(side="left", padx=(16, 0))
        r3 = ttk.Frame(f_opt); r3.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(r3, text="サイズ(px)").pack(side="left")
        self.size_var = tk.StringVar(value=self.cfg["size"])
        cb = ttk.Combobox(r3, textvariable=self.size_var, values=SIZES, width=6)
        cb.pack(side="left", padx=(4, 16)); cb.bind("<<ComboboxSelected>>", lambda e: self.on_option())
        ttk.Label(r3, text="切り抜き精度").pack(side="left")
        self.model_var = tk.StringVar(value=self.cfg["model"] if self.cfg["model"] in MODELS else list(MODELS)[0])
        cm = ttk.Combobox(r3, textvariable=self.model_var, values=list(MODELS), state="readonly", width=12)
        cm.pack(side="left", padx=(4, 16)); cm.bind("<<ComboboxSelected>>", lambda e: self.on_option())
        ttk.Label(r3, text="余白").pack(side="left")
        self.margin_var = tk.DoubleVar(value=self.cfg["margin"])
        self.margin_lbl = ttk.Label(r3, text=f"{self.cfg['margin']:.0f}%", width=4)
        ttk.Scale(r3, from_=0, to=20, variable=self.margin_var, length=int(140 * self.sf),
                  command=lambda v: (self.margin_lbl.config(text=f"{float(v):.0f}%"), self.on_option())
                  ).pack(side="left", padx=4)
        self.margin_lbl.pack(side="left")

        # ── 写真一覧 ──
        f_list = ttk.LabelFrame(root, text="写真（ドラッグで並び替え ／ ⟲⟳で回転 ／ ✕で外す ／ 写真の間の丸ボタンで表・裏ペア）")
        f_list.pack(fill="both", expand=True, **pad)
        bar = ttk.Frame(f_list); bar.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Button(bar, text="写真を追加…", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="フォルダを追加…", command=self.pick_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="すべて外す", command=self.clear_items).pack(side="left", padx=4)
        ttk.Button(bar, text="2枚ずつペアにする", command=lambda: self.set_all_links(True)).pack(side="left", padx=(16, 4))
        ttk.Button(bar, text="ペアを全部解除", command=lambda: self.set_all_links(False)).pack(side="left")
        self.count_lbl = ttk.Label(bar, text="")
        self.count_lbl.pack(side="right")

        holder = ttk.Frame(f_list); holder.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.canvas = tk.Canvas(holder, bg="white", highlightthickness=1, highlightbackground="#ccc")
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        for w in (self.canvas, root):
            w.bind("<MouseWheel>", self.on_wheel)
            w.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            w.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))

        # ── 実行 ──
        f_run = ttk.Frame(root); f_run.pack(fill="x", padx=pad["padx"], pady=(0, int(10 * self.sf)))
        self.run_btn = ttk.Button(f_run, text="▶ 加工スタート", style="Big.TButton", command=self.start)
        self.run_btn.pack(side="right")
        st = ttk.Frame(f_run); st.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.status = ttk.Label(st, text="写真をここにドラッグ＆ドロップしてください")
        self.status.pack(anchor="w")
        self.progress = ttk.Progressbar(st, mode="determinate")
        self.progress.pack(fill="x", pady=(4, 0))

        if HAS_DND:
            for w in (root, self.canvas):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

        threading.Thread(target=self.analyze_worker, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        self.redraw()

    # ── 設定 ──
    def on_option(self):
        self.store_config()
        self.redraw()

    def on_auto_rotate(self):
        # オン：判定結果の向きに／オフ：元の向きに戻す（手で回した分はリセット）
        for it in self.items:
            if it.state == "ready":
                it.rotation = it.auto_rotation if self.rot_var.get() else 0
        self.on_option()

    def store_config(self):
        try:
            margin = round(float(self.margin_var.get()))
        except Exception:
            margin = 6
        self.cfg.update(output_dir=self.out_var.get(), auto_rotate=self.rot_var.get(),
                        shadow=self.shadow_var.get(), keep_bg=self.keepbg_var.get(), size=self.size_var.get(),
                        model=self.model_var.get(), margin=margin)
        save_config(self.cfg)

    def pick_output(self):
        d = filedialog.askdirectory(title="保存先フォルダを選んでください", initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)
            self.store_config()

    def open_output(self):
        p = Path(self.out_var.get())
        p.mkdir(parents=True, exist_ok=True)
        open_folder(p)

    def on_close(self):
        self.store_config()
        self.root.destroy()

    # ── 写真の追加 ──
    def pick_files(self):
        fs = filedialog.askopenfilenames(
            title="写真を選んでください（複数選択OK）",
            filetypes=[("画像", " ".join(f"*{e} *{e.upper()}" for e in sorted(core.EXTS))), ("すべて", "*.*")])
        if fs:
            self.add_paths(fs)

    def pick_folder(self):
        d = filedialog.askdirectory(title="写真の入ったフォルダを選んでください")
        if d:
            self.add_paths([d])

    def on_drop(self, event):
        self.add_paths(self.root.tk.splitlist(event.data))
        return event.action

    def add_paths(self, paths):
        if self.processing:
            return
        have = {it.path for it in self.items}
        new = [p for p in expand_paths(paths) if p not in have]
        if not new:
            self.status.config(text="追加できる写真がありませんでした")
            return
        for p in new:
            it = Item(p)
            self.items.append(it)
            self.analyze_q.put(it)
        self.redraw()
        self.update_status()

    def set_all_links(self, on: bool):
        if self.processing:
            return
        for i, it in enumerate(self.items):
            it.link_next = on and i % 2 == 0
        normalize_links(self.items)
        self.redraw()

    def toggle_link(self, it: Item):
        i = self.items.index(it)
        if i + 1 >= len(self.items):
            return
        if it.link_next:
            it.link_next = False
        else:
            # つなぐ2枚が、ほかのペアに入っていたら外す
            if i > 0:
                self.items[i - 1].link_next = False
            self.items[i + 1].link_next = False
            it.link_next = True
        normalize_links(self.items)
        self.redraw()

    def clear_items(self):
        if self.processing:
            return
        self.items.clear()
        self.photos.clear()
        self.redraw()
        self.update_status()

    # ── 下調べ（AIで切り抜き・向き判定・サムネイル） ──
    def get_session(self, name):
        with self.session_lock:
            if name not in self.sessions:
                mb = core.MODELS[name][5]
                self.ui_q.put(("status", "切り抜きAIを準備中…"))
                self.sessions[name] = core.new_session(
                    name, lambda r: self.ui_q.put(("status", f"切り抜きAIをダウンロード中… {r*100:.0f}%（約{mb}MB・初回のみ）")))
            return self.sessions[name]

    def analyze_worker(self):
        while True:
            it = self.analyze_q.get()
            if it not in self.items:
                continue
            try:
                sess = self.get_session(ANALYZE_MODEL)
                info = core.analyze(it.path, sess, auto_rotate=True)
                self.ui_q.put(("analyzed", it, info))
            except Exception as e:
                self.ui_q.put(("analyze_error", it, str(e)))

    # ── 一覧の描画 ──
    def layout(self):
        cw = max(self.canvas.winfo_width(), self.tile_w + 2 * self.gap)
        cols = max(1, (cw - self.gap) // (self.tile_w + self.gap))
        return cols

    def tile_xy(self, i, cols):
        r, c = divmod(i, cols)
        return self.gap + c * (self.tile_w + self.gap), self.gap + r * (self.tile_h + self.gap)

    def photo_for(self, it: Item):
        key = (it.id, it.rotation)
        if key not in self.photos:
            im = core.rotate_img(it.thumb, it.rotation).copy()
            im.thumbnail((self.thumb_box, self.thumb_box), Image.LANCZOS)
            self.photos[key] = ImageTk.PhotoImage(im)
        return self.photos[key]

    def redraw(self):
        cv = self.canvas
        cv.delete("all")
        if not self.items:
            w, h = max(cv.winfo_width(), 300), max(cv.winfo_height(), 200)
            cv.create_text(w / 2, h / 2 - 14 * self.sf, text="ここに写真やフォルダをドラッグ＆ドロップ",
                           font=(self.fam, self.fsize + 4, "bold"), fill="#888")
            cv.create_text(w / 2, h / 2 + 16 * self.sf, text="何枚でもまとめて入れられます（撮影した順に並びます）",
                           font=(self.fam, self.fsize), fill="#aaa")
            cv.configure(scrollregion=(0, 0, w, h))
            self.update_count()
            return
        cols = self.layout()
        n = len(self.items)
        role = {}
        for k, g in enumerate(make_groups(self.items)):
            if len(g) == 2:
                role[g[0].id] = (k, "表")
                role[g[1].id] = (k, "裏")
        pair_no = {}
        for it in self.items:
            if it.id in role and role[it.id][1] == "表":
                pair_no[role[it.id][0]] = len(pair_no) + 1
        # ペアの2枚を帯でつなぐ（同じ行にあるとき）
        for i, it in enumerate(self.items[:-1]):
            if it.link_next:
                x1, y1 = self.tile_xy(i, cols)
                x2, y2 = self.tile_xy(i + 1, cols)
                if y1 == y2:
                    bg = PAIR_COLORS[(pair_no[role[it.id][0]] - 1) % 2]
                    cv.create_rectangle(x1 + self.tile_w - 2, y1 + self.tile_h * 0.25, x2 + 2,
                                        y1 + self.tile_h * 0.75, fill=bg, outline="")
        for i, it in enumerate(self.items):
            x, y = self.tile_xy(i, cols)
            tag = f"t{it.id}"
            tags = ("tile", tag)
            if it.id in role:
                k, side = role[it.id]
                no = pair_no[k]
                bg = PAIR_COLORS[(no - 1) % 2]
                label = f"{i + 1}  ペア{no}・{side}"
            else:
                bg = "#f2f2f2"
                label = f"{i + 1}"
            cv.create_rectangle(x, y, x + self.tile_w, y + self.tile_h, fill=bg, outline="#d0d0d0", tags=tags)
            cv.create_text(x + 8 * self.sf, y + 6 * self.sf, text=label, anchor="nw",
                           font=(self.fam, self.fsize, "bold"), fill="#333", tags=tags)
            cx, cy = x + self.tile_w / 2, y + 26 * self.sf + self.thumb_box / 2
            if it.state == "ready" and it.thumb is not None:
                cv.create_image(cx, cy, image=self.photo_for(it), tags=tags)
            else:
                cv.create_rectangle(cx - self.thumb_box / 2, cy - self.thumb_box / 2,
                                    cx + self.thumb_box / 2, cy + self.thumb_box / 2,
                                    fill="#e8e8e8", outline="", tags=tags)
                cv.create_text(cx, cy, text="確認中…" if it.state == "wait" else "読み込めません",
                               fill="#888" if it.state == "wait" else "#c33", tags=tags)
            # ファイル名
            name = it.path.name if len(it.path.name) <= 20 else it.path.name[:9] + "…" + it.path.name[-9:]
            cv.create_text(cx, y + self.tile_h - 62 * self.sf, text=name, fill="#666",
                           font=(self.fam, self.fsize - 2), tags=tags)
            # 向きの判定結果
            if it.state == "ready" and self.rot_var.get():
                if it.rotation != it.auto_rotation:
                    txt, col = "手動で回転", "#555"
                elif it.confident:
                    txt, col = core_kind_label(it.kind), "#2a7a2a"
                else:
                    txt, col = "⚠ 向きを確認", "#d07000"
                cv.create_text(cx, y + self.tile_h - 42 * self.sf, text=txt, fill=col,
                               font=(self.fam, self.fsize - 1), tags=tags)
            # ボタン（⟲ ⟳ ✕）
            by = y + self.tile_h - 18 * self.sf
            for sym, act, bx, col in (("⟲", "rotL", cx - 34 * self.sf, ACCENT), ("⟳", "rotR", cx, ACCENT),
                                      ("✕", "del", x + self.tile_w - 18 * self.sf, "#c33")):
                cv.create_text(bx, by, text=sym, fill=col, font=(self.fam, self.fsize + 6, "bold"),
                               tags=("btn", act, tag))
        # 写真と写真の間の「つなぐ」ボタン
        for i, it in enumerate(self.items[:-1]):
            x, y = self.tile_xy(i, cols)
            self.draw_link_button(x + self.tile_w + self.gap / 2, y + self.tile_h / 2, it)
        rows = (n + cols - 1) // cols
        cv.configure(scrollregion=(0, 0, cv.winfo_width(), self.gap + rows * (self.tile_h + self.gap)))
        self.update_count()

    def draw_link_button(self, cx, cy, it: Item):
        """鎖のマークの丸ボタン（つながっている＝青、つながっていない＝グレー）"""
        cv, r = self.canvas, 13 * self.sf
        on = it.link_next
        tags = ("btn", "link", f"l{it.id}")
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=ACCENT if on else "white",
                       outline=ACCENT if on else "#aaa", width=max(1, int(1.5 * self.sf)), tags=tags)
        fg, bg = ("white", ACCENT) if on else ("#8a8a8a", "white")
        u = self.sf
        # 斜めの2つの輪（鎖のマーク）：太線の上に背景色の細線を重ねて中抜きにする
        for (x1, y1, x2, y2) in ((-6.5, 6.5, -1.5, 1.5), (1.5, -1.5, 6.5, -6.5)):
            cv.create_line(cx + x1 * u, cy + y1 * u, cx + x2 * u, cy + y2 * u, fill=fg,
                           width=7 * u, capstyle="round", tags=tags)
            cv.create_line(cx + x1 * u, cy + y1 * u, cx + x2 * u, cy + y2 * u, fill=bg,
                           width=3 * u, capstyle="round", tags=tags)
        cv.create_line(cx - 2.5 * u, cy + 2.5 * u, cx + 2.5 * u, cy - 2.5 * u, fill=fg,
                       width=2 * u, capstyle="round", tags=tags)
        if not on:  # つながっていない＝斜線で切る
            cv.create_line(cx - 7 * u, cy - 7 * u, cx + 7 * u, cy + 7 * u, fill="white", width=4 * u, tags=tags)
            cv.create_line(cx - 7 * u, cy - 7 * u, cx + 7 * u, cy + 7 * u, fill="#8a8a8a", width=1.5 * u, tags=tags)

    def update_count(self):
        n = len(self.items)
        if not n:
            self.count_lbl.config(text="")
            return
        groups = make_groups(self.items)
        pairs = sum(1 for g in groups if len(g) == 2)
        self.count_lbl.config(text=f"{n}枚（ペア{pairs}組） → 出来上がり {len(groups)}枚" if pairs else f"{n}枚")

    def update_status(self):
        if self.processing:
            return
        waiting = sum(1 for it in self.items if it.state == "wait")
        if waiting:
            done = len(self.items) - waiting
            self.status.config(text=f"写真を確認中… {done}/{len(self.items)}")
            self.progress.config(maximum=len(self.items), value=done)
            self.run_btn.config(state="disabled")
        else:
            unsure = sum(1 for it in self.items if it.state == "ready" and not it.confident) if self.rot_var.get() else 0
            msg = "準備OK。順番と向きを確認して「加工スタート」を押してください" if self.items else \
                "写真をここにドラッグ＆ドロップしてください"
            if unsure:
                msg += f"（向き要確認：{unsure}枚）"
            self.status.config(text=msg)
            self.progress.config(value=0)
            self.run_btn.config(state="normal")

    # ── マウス操作（クリック・ドラッグで並び替え） ──
    def item_by_tag(self, tags):
        for t in tags:
            if t.startswith("t") and t[1:].isdigit():
                tid = int(t[1:])
                for it in self.items:
                    if it.id == tid:
                        return it
        return None

    def on_press(self, e):
        if self.processing:
            return
        cur = self.canvas.find_withtag("current")
        if not cur:
            return
        tags = self.canvas.gettags(cur[0])
        if "link" in tags:
            lid = next(int(t[1:]) for t in tags if t.startswith("l") and t[1:].isdigit())
            it = next((x for x in self.items if x.id == lid), None)
            if it is not None:
                self.toggle_link(it)
            return
        it = self.item_by_tag(tags)
        if it is None:
            return
        if "btn" in tags:
            if "del" in tags:
                i = self.items.index(it)
                if i > 0:
                    self.items[i - 1].link_next = False
                self.items.remove(it)
                normalize_links(self.items)
            elif "rotL" in tags:
                it.rotation = (it.rotation - 90) % 360
            elif "rotR" in tags:
                it.rotation = (it.rotation + 90) % 360
            self.redraw(); self.update_status()
            return
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        self.drag = {"item": it, "x": x, "y": y, "moved": False, "target": None}
        self.canvas.tag_raise(f"t{it.id}")

    def on_motion(self, e):
        d = self.drag
        if not d:
            return
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        self.canvas.move(f"t{d['item'].id}", x - d["x"], y - d["y"])
        d["x"], d["y"], d["moved"] = x, y, True
        # 画面の端に来たら自動スクロール
        if e.y < 20 * self.sf:
            self.canvas.yview_scroll(-1, "units")
        elif e.y > self.canvas.winfo_height() - 20 * self.sf:
            self.canvas.yview_scroll(1, "units")
        # 挿入位置の目印
        self.canvas.delete("marker")
        idx = self.drop_index(x, y)
        d["target"] = idx
        cols = self.layout()
        if idx < len(self.items):
            mx, my = self.tile_xy(idx, cols)
        else:
            mx, my = self.tile_xy(len(self.items) - 1, cols)
            mx += self.tile_w + self.gap
        mx -= self.gap / 2
        self.canvas.create_line(mx, my, mx, my + self.tile_h, fill=ACCENT, width=int(4 * self.sf), tags="marker")

    def drop_index(self, x, y):
        cols = self.layout()
        unit_w, unit_h = self.tile_w + self.gap, self.tile_h + self.gap
        col = int(max(0, x - self.gap / 2) // unit_w)
        if (max(0, x - self.gap / 2) - col * unit_w) > self.tile_w / 2:
            col += 1
        col = min(col, cols)
        row = int(max(0, y - self.gap / 2) // unit_h)
        return max(0, min(len(self.items), row * cols + col))

    def on_release(self, e):
        d, self.drag = self.drag, None
        self.canvas.delete("marker")
        if not d or not d["moved"] or d["target"] is None:
            self.redraw()
            return
        it = d["item"]
        old = self.items.index(it)
        new = d["target"]
        if new in (old, old + 1):
            self.redraw()
            return
        # 動かした写真のペアは解除する
        if old > 0:
            self.items[old - 1].link_next = False
        it.link_next = False
        self.items.pop(old)
        if new > old:
            new -= 1
        self.items.insert(new, it)
        if new > 0:  # 割り込んだ先のペアも解除
            self.items[new - 1].link_next = False
        normalize_links(self.items)
        self.redraw()

    def on_wheel(self, e):
        delta = e.delta if sys.platform == "darwin" else e.delta / 120
        self.canvas.yview_scroll(int(-delta * (1 if sys.platform == "darwin" else 3)), "units")

    # ── 加工 ──
    def start(self):
        if self.processing or not self.items:
            if not self.items:
                messagebox.showinfo(APP_TITLE, "先に写真を入れてください")
            return
        try:
            size = int(self.size_var.get())
            assert 200 <= size <= 5000
        except Exception:
            messagebox.showwarning(APP_TITLE, "サイズは200〜5000の数字で入力してください")
            return
        self.store_config()
        args = Namespace(size=size, ratio="1:1", margin=round(float(self.margin_var.get())) / 100, quality=92,
                         keep_bg=self.keepbg_var.get(), shadow=self.shadow_var.get(), pair=False,
                         gap=0.04, no_refine=False, model=MODELS[self.model_var.get()])
        normalize_links(self.items)
        groups = [g for g in make_groups(self.items) if all(it.state == "ready" for it in g)]
        out_dir = Path(self.out_var.get())
        self.processing = True
        self.run_btn.config(state="disabled")
        threading.Thread(target=self.process_worker, args=(groups, out_dir, args), daemon=True).start()

    def process_worker(self, groups, out_dir: Path, args):
        q = self.ui_q
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            session = None if args.keep_bg else self.get_session(args.model)
            reuse = args.model == ANALYZE_MODEL  # 下調べのAI結果を使い回せる
            q.put(("total", len(groups)))
            ok = ng = 0
            for n, g in enumerate(groups, 1):
                names = " + ".join(it.path.name for it in g)
                q.put(("status", f"加工中 {n}/{len(groups)}：{names}"))
                try:
                    parts = [core.extract_subject(it.path, args, session, it.rotation, it.raw if reuse else None)
                             for it in g]
                    stem = "+".join(it.path.stem for it in g)
                    core.save(core.compose(parts, args), out_dir, stem, args, overwrite=False)
                    ok += 1
                except Exception as e:
                    ng += 1
                    q.put(("log", f"{names}：{e}"))
                q.put(("step", n))
            q.put(("done", ok, ng, out_dir))
        except Exception as e:
            q.put(("error", f"{e}\n\n{traceback.format_exc()}"))

    def poll(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                kind = msg[0]
                if kind == "analyzed":
                    _, it, info = msg
                    it.state, it.raw, it.thumb = "ready", info["raw"], info["thumb"]
                    it.kind, it.confident = info["kind"], info["confident"]
                    it.auto_rotation = info["rotation"]
                    it.rotation = info["rotation"] if self.rot_var.get() else 0
                    if it in self.items:
                        self.redraw(); self.update_status()
                elif kind == "analyze_error":
                    _, it, err = msg
                    it.state, it.error = "error", err
                    if it in self.items:
                        self.redraw(); self.update_status()
                elif kind == "status":
                    self.status.config(text=msg[1])
                elif kind == "total":
                    self.progress.config(maximum=msg[1], value=0)
                elif kind == "step":
                    self.progress.config(value=msg[1])
                elif kind == "log":
                    self.errors = getattr(self, "errors", []) + [msg[1]]
                elif kind == "done":
                    _, ok, ng, out_dir = msg
                    self.processing = False
                    self.run_btn.config(state="normal")
                    self.status.config(text=f"完了：{ok}枚 できました" + (f"（{ng}件 失敗）" if ng else ""))
                    if ng:
                        messagebox.showwarning(APP_TITLE, "うまく加工できなかった写真があります：\n\n" +
                                               "\n".join(getattr(self, "errors", [])[-10:]))
                    self.errors = []
                    open_folder(out_dir)
                elif kind == "error":
                    self.processing = False
                    self.run_btn.config(state="normal")
                    self.status.config(text="エラーが発生しました")
                    messagebox.showerror(APP_TITLE, msg[1].split("\n")[0])
        except queue.Empty:
            pass
        self.root.after(80, self.poll)


def core_kind_label(kind):
    import orientation
    return f"{orientation.KIND_LABEL.get(kind, '?')}・向きOK"


def selftest(src: str, dst: str) -> int:
    """ビルド確認用：画面を出さずに、下調べ→向き補正→ペア加工まで行い selftest.log に書く"""
    out = Path(dst); out.mkdir(parents=True, exist_ok=True)
    log = out / "selftest.log"
    try:
        args = Namespace(size=1200, ratio="1:1", margin=0.06, quality=92, keep_bg=False, shadow=True,
                         pair=True, gap=0.04, no_refine=False, model=ANALYZE_MODEL)
        files = expand_paths([src])
        session = core.new_session(ANALYZE_MODEL)
        lines = []
        infos = [(f, core.analyze(f, session)) for f in files]
        for f, inf in infos:
            lines.append(f"{f.name}: rotation={inf['rotation']} kind={inf['kind']} confident={inf['confident']}")
        for i in range(0, len(infos), 2):
            g = infos[i:i + 2]
            parts = [core.extract_subject(f, args, session, inf["rotation"], inf["raw"]) for f, inf in g]
            lines.append(core.save(core.compose(parts, args), out, "+".join(f.stem for f, _ in g), args).name)
        # 画面部品も組み立てられるか確認
        enable_sharp_text()
        root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        root.withdraw()
        App(root)
        root.update()
        root.destroy()
        log.write_text("OK\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return 0
    except Exception:
        log.write_text("NG\n" + traceback.format_exc(), encoding="utf-8")
        return 1


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
    enable_sharp_text()
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    try:
        icon = HERE / "assets" / "icon.png"
        if icon.exists():
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
    except Exception:
        pass
    try:
        ttk.Style().theme_use("aqua" if platform.system() == "Darwin" else
                              "vista" if platform.system() == "Windows" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
