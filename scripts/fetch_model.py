"""ビルド前に、アプリへ同梱する切り抜きAIモデルをダウンロードする"""
import hashlib
import urllib.request
from pathlib import Path

URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"
MD5 = "fc16ebd8b0c10d971d3513d564d01e29"
dst = Path(__file__).resolve().parent.parent / "models" / "isnet-general-use.onnx"
dst.parent.mkdir(exist_ok=True)

if not dst.exists() or hashlib.md5(dst.read_bytes()).hexdigest() != MD5:
    print("downloading model...")
    urllib.request.urlretrieve(URL, dst)

digest = hashlib.md5(dst.read_bytes()).hexdigest()
assert digest == MD5, f"checksum mismatch: {digest}"
print("model ok:", dst, dst.stat().st_size // 1_000_000, "MB")
