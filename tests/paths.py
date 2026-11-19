"""Пути для тестов: код программы лежит уровнем выше, имитации Windows-библиотек — в tests/fake."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake"


def use(fakes: bool = True, src: Path = ROOT) -> None:
    for path in ([str(FAKE)] if fakes else []) + [str(src), str(Path(__file__).resolve().parent)]:
        if path in sys.path:
            sys.path.remove(path)
    order = ([str(FAKE)] if fakes else []) + [str(src), str(Path(__file__).resolve().parent)]
    sys.path[:0] = order
