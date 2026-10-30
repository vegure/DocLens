# PyInstaller spec — DocLens 后端单文件
block_cipher = None

a = Analysis(
    ['../work/server.py'],
    pathex=['../work'],
    binaries=[],
    datas=[],
    hiddenimports=['scan', 'PIL'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'pytest'],
    cipher=block_cipher)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          name='server',
          debug=False,
          strip=False,
          upx=False,
          console=True,
          runtime_tmpdir=None)
