"""Всё, что зависит от Windows: устройства по умолчанию, горячие клавиши, окно программы, автозапуск."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import threading
import uuid
from ctypes import POINTER, Structure, byref, c_long, c_ubyte, c_uint32, c_ushort, c_void_p, c_wchar_p
from typing import Callable, Optional

log = logging.getLogger("golos")

IS_WINDOWS = os.name == "nt"


class _GUID(Structure):
    _fields_ = [("a", c_uint32), ("b", c_ushort), ("c", c_ushort), ("d", c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PropertyKey(Structure):
    _fields_ = [("fmtid", _GUID), ("pid", c_uint32)]


class _PropVariant(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("value", c_void_p), ("pad", c_void_p)]


_CLSID_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_CLSID_POLICY = "870AF99C-171D-4F9E-AF0D-E63DF40C2BC9"
_IID_POLICY = "F8679F50-850A-41CF-9C72-430F290290C8"
_FRIENDLY_NAME_FMTID = "A45C254E-DF1C-4EFD-8020-67D146A850E0"
_FRIENDLY_NAME_PID = 14
_E_RENDER = 0
_E_CAPTURE = 1
_DEVICE_ACTIVE = 1
_ROLES = (0, 1, 2)  # обычные программы, мультимедиа, связь
_VT_LPWSTR = 31


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what}: 0x{hr & 0xFFFFFFFF:08X}")


def _method(obj: int, index: int, argtypes: tuple, *args) -> int:
    table = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    function = ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(table[index])
    return function(obj, *args)


def _release(obj: Optional[int]) -> None:
    if obj:
        _method(obj, 2, ())


class _Com:
    def __enter__(self):
        self.ole = ctypes.WinDLL("ole32")
        self.ole.CoInitializeEx.argtypes = [c_void_p, c_uint32]
        self.ole.CoInitializeEx.restype = c_long
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = [POINTER(_GUID), c_void_p, c_uint32, POINTER(_GUID), POINTER(c_void_p)]
        self.ole.CoCreateInstance.restype = c_long
        self.ole.CoTaskMemFree.argtypes = [c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.ole.PropVariantClear.argtypes = [POINTER(_PropVariant)]
        self.ole.PropVariantClear.restype = c_long
        self.ready = self.ole.CoInitializeEx(None, 2) >= 0
        return self

    def __exit__(self, *_exc):
        if self.ready:
            self.ole.CoUninitialize()

    def create(self, clsid: str, iid: str) -> int:
        result = c_void_p()
        class_id, interface_id = _guid(clsid), _guid(iid)
        _check(self.ole.CoCreateInstance(byref(class_id), None, 23, byref(interface_id), byref(result)), "CoCreateInstance")
        return int(result.value or 0)

    def device_id(self, device: int) -> str:
        pointer = c_void_p()
        _check(_method(device, 5, (POINTER(c_void_p),), byref(pointer)), "GetId")
        text = ctypes.wstring_at(int(pointer.value or 0))
        self.ole.CoTaskMemFree(pointer)
        return text

    def device_name(self, device: int) -> str:
        store = c_void_p()
        _check(_method(device, 4, (c_uint32, POINTER(c_void_p)), 0, byref(store)), "OpenPropertyStore")
        store_ptr = int(store.value or 0)
        try:
            key = _PropertyKey(_guid(_FRIENDLY_NAME_FMTID), _FRIENDLY_NAME_PID)
            value = _PropVariant()
            _check(_method(store_ptr, 5, (POINTER(_PropertyKey), POINTER(_PropVariant)), byref(key), byref(value)), "GetValue")
            try:
                if value.vt == _VT_LPWSTR and value.value:
                    return ctypes.wstring_at(value.value)
                return ""
            finally:
                self.ole.PropVariantClear(byref(value))
        finally:
            _release(store_ptr)


def endpoints(flow: int) -> list[tuple[str, str]]:
    """Список (id, имя) всех включённых устройств: flow 0 — вывод звука, 1 — микрофоны."""
    result = []
    with _Com() as com:
        enumerator = com.create(_CLSID_ENUMERATOR, _IID_ENUMERATOR)
        try:
            collection = c_void_p()
            _check(_method(enumerator, 3, (c_uint32, c_uint32, POINTER(c_void_p)), flow, _DEVICE_ACTIVE, byref(collection)), "EnumAudioEndpoints")
            collection_ptr = int(collection.value or 0)
            try:
                count = c_uint32()
                _check(_method(collection_ptr, 3, (POINTER(c_uint32),), byref(count)), "GetCount")
                for index in range(count.value):
                    device = c_void_p()
                    _check(_method(collection_ptr, 4, (c_uint32, POINTER(c_void_p)), index, byref(device)), "Item")
                    device_ptr = int(device.value or 0)
                    try:
                        result.append((com.device_id(device_ptr), com.device_name(device_ptr)))
                    finally:
                        _release(device_ptr)
            finally:
                _release(collection_ptr)
        finally:
            _release(enumerator)
    return result


def capture_devices() -> list[tuple[str, str]]:
    return endpoints(_E_CAPTURE)


def render_devices() -> list[tuple[str, str]]:
    return endpoints(_E_RENDER)


def default_ids(flow: int) -> dict[str, str]:
    """Какие устройства сейчас основные в Windows, по каждой роли."""
    result: dict[str, str] = {}
    with _Com() as com:
        enumerator = com.create(_CLSID_ENUMERATOR, _IID_ENUMERATOR)
        try:
            for role in _ROLES:
                device = c_void_p()
                hr = _method(enumerator, 4, (c_uint32, c_uint32, POINTER(c_void_p)), flow, role, byref(device))
                if hr < 0:
                    continue
                device_ptr = int(device.value or 0)
                try:
                    result[str(role)] = com.device_id(device_ptr)
                finally:
                    _release(device_ptr)
        finally:
            _release(enumerator)
    return result


def default_capture_ids() -> dict[str, str]:
    return default_ids(_E_CAPTURE)


def default_render_ids() -> dict[str, str]:
    return default_ids(_E_RENDER)


def set_default(device_id: str, roles=_ROLES) -> None:
    with _Com() as com:
        policy = com.create(_CLSID_POLICY, _IID_POLICY)
        try:
            for role in roles:
                _check(_method(policy, 13, (c_wchar_p, c_uint32), device_id, role), "SetDefaultEndpoint")
        finally:
            _release(policy)


def set_default_capture(device_id: str, roles=_ROLES) -> None:
    set_default(device_id, roles)


def is_cable_name(name: str) -> bool:
    """Любое устройство VB-CABLE: CABLE Input, CABLE Output, CABLE In 16ch."""
    return "cable" in name.lower()


def find_cable_capture() -> Optional[str]:
    for device_id, name in capture_devices():
        low = name.lower()
        if "cable" in low and "output" in low:
            return device_id
    return None


def find_cable_render() -> Optional[str]:
    for device_id, name in render_devices():
        low = name.lower()
        if "cable" in low and "input" in low:
            return device_id
    return None


def device_name(device_id: str, flow: int) -> Optional[str]:
    for found, name in endpoints(flow):
        if found == device_id:
            return name
    return None


def restore_render(saved: dict[str, str]) -> bool:
    """Если основными колонками стал VB-CABLE, вернуть прежние. Возвращает True, если что-то поменяли."""
    devices = dict(render_devices())
    current = default_render_ids()
    real = [d for d in devices if not is_cable_name(devices[d])]
    changed = False
    for role, device_id in current.items():
        if device_id in devices and not is_cable_name(devices[device_id]):
            continue
        wanted = saved.get(role)
        if not wanted or wanted not in devices or is_cable_name(devices[wanted]):
            wanted = real[0] if real else None
        if wanted:
            set_default(wanted, (int(role),))
            changed = True
    return changed


def make_cable_default() -> dict[str, str]:
    """Сделать виртуальный кабель основным микрофоном. Возвращает прежние значения."""
    cable = find_cable_capture()
    if not cable:
        raise OSError("CABLE Output не найден")
    saved = default_capture_ids()
    set_default_capture(cable)
    return saved


def restore_default(saved: dict[str, str]) -> None:
    for role, device_id in saved.items():
        set_default_capture(device_id, (int(role),))


# ---- автозапуск после перезагрузки и перезагрузка ----

_RUN_ONCE = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"


def set_run_once(command: Optional[str], name: str = "Golos") -> None:
    """Запустить команду один раз при следующем входе в Windows (None — отменить)."""
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_ONCE) as key:
        if command:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def reboot() -> None:
    subprocess.Popen(["shutdown.exe", "/r", "/t", "5", "/c", "Golos: перезагрузка для завершения установки VB-CABLE"],
                     creationflags=0x08000000)


def run_elevated(path: str, params: str, folder: str, timeout: float = 900.0) -> int:
    """Запустить программу с правами администратора и дождаться её. Возвращает код выхода.

    Если пользователь нажал «Нет» в окне контроля учётных записей, бросает PermissionError.
    """
    from ctypes import wintypes

    class _ShellExecuteInfo(Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", c_wchar_p), ("lpFile", c_wchar_p), ("lpParameters", c_wchar_p),
            ("lpDirectory", c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", c_void_p), ("lpClass", c_wchar_p), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000400  # NOCLOSEPROCESS | FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = path
    info.lpParameters = params
    info.lpDirectory = folder
    info.nShow = 1
    if not shell32.ShellExecuteExW(byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise PermissionError("отменено пользователем")
        raise OSError(f"ShellExecuteEx: ошибка {error}")
    if not info.hProcess:
        return 0
    try:
        if kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:
            raise TimeoutError("установка не завершилась")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


# ---- горячие клавиши ----

_MOD_ALT, _MOD_CONTROL, _MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
_WM_HOTKEY, _WM_QUIT, _WM_CLOSE = 0x0312, 0x0012, 0x0010


class Hotkeys:
    """RegisterHotKey — обычный механизм Windows. В процессы игр программа не вмешивается."""

    def __init__(self, bindings: dict[str, Callable[[], None]]):
        self.bindings = list(bindings.items())
        self.thread_id = 0
        self.failed: list[str] = []
        self.ready = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not IS_WINDOWS:
            self.ready.set()
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="hotkeys")
        self.thread.start()
        self.ready.wait(2.0)

    def _run(self) -> None:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.argtypes = [POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.thread_id = kernel32.GetCurrentThreadId()
        for number, (combo, _callback) in enumerate(self.bindings, start=1):
            letter = combo.rsplit("+", 1)[-1].upper()
            if not user32.RegisterHotKey(None, number, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, ord(letter)):
                self.failed.append(combo)
                log.warning("горячая клавиша занята: %s", combo)
        self.ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(byref(message), None, 0, 0) > 0:
            if message.message == _WM_HOTKEY:
                index = int(message.wParam) - 1
                if 0 <= index < len(self.bindings):
                    try:
                        self.bindings[index][1]()
                    except Exception:
                        log.exception("ошибка горячей клавиши")
        for number in range(1, len(self.bindings) + 1):
            user32.UnregisterHotKey(None, number)

    def stop(self) -> None:
        if IS_WINDOWS and self.thread_id:
            ctypes.WinDLL("user32").PostThreadMessageW(self.thread_id, _WM_QUIT, 0, 0)


# ---- одна копия программы и окно ----

_mutex_handle = None


def already_running() -> bool:
    global _mutex_handle
    if not IS_WINDOWS:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [c_void_p, ctypes.c_int, c_wchar_p]
    kernel32.CreateMutexW.restype = c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, 0, "Golos.SingleInstance.Mutex")
    return ctypes.get_last_error() == 183


def set_app_id() -> None:
    if IS_WINDOWS:
        try:
            ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID("Golos.Voice")
        except Exception:
            pass


def message_box(text: str, title: str = "Golos") -> None:
    if IS_WINDOWS:
        ctypes.WinDLL("user32").MessageBoxW(None, text, title, 0x10)


def _find_browser() -> Optional[str]:
    bases = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for relative in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for base in bases:
            path = os.path.join(base, relative) if base else ""
            if path and os.path.isfile(path):
                return path
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url: str, profile_dir: str) -> Optional[subprocess.Popen]:
    """Открывает интерфейс отдельным окном без вкладок через Edge или Chrome."""
    browser = _find_browser()
    if browser:
        try:
            return subprocess.Popen([
                browser,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--window-size=1060,740",
                "--no-first-run",
                "--no-default-browser-check",
            ])
        except OSError:
            log.exception("не удалось запустить браузер")
    import webbrowser
    webbrowser.open(url)
    return None


def focus_window(title: str = "Golos") -> bool:
    if not IS_WINDOWS:
        return False
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.argtypes = [c_wchar_p, c_wchar_p]
    user32.FindWindowW.restype = c_void_p
    handle = user32.FindWindowW(None, title)
    if not handle:
        return False
    user32.ShowWindow.argtypes = [c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [c_void_p]
    user32.ShowWindow(handle, 9)
    user32.SetForegroundWindow(handle)
    return True


def close_window(title: str = "Golos") -> None:
    if not IS_WINDOWS:
        return
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.argtypes = [c_wchar_p, c_wchar_p]
    user32.FindWindowW.restype = c_void_p
    user32.PostMessageW.argtypes = [c_void_p, ctypes.c_uint, c_void_p, c_void_p]
    handle = user32.FindWindowW(None, title)
    if handle:
        user32.PostMessageW(handle, _WM_CLOSE, None, None)
