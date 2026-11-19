"""Установка и настройка VB-CABLE в один клик.

Что происходит по шагам:
1. Запоминаем, какие колонки и микрофон сейчас основные в Windows.
2. Скачиваем архив только с официального адреса download.vb-audio.com.
3. Проверяем цифровую подпись установщика (должна быть VB-Audio / Vincent Burel).
4. Запускаем установщик с правами администратора. Windows спросит разрешение.
5. Настраиваем: возвращаем колонки (Windows любит делать основным новый кабель),
   делаем изменённый голос основным микрофоном, подключаем Golos к кабелю.
6. Если устройства появятся только после перезагрузки, Golos запустится сам после неё
   и закончит настройку.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

import winsys

log = logging.getLogger("golos")

PAGE_URL = "https://vb-audio.com/Cable/"
DOWNLOAD_PREFIX = "https://download.vb-audio.com/Download_CABLE/"
KNOWN_PACKS = (46, 45, 43)
MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024
TRUSTED_SIGNERS = ("burel", "vb-audio", "vb audio")
DEVICE_WAIT = 25.0
CREATE_NO_WINDOW = 0x08000000
USER_AGENT = "Golos voice changer (Windows)"


class SetupError(Exception):
    """Ошибка, понятная пользователю."""


class Cancelled(Exception):
    pass


# ---- работа с сетью и системой: отдельные функции, чтобы их можно было подменить в тестах ----

def _open(url: str, timeout: float = 25.0):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_page() -> str:
    with _open(PAGE_URL, 15.0) as response:
        return response.read(2_000_000).decode("utf-8", errors="replace")


def download(url: str, target: Path, progress: Callable[[int, int], None]) -> None:
    """Скачать файл. Сначала средствами Python, при ошибке через curl.exe из Windows."""
    try:
        with _open(url) as response:
            final = response.geturl()
            if not final.startswith(DOWNLOAD_PREFIX):
                raise SetupError(f"Сайт перенаправил загрузку на чужой адрес: {final}")
            total = int(response.headers.get("Content-Length") or 0)
            if total > MAX_DOWNLOAD:
                raise SetupError("Файл на сайте подозрительно большой, установка остановлена.")
            done = 0
            with open(target, "wb") as file:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_DOWNLOAD:
                        raise SetupError("Файл на сайте подозрительно большой, установка остановлена.")
                    file.write(chunk)
                    progress(done, total)
        return
    except (SetupError, urllib.error.HTTPError):
        raise
    except Exception as exc:
        log.warning("загрузка через Python не удалась (%s), пробую curl.exe", exc)
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if not curl.is_file():
        raise SetupError("Не удалось скачать VB-CABLE. Проверь интернет.")
    progress(0, 0)
    result = subprocess.run(
        [str(curl), "-fsSL", "--proto", "=https", "--max-filesize", str(MAX_DOWNLOAD), "-o", str(target), url],
        capture_output=True, timeout=600, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise SetupError("Не удалось скачать VB-CABLE. Проверь интернет или включи VPN.")


def check_signature(path: Path) -> tuple[bool, str]:
    """Проверка подписи средствами Windows. Возвращает (подпись действительна, владелец)."""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$s=Get-AuthenticodeSignature -LiteralPath $env:GOLOS_CHECK_FILE;"
        "$o=@{status=[int]$s.Status;subject=''};"
        "if($s.SignerCertificate){$o.subject=$s.SignerCertificate.Subject};"
        "$o|ConvertTo-Json -Compress"
    )
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    env = dict(os.environ, GOLOS_CHECK_FILE=str(path))
    result = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, timeout=90, env=env, creationflags=CREATE_NO_WINDOW,
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    data = json.loads(text[text.find("{"):]) if "{" in text else {}
    return data.get("status") == 0, str(data.get("subject") or "")


def run_installer(setup: Path) -> int:
    return winsys.run_elevated(str(setup), "-i -h", str(setup.parent))


def cable_present() -> bool:
    return bool(winsys.find_cable_capture() and winsys.find_cable_render())


def remember_defaults() -> dict:
    try:
        return {"render": winsys.default_render_ids(), "capture": winsys.default_capture_ids()}
    except Exception:
        log.exception("не удалось прочитать устройства по умолчанию")
        return {"render": {}, "capture": {}}


# ---- вспомогательное ----

def pack_urls(page: Optional[str]) -> list[str]:
    numbers = []
    if page:
        numbers = sorted({int(n) for n in re.findall(r"VBCABLE_Driver_Pack(\d{2,3})\.zip", page)}, reverse=True)
    ordered = []
    for number in [*numbers, *KNOWN_PACKS]:
        if number not in ordered:
            ordered.append(number)
    return [f"{DOWNLOAD_PREFIX}VBCABLE_Driver_Pack{n}.zip" for n in ordered]


def pick_setup(names: list[str], machine: str) -> Optional[str]:
    exes = [n for n in names if n.lower().endswith(".exe") and "setup" in Path(n).name.lower()]
    machine = machine.lower()
    if "arm" in machine:
        wanted = [n for n in exes if "arm64" in n.lower()] or [n for n in exes if "x64" in n.lower()]
    elif machine in ("amd64", "x86_64", "x64"):
        wanted = [n for n in exes if "x64" in n.lower()]
    else:
        wanted = [n for n in exes if Path(n).name.lower() == "vbcable_setup.exe"]
    return wanted[0] if wanted else None


def safe_extract(archive: Path, folder: Path) -> list[str]:
    folder = folder.resolve()
    with zipfile.ZipFile(archive) as pack:
        members = pack.infolist()
        if sum(m.file_size for m in members) > MAX_UNPACKED:
            raise SetupError("Архив VB-CABLE подозрительно большой, установка остановлена.")
        for member in members:
            target = (folder / member.filename).resolve()
            if folder not in target.parents and target != folder:
                raise SetupError("Архив VB-CABLE повреждён.")
        pack.extractall(folder)
        return [m.filename for m in members if not m.is_dir()]


def signer_name(subject: str) -> str:
    match = re.search(r"CN=\"?([^,\"]+)", subject)
    return match.group(1).strip() if match else subject


class CableSetup:
    """Фоновая установка. Окно программы опрашивает snapshot() и показывает ход."""

    def __init__(self, work_dir: Path, configure: Callable[[dict], None],
                 persist: Callable[[Optional[dict]], None], restart_command: Optional[str]):
        self.work_dir = work_dir
        self.configure = configure
        self.persist = persist
        self.restart_command = restart_command
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.state = "idle"        # idle, running, need_reboot, done, error, cancelled
        self.step = ""             # download, verify, install, configure
        self.percent = -1
        self.message = ""
        self.signer = ""
        self.only_configure = False
        self.serial = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state, "step": self.step, "percent": self.percent,
                "message": self.message, "signer": self.signer,
                "only_configure": self.only_configure, "n": self.serial,
            }

    def _set(self, **values) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)
            self.serial += 1

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, before: Optional[dict] = None, only_configure: bool = False) -> bool:
        if self.busy():
            return False
        self._set(state="running", step="", percent=-1, message="", signer="", only_configure=only_configure)
        self.thread = threading.Thread(target=self._run, args=(before, only_configure), daemon=True, name="cable-setup")
        self.thread.start()
        return True

    def mark_reboot(self) -> None:
        self._set(state="need_reboot", step="install", message="")

    def reset(self) -> None:
        if not self.busy():
            self._set(state="idle", step="", percent=-1, message="")

    # ---- сам процесс ----

    def _run(self, before: Optional[dict], only_configure: bool) -> None:
        folder: Optional[Path] = None
        try:
            if before is None:
                before = remember_defaults()
            if only_configure or cable_present():
                self._configure(before)
                return
            self.persist({"stage": "installing", "before": before})
            folder = self.work_dir / f"vbcable-{int(time.time())}"
            folder.mkdir(parents=True, exist_ok=True)
            setup = self._download_and_verify(folder)
            self._install(setup)
            if self._wait_for_devices():
                self._configure(before)
            else:
                self.persist({"stage": "need_reboot", "before": before})
                if self.restart_command:
                    try:
                        winsys.set_run_once(self.restart_command)
                    except Exception:
                        log.exception("не удалось включить запуск после перезагрузки")
                self._set(state="need_reboot", step="install")
                log.info("VB-CABLE установлен, нужна перезагрузка")
        except Cancelled:
            self.persist(None)
            self._set(state="cancelled", message="Установка отменена: Windows не получила разрешение администратора. "
                                                  "Без него драйверы не ставятся.")
        except SetupError as exc:
            log.error("установка VB-CABLE: %s", exc)
            self.persist(None)
            self._set(state="error", message=str(exc))
        except Exception as exc:
            log.exception("установка VB-CABLE не удалась")
            self.persist(None)
            self._set(state="error", message=f"Установка не удалась: {exc}")
        finally:
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)

    def _download_and_verify(self, folder: Path) -> Path:
        self._set(step="download", percent=-1)
        try:
            page = fetch_page()
        except Exception as exc:
            log.warning("страница VB-CABLE недоступна (%s), беру известную версию", exc)
            page = None
        archive = folder / "vbcable.zip"
        last: Optional[Exception] = None
        for url in pack_urls(page):
            try:
                download(url, archive, self._progress)
                log.info("скачано %s", url)
                last = None
                break
            except urllib.error.HTTPError as exc:
                last = exc
                log.warning("нет файла %s (%s)", url, exc.code)
        if last is not None or not archive.is_file():
            raise SetupError("Не удалось найти VB-CABLE на официальном сайте. Установи его вручную по кнопке ниже.")

        self._set(step="verify", percent=-1)
        try:
            names = safe_extract(archive, folder / "pack")
        except zipfile.BadZipFile:
            raise SetupError("Скачанный архив повреждён. Попробуй ещё раз.") from None
        chosen = pick_setup(names, platform.machine())
        if not chosen:
            raise SetupError("В архиве VB-CABLE нет установщика для твоей Windows. Установи его вручную по кнопке ниже.")
        setup = folder / "pack" / chosen
        try:
            valid, subject = check_signature(setup)
        except Exception as exc:
            log.exception("проверка подписи не удалась")
            raise SetupError(f"Windows не смогла проверить подпись установщика ({exc}).") from None
        name = signer_name(subject)
        log.info("подпись установщика: %s, действительна: %s", subject, valid)
        if not valid or not any(s in subject.lower() for s in TRUSTED_SIGNERS):
            raise SetupError(f"Подпись установщика не прошла проверку ({name or 'нет подписи'}). "
                             "Установка остановлена ради безопасности.")
        self._set(signer=name)
        return setup

    def _progress(self, done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else -1
        if percent != self.percent:
            self._set(percent=percent)

    def _install(self, setup: Path) -> None:
        self._set(step="install", percent=-1)
        try:
            code = run_installer(setup)
        except PermissionError:
            raise Cancelled() from None
        except TimeoutError:
            raise SetupError("Установщик VB-CABLE не завершился за 15 минут.") from None
        log.info("установщик VB-CABLE завершился с кодом %s", code)

    def _wait_for_devices(self) -> bool:
        deadline = time.monotonic() + DEVICE_WAIT
        while time.monotonic() < deadline:
            try:
                if cable_present():
                    return True
            except Exception:
                log.exception("не удалось проверить устройства")
            time.sleep(1.0)
        return False

    def _configure(self, before: dict) -> None:
        self._set(step="configure", percent=-1)
        self.configure(before)
        self.persist(None)
        if self.restart_command:
            try:
                winsys.set_run_once(None)
            except Exception:
                pass
        self._set(state="done", step="configure")
        log.info("VB-CABLE настроен")
