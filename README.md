# 出品写真ツール

ヤフオク・メルカリ・ラクマ用に、撮影した写真の背景を真っ白にして正方形にそろえるアプリです。
Windows / Mac で、Python などを入れずにアイコンから起動できます。

## ダウンロード
右側の **Releases** → 最新版から、使うパソコンに合うファイルをダウンロードします。

| パソコン | ファイル |
|---|---|
| Windows | `ShuppinPhoto-Setup-Windows.exe` |
| Mac（M1〜M4 など Apple シリコン） | `ShuppinPhoto-Mac-AppleSilicon.dmg` |
| Mac（Intel） | `ShuppinPhoto-Mac-Intel.dmg` |

### Windows
1. `ShuppinPhoto-Setup-Windows.exe` を開く
2. 「WindowsによってPCが保護されました」と出たら **詳細情報 → 実行**
3. インストールが終わると、デスクトップとスタートメニューに「出品写真ツール」ができます

### Mac
1. `.dmg` を開き、「出品写真ツール」を **Applications** フォルダへドラッグ
2. 初回は「開けません」と出るので、**システム設定 → プライバシーとセキュリティ** の下の方にある **「このまま開く」** を押す
3. 以降は Launchpad や Dock からクリックで起動できます

## 使い方
1. 写真フォルダを「選ぶ…」で選ぶ（ウィンドウにドラッグ＆ドロップでもOK）
2. 長いパンツなどは「表・裏の2枚を横に並べて1枚にする」にチェック
   - ファイル名が `○○_表` / `○○_裏` ならその組み合わせ、それ以外は名前順に2枚ずつペア
3. 「加工スタート」→ 終わると保存先（`元のフォルダ名_output`）が自動で開きます

## 開発者向け
- `shuppin_photo.py` … 加工処理本体（コマンドラインでも使用可）
- `shuppin_app.py` … 画面
- GitHub Actions（`.github/workflows/build.yml`）で main に push するたびに Windows / Mac 版を自動ビルドし、Releases の「最新版」に置きます
