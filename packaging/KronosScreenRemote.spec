# PyInstaller spec. Build from a LOCAL checkout (not over SMB):
#   pyinstaller packaging/KronosScreenRemote.spec --noconfirm
# Paths resolve relative to the repo root, one level above this file.
import os, sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# Code locates Resources/ via Path(__file__).parent[.parent]; PyInstaller keeps
# that layout under the bundle root, so Resources/ ships at the same relative path.
datas = [(os.path.join(ROOT, "Resources"), "Resources")]

# The PySide6 meta-package drags in WebEngine/Quick/3D/etc.; the app uses only
# QtCore/QtGui/QtWidgets/QtMultimedia.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtSensors", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtVirtualKeyboard", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtStateMachine", "PySide6.QtHttpServer",
    "tkinter", "pyaudio",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    datas=datas,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = os.path.join(ROOT, "Resources", "Icons",
                    "AppIcon.ico" if sys.platform == "win32" else "AppIcon.png")

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="KronosScreenRemote",
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="KronosScreenRemote")
