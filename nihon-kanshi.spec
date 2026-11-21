# PyInstaller の設定。 pyinstaller nihon-kanshi.spec で dist/nihon-kanshi.exe を作る
# 初回起動時に config.yaml・source/ を exe の隣に書き出す (config.py)。web/ は同梱分を使う。data/ は exe の隣に置く
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["main.py"],
    datas=[("config.yaml", "."), ("source", "source"), ("web", "web")],
    hiddenimports=collect_submodules("uvicorn"),
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name="nihon-kanshi",
    console=True,  # ログと Ctrl+C での終了のためにコンソールを出す
    upx=False,
)
