#!/usr/bin/env python3
"""
出品写真ツール（Windows / Mac 共通アプリ）

写真フォルダを選んで「加工スタート」を押すだけで、
背景を白くして正方形にそろえた出品用写真を作ります。
処理の中身は同じフォルダの shuppin_photo.py を使います。
"""

from __future__ import annotations

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
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))


def user_data_dir() -> Path:
    """切り抜きAIモデルなどを置く、ユーザーが書き込めるフォルダ"""
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

# モデルの置き場所：アプリ同梱 → ユーザーフォルダ（ダウンロード先）
os.environ.setdefault("SHUPPIN_MODEL_HOME", str(user_data_dir() / "models"))
core.BUNDLED_MODEL_DIRS.append(HERE / "models")

# ドラッグ＆ドロップ（入っていれば使う。無くても動く）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    HAS_DND = False

APP_TITLE = "出品写真ツール"
MODELS = {
    "標準（速い）": "isnet-general-use",
    "高精度（遅い）": "birefnet-general",
}
SIZES = ["1200", "1080", "1500", "1000", "800"]


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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.sessions = {}
        self.running = False
        self.last_output: Path | None = None

        root.title(APP_TITLE)
        root.minsize(560, 560)
        pad = {"padx": 12, "pady": 6}

        # ── フォルダ ──
        frm_in = ttk.LabelFrame(root, text="① 写真フォルダ")
        frm_in.pack(fill="x", **pad)
        self.in_var = tk.StringVar()
        self.in_var.trace_add("write", lambda *_: self._sync_output())
        ttk.Entry(frm_in, textvariable=self.in_var).pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ttk.Button(frm_in, text="選ぶ…", command=self.pick_input).pack(side="left", padx=(0, 8))

        hint = "ここにフォルダをドラッグ＆ドロップしてもOK" if HAS_DND else "「選ぶ…」から写真の入ったフォルダを選んでください"
        ttk.Label(root, text=hint, foreground="#666").pack(anchor="w", padx=16)

        frm_out = ttk.LabelFrame(root, text="② 保存先（自動で作られます）")
        frm_out.pack(fill="x", **pad)
        self.out_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=8, pady=8)
        ttk.Button(frm_out, text="変更…", command=self.pick_output).pack(side="left", padx=(0, 8))

        # ── オプション ──
        frm_opt = ttk.LabelFrame(root, text="③ 設定")
        frm_opt.pack(fill="x", **pad)

        self.pair_var = tk.BooleanVar(value=False)
        self.shadow_var = tk.BooleanVar(value=True)
        self.keepbg_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm_opt, text="表・裏の2枚を横に並べて1枚にする（長いパンツなど）",
                        variable=self.pair_var).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=3)
        ttk.Checkbutton(frm_opt, text="うっすら影をつける", variable=self.shadow_var
                        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=3)
        ttk.Checkbutton(frm_opt, text="背景は消さない（切り出し・サイズ調整だけ）", variable=self.keepbg_var
                        ).grid(row=1, column=2, columnspan=2, sticky="w", padx=8, pady=3)

        ttk.Label(frm_opt, text="サイズ(px)").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        self.size_var = tk.StringVar(value="1200")
        ttk.Combobox(frm_opt, textvariable=self.size_var, values=SIZES, width=8
                     ).grid(row=2, column=1, sticky="w")

        ttk.Label(frm_opt, text="切り抜き精度").grid(row=2, column=2, sticky="w", padx=8)
        self.model_var = tk.StringVar(value=list(MODELS)[0])
        ttk.Combobox(frm_opt, textvariable=self.model_var, values=list(MODELS), state="readonly", width=14
                     ).grid(row=2, column=3, sticky="w")

        ttk.Label(frm_opt, text="余白").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        self.margin_var = tk.DoubleVar(value=6)
        self.margin_lbl = ttk.Label(frm_opt, text="6%")
        ttk.Scale(frm_opt, from_=0, to=20, variable=self.margin_var, length=200,
                  command=lambda v: self.margin_lbl.config(text=f"{float(v):.0f}%")
                  ).grid(row=3, column=1, columnspan=2, sticky="w")
        self.margin_lbl.grid(row=3, column=3, sticky="w")

        # ── 実行 ──
        frm_run = ttk.Frame(root)
        frm_run.pack(fill="x", **pad)
        self.run_btn = ttk.Button(frm_run, text="▶ 加工スタート", command=self.start)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(frm_run, text="保存先を開く", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", padx=12)
        self.status = ttk.Label(root, text="待機中")
        self.status.pack(anchor="w", padx=12, pady=(4, 0))

        self.log = tk.Text(root, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        if HAS_DND:
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self.on_drop)

        root.after(100, self.poll)

    # ── 入出力 ──
    def _sync_output(self):
        p = self.in_var.get().strip()
        if p:
            src = Path(p)
            base = src if src.is_dir() else src.parent / src.stem
            self.out_var.set(str(base.parent / (base.name + "_output")))

    def pick_input(self):
        d = filedialog.askdirectory(title="写真の入ったフォルダを選んでください")
        if d:
            self.in_var.set(d)

    def pick_output(self):
        d = filedialog.askdirectory(title="保存先フォルダを選んでください")
        if d:
            self.out_var.set(d)

    def on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        if paths:
            self.in_var.set(paths[0])

    def open_output(self):
        if self.last_output:
            open_folder(self.last_output)

    def write_log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ── 実行 ──
    def start(self):
        if self.running:
            return
        src = Path(self.in_var.get().strip())
        if not src.exists():
            messagebox.showwarning(APP_TITLE, "写真フォルダを選んでください")
            return
        try:
            size = int(self.size_var.get())
            assert 200 <= size <= 5000
        except Exception:
            messagebox.showwarning(APP_TITLE, "サイズは200〜5000の数字で入力してください")
            return

        args = Namespace(
            size=size, ratio="1:1", margin=round(self.margin_var.get()) / 100,
            quality=92, keep_bg=self.keepbg_var.get(), shadow=self.shadow_var.get(),
            pair=self.pair_var.get(), gap=0.04, no_refine=False,
            model=MODELS[self.model_var.get()],
        )
        out_dir = Path(self.out_var.get().strip() or (str(src) + "_output"))

        self.running = True
        self.run_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.log.config(state="normal"); self.log.delete("1.0", "end"); self.log.config(state="disabled")
        threading.Thread(target=self.worker, args=(src, out_dir, args), daemon=True).start()

    def worker(self, src: Path, out_dir: Path, args: Namespace):
        q = self.q
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            files = [src] if src.is_file() else sorted(
                f for f in src.iterdir() if f.suffix.lower() in core.EXTS and not f.name.startswith("."))
            if not files:
                q.put(("error", "フォルダに画像が見つかりませんでした"))
                return

            session = None
            if not args.keep_bg:
                if args.model not in self.sessions:
                    q.put(("status", "切り抜きAIを準備中…"))
                    size_mb = core.MODELS[args.model][5]

                    def prog(r):
                        q.put(("status", f"切り抜きAIをダウンロード中… {r*100:.0f}%（約{size_mb}MB・初回のみ）"))
                    self.sessions[args.model] = core.new_session(args.model, prog)
                session = self.sessions[args.model]

            groups = core.make_groups(files, args.pair)
            q.put(("total", len(groups)))
            ok, ng = 0, 0
            for i, (group, stem) in enumerate(groups, 1):
                names = " + ".join(f.name for f in group)
                q.put(("status", f"加工中 {i}/{len(groups)}：{names}"))
                try:
                    items = [core.extract_subject(f, args, session) for f in group]
                    out = core.save(core.compose(items, args), out_dir, stem, args)
                    ok += 1
                    q.put(("log", f"✓ {names} → {out.name}"))
                except Exception as e:
                    ng += 1
                    q.put(("log", f"✗ {names}：{e}"))
                q.put(("step", i))
            q.put(("done", ok, ng, out_dir))
        except Exception as e:
            q.put(("error", f"{e}\n\n{traceback.format_exc()}"))

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status.config(text=msg[1])
                elif kind == "total":
                    self.progress.config(maximum=msg[1], value=0)
                elif kind == "step":
                    self.progress.config(value=msg[1])
                elif kind == "log":
                    self.write_log(msg[1])
                elif kind == "done":
                    _, ok, ng, out_dir = msg
                    self.last_output = out_dir
                    self.status.config(text=f"完了：{ok}枚 成功 / {ng}枚 失敗")
                    self.write_log(f"\n保存先：{out_dir}")
                    self.finish()
                    self.open_btn.config(state="normal")
                    open_folder(out_dir)
                elif kind == "error":
                    self.status.config(text="エラーが発生しました")
                    self.write_log(msg[1])
                    messagebox.showerror(APP_TITLE, msg[1].split("\n")[0])
                    self.finish()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finish(self):
        self.running = False
        self.run_btn.config(state="normal")


def selftest(src: str, dst: str) -> int:
    """ビルド確認用：画面を出さずに加工して結果を selftest.log に書く"""
    out = Path(dst); out.mkdir(parents=True, exist_ok=True)
    log = out / "selftest.log"
    try:
        args = Namespace(size=1200, ratio="1:1", margin=0.06, quality=92, keep_bg=False, shadow=True,
                         pair=True, gap=0.04, no_refine=False, model="isnet-general-use")
        files = sorted(f for f in Path(src).iterdir() if f.suffix.lower() in core.EXTS)
        session = core.new_session(args.model)
        lines = []
        for group, stem in core.make_groups(files, True):
            items = [core.extract_subject(f, args, session) for f in group]
            lines.append(core.save(core.compose(items, args), out, stem, args).name)
        log.write_text("OK\n" + "\n".join(lines) + "\n", encoding="utf-8")
        import tkinter  # noqa: F401  GUI部品が入っているかも確認
        if HAS_DND:
            r = TkinterDnD.Tk(); r.withdraw(); r.destroy()
        return 0
    except Exception:
        log.write_text("NG\n" + traceback.format_exc(), encoding="utf-8")
        return 1


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
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
