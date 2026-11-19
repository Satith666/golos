"""Проверки на настоящей Windows (запускаются на серверах GitHub при каждой сборке).

Проверяет то, что нельзя проверить имитацией:
- вызовы звуковой системы Windows (COM) не падают;
- запуск с правами администратора и код выхода;
- запись в реестр для запуска после перезагрузки;
- проверка цифровой подписи через Windows;
- настоящий архив VB-CABLE: скачивается с официального сайта, в нём есть установщик,
  его подпись действительна и принадлежит VB-Audio.
"""
import os
import platform
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cable  # noqa: E402
import winsys  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, func):
    try:
        detail = func()
        results.append((name, True, str(detail or "")))
    except Exception:
        results.append((name, False, traceback.format_exc()))


def endpoints():
    render, capture = winsys.render_devices(), winsys.capture_devices()
    return f"вывод: {render}; микрофоны: {capture}"


def defaults():
    return f"вывод: {winsys.default_render_ids()}; микрофоны: {winsys.default_capture_ids()}"


def run_once():
    import winreg
    winsys.set_run_once('"C:\\Golos test\\Golos.exe"', name="GolosSmokeTest")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, winsys._RUN_ONCE) as key:
        value = winreg.QueryValueEx(key, "GolosSmokeTest")[0]
    winsys.set_run_once(None, name="GolosSmokeTest")
    assert value == '"C:\\Golos test\\Golos.exe"', value
    return value


def elevated():
    code = winsys.run_elevated(os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), "/c exit 7",
                               os.environ["SystemRoot"], timeout=60)
    assert code == 7, code
    return f"код выхода {code}"


def signature_windows():
    valid, subject = cable.check_signature(Path(os.environ["SystemRoot"]) / "explorer.exe")
    assert valid and "microsoft" in subject.lower(), (valid, subject)
    return subject


def signature_unsigned():
    folder = Path(tempfile.mkdtemp())
    fake = folder / "VBCABLE_Setup_x64.exe"
    fake.write_bytes(b"MZ not really a program")
    valid, subject = cable.check_signature(fake)
    assert not valid, subject
    return "неподписанный файл отклонён"


def real_vbcable():
    page = cable.fetch_page()
    urls = cable.pack_urls(page)
    folder = Path(tempfile.mkdtemp())
    archive = folder / "vbcable.zip"
    used = None
    for url in urls:
        try:
            cable.download(url, archive, lambda done, total: None)
            used = url
            break
        except Exception as exc:
            print("нет", url, exc)
    assert used, "архив не скачался"
    names = cable.safe_extract(archive, folder / "pack")
    setup = cable.pick_setup(names, platform.machine())
    assert setup, names
    valid, subject = cable.check_signature(folder / "pack" / setup)
    trusted = any(s in subject.lower() for s in cable.TRUSTED_SIGNERS)
    assert valid and trusted, f"подпись: {valid}, {subject}"
    return f"{used} -> {setup}, подписано: {subject}, файлов в архиве: {len(names)}"


check("устройства Windows", endpoints)
check("устройства по умолчанию", defaults)
check("запуск после перезагрузки (реестр)", run_once)
check("запуск с правами администратора", elevated)
check("подпись файла Windows", signature_windows)
check("неподписанный файл", signature_unsigned)
check("настоящий VB-CABLE с сайта", real_vbcable)

lines = []
for name, ok, detail in results:
    lines.append(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}")
text = "\n".join(lines)
print(text)
Path("win-smoke.txt").write_text(text + "\n", encoding="utf-8")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
