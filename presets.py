"""Готовые голоса. Значения эффектов от 0 до 1, высота и тембр в полутонах."""

BUILTIN = {
    "Мой голос": {},
    "Низкий бас": {"pitch": -4.0, "formant": -2.5},
    "Здоровяк": {"pitch": -6.0, "formant": -5.0, "reverb": 0.08},
    "Демон": {"pitch": -9.0, "formant": -4.0, "drive": 0.22, "reverb": 0.3, "trim_db": -7.0},
    "Монстр": {"pitch": -12.0, "formant": -8.0, "drive": 0.4, "metal": 0.18, "metal_hz": 42.0, "reverb": 0.18, "trim_db": -7.0},
    "Высокий": {"pitch": 4.0, "formant": 1.5},
    "Девушка": {"pitch": 5.0, "formant": 3.0},
    "Ребёнок": {"pitch": 8.0, "formant": 5.0},
    "Бурундук": {"pitch": 10.0, "formant": 10.0},
    "Робот": {"robot": 1.0, "metal": 0.2, "metal_hz": 95.0, "reverb": 0.08, "trim_db": 0.5},
    "Киборг": {"pitch": -3.0, "formant": -1.0, "robot": 0.55, "metal": 0.45, "metal_hz": 60.0, "trim_db": 5.0},
    "Инопланетянин": {"pitch": 3.0, "formant": -3.0, "metal": 0.8, "metal_hz": 310.0, "reverb": 0.15, "trim_db": 2.5},
    "Шёпот": {"whisper": 1.0, "trim_db": 4.0},
    "Призрак": {"pitch": -2.0, "whisper": 0.75, "echo": 0.3, "reverb": 0.55, "trim_db": 5.0},
    "Рация": {"radio": 1.0, "trim_db": -7.0},
    "Пещера": {"echo": 0.4, "reverb": 0.75, "trim_db": -5.0},
}
