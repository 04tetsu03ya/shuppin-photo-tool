"""
自動更新：GitHub の最新版を確認し、新しければダウンロードして入れ替える。

  ・Windows：インストーラーを静かに実行（終わると自動でアプリが再起動）
  ・Mac    ：.dmg を開いてアプリを入れ替え、再起動
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = "04tetsu03ya/shuppin-photo-tool"
BASE = f"https://github.com/{REPO}/releases/download/latest"
UA = {"User-Agent": "ShuppinPhoto-Updater"}


def _ctx():
    import shuppin_photo
    return shuppin_photo._ssl_context()


def parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except Exception:
        return (0,)


def latest_version(timeout: float = 10) -> str | None:
    """公開されている最新版の番号（取れなければ None）"""
    try:
        req = urllib.request.Request(f"{BASE}/version.txt", headers=UA)
        with urllib.request.urlopen(req, context=_ctx(), timeout=timeout) as r:
            return r.read(64).decode("utf-8").strip() or None
    except Exception:
        return None


def is_newer(latest: str | None, current: str) -> bool:
    return bool(latest) and parse_version(latest) > parse_version(current)


def asset_name() -> str | None:
    if sys.platform.startswith("win"):
        return "ShuppinPhoto-Setup-Windows.exe"
    if sys.platform == "darwin":
        arm = platform.machine().lower() in ("arm64", "aarch64")
        return f"ShuppinPhoto-Mac-{'AppleSilicon' if arm else 'Intel'}.dmg"
    return None


def can_self_update() -> bool:
    """アプリ版（Windows / Mac）として動いているときだけ自動で入れ替えられる"""
    return bool(getattr(sys, "frozen", False)) and asset_name() is not None


def download(progress=None) -> Path:
    name = asset_name()
    if not name:
        raise RuntimeError("このパソコンでは自動更新できません")
    dst = Path(tempfile.gettempdir()) / name
    req = urllib.request.Request(f"{BASE}/{name}", headers=UA)
    with urllib.request.urlopen(req, context=_ctx(), timeout=60) as r, open(dst, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    return dst


def _mac_app_bundle() -> Path | None:
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def install_and_restart(file: Path):
    """ダウンロードした更新を入れて、アプリを再起動する（この関数のあと、アプリは終了すること）"""
    if sys.platform.startswith("win"):
        # 静かにインストール → インストーラーが終わったら新しいアプリを起動する
        subprocess.Popen([str(file), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         close_fds=True)
        return
    if sys.platform == "darwin":
        app = _mac_app_bundle()
        if app is None or not os.access(app.parent, os.W_OK):
            subprocess.Popen(["open", str(file)])  # 入れ替えられない場合は dmg を開いて手動で
            return
        script = Path(tempfile.gettempdir()) / "shuppin_update.sh"
        mnt = Path(tempfile.gettempdir()) / "shuppin_update_mnt"
        script.write_text(f'''#!/bin/bash
# アプリが終わるのを待ってから入れ替える
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.5; done
mkdir -p "{mnt}"
hdiutil attach -nobrowse -quiet -mountpoint "{mnt}" "{file}" || {{ open "{file}"; exit 1; }}
NEW=$(ls -d "{mnt}"/*.app | head -n1)
rm -rf "{app}.old"
mv "{app}" "{app}.old" && ditto "$NEW" "{app}" && rm -rf "{app}.old" || {{ rm -rf "{app}"; mv "{app}.old" "{app}"; }}
xattr -dr com.apple.quarantine "{app}" 2>/dev/null
hdiutil detach -quiet "{mnt}"
open "{app}"
''', encoding="utf-8")
        script.chmod(0o755)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise RuntimeError("このパソコンでは自動更新できません")
