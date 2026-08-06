# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the IT Asset System desktop app.
# Build:  .venv\Scripts\python.exe -m PyInstaller asset_system.spec

block_cipher = None

a = Analysis(
    ['desktop_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[
        'app',
        'ai.blueprint',
        'ai.detector',
        'ai.reporter',
        'ai.groq_client',
        'ai.scheduler',
        'backup_db',
        'flask',
        'flask_cors',
        'flask_wtf',
        'flask_login',
        'flask_pymongo',
        'dotenv',
        'bcrypt',
        'email_validator',
        'qrcode',
        'reportlab',
        'openpyxl',
        'pandas',
        'openai',
        'apscheduler',
        'webview',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AssetSystem',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AssetSystem',
)