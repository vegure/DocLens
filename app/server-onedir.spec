# PyInstaller spec — DocLens 后端 onedir 模式
# 公证(notorization)友好: 无运行时自解压, 每个 .so 都可被 codesign 覆盖签名。
# 产物: dist/server-onedir/server(启动器) + _internal/(依赖), 整目录拷入 App。
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
          [],
          exclude_binaries=True,      # onedir: 二进制留在 _internal
          name='server',
          debug=False,
          strip=False,
          upx=False,
          console=True,
          runtime_tmpdir=None)
coll = COLLECT(exe,
               a.binaries,
               a.zipfiles,
               a.datas,
               strip=False,
               upx=False,
               name='server-onedir')
