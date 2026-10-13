"""Единая мера текстовых утечек — канон для всех проектов.

Запуск: python -X utf8 leakcheck.py [ФАЙЛ…] [--ext ФАЙЛ] [--prose-num] [--latin]
        [--no-secret] [--mode ИМЯ] [--version]

Коды выхода: 0 — чисто, 1 — есть находки, 2 — вход пуст или не читается,
результат неинформативен. Последний важен: молчаливое «Чисто» по пустому файлу
проверкой не является, и ровно так однажды ритуал коммита прошёл, ничего не
проверив.

Что мерит ядро, не зная ничего о проекте:
  1. иероглифы (CJK, включая расширения и знаки fullWidth);
  2. слитную кириллицу с латиницей внутри одного запуска букв и цифр — поэтому
     вставленная цифра (`задач2Search`) находится, а дефисные склейки
     («Git-репозиторий», «HTML-артефакт») — нет: дефис мерка считает
     разделителем, легальных таких мест в словарях десятки;
  3. склейку через точку (`слово.имя`, `имя.Слово`) — прежняя версия меры видела
     в точке разделитель и молчала, из-за чего дефект проскочил в теле коммита;
  4. вкрапление, похожее на секрет: `token = …`, `api_key: …` и прочее с
     шестнадцатью и более символами значения.

Содержимое обратных кавычек вырезается до счёта: `documentElement.lang` — это
данные, а не русское предложение, и ловить в нём латиницу — значит тонуть в
ложных срабатываниях. Экраны литералов (`\t`, `\n`) вырезаются тем же шагом:
иначе буква экрана склеивается со следующим русским словом.

Знания о конкретном проекте подключаются модулем через `--ext ФАЙЛ`. Модуль
может объявить любое из:
  EXTRA          — список `(заголовок, regex)` новых классов находок;
  DATA           — dict с ключами `keep_tokens`, `brand_values`,
                   `brand_titles` — легальные совпадения с английским;
  MODES          — dict `{"имя": вызываемый(core, args)}` — целый режим проекта
                   (например мерка словаря переводов со своим покрытием);
  ADD_ARGUMENTS  — список `( ("-флаг", …), {kwargs argparse} )`: проектные флаги
                   дописываются к парсеру ядра, иначе режим проекта нечем вызвать;
  VERIFY_TARGETS — список путей, которые обязаны существовать (гейт того, что
                   дополнение подключилось к живому проекту, а не к пустоте).

Собственный текст этой меры проходит её же проверку — примеры дефектов поэтому
стоят в обратных кавычках, и это проверяется гейтом, а не словом.
"""
import argparse
import importlib.util
import os
import re
import sys

__version__ = "1.0.0"

CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                 "\uac00-\ud7af\u3001\u3003\uff00-\uffff]")
# Смешанное «слово» = непрерывный запуск букв/цифр, в котором есть и кириллица,
# и латиница. Поиск по соседним символам пропускал вставленную цифру.
MIXED_RUN = re.compile(r"[0-9A-Za-z\u0400-\u04ff]+")
CYRILLIC = re.compile("[\u0400-\u04ff]")
LATIN = re.compile("[A-Za-z]")
DOT_GLUE = re.compile(r"[А-Яа-яЁё]\.[A-Za-z]|[A-Za-z]\.[А-Яа-яЁё]")
PLACEHOLDER = re.compile(r"{{\s*([\w.]+)\s*(?:,[^}]*)?}}")
TAG = re.compile(r"</?[\w-]+/?>")
PLURAL = re.compile(r"_(one|two|few|many|other)$")
BACKTICK = re.compile(r"`[^`]*`")
ESCAPES = re.compile(r"\\[tnrxv0]")
NUMBER_WORD = re.compile(r"\b\d+\s+[а-яё]{3,}", re.IGNORECASE)
SECRET = re.compile(r"(?:bottoken|api[_-]?key|password|passwd|secret|token)"
                    r"\s*[=:]\s*['\"]?[A-Za-z0-9\-_/]{16,}", re.I)
LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-'/:+]{1,}")


def num(n, one, few, many):
    """Русская счётная форма: 1 «файл», 2 «файла», 5 «файлов»."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def leaves(d, prefix=()):
    """Плоский обход словаря: пути-кортежи -> значения (списки сворачиваются)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(leaves(v, prefix + (k,)))
        elif isinstance(v, list):
            out[prefix + (k,)] = " | ".join(str(x) for x in v)
        else:
            out[prefix + (k,)] = v
    return out


def mixed(value):
    """Слова, где кириллица и латиница идут слитно (дефис — разделитель)."""
    return [w for w in MIXED_RUN.findall(str(value))
            if CYRILLIC.search(w) and LATIN.search(w)]


def verbatim_ok(value, en_value=None, data=None):
    """Совпадение с английским допустимо, если в строке нечего переводить.

    `data` — легальные списки проекта из дополнения: `keep_tokens` (термины,
    которые держатся латиницей осознанно), `brand_values` (значения целиком:
    собственные названия наборов и продуктов), `brand_titles`. Токен-лист для
    брендов не подходит: туда уехали бы `file` и `icons`, и реальный недосмотр
    вида «File» вместо «Файл» перестал бы ловиться.
    """
    data = data or {}
    keep = {t.lower() for t in data.get("keep_tokens", ())}
    brands = {v.lower() for v in data.get("brand_values", ())}
    s = str(value).strip()
    if s == "":
        return True
    if s.lower() in brands:
        return True
    # Целиком URL или путь — переводить нечего. Правило обязательное для ВСЕЙ
    # строки: маска по подстроке проглотила бы «and/or» из прозы.
    if re.fullmatch(r"[A-Za-z][\w+.-]*://\S+", s):
        return True
    if re.fullmatch(r"(?:[~.]?/)[\w./+@-]+", s) and "/" in s[1:]:
        return True
    tokens = re.findall(r"[A-Za-z]+", PLACEHOLDER.sub("", s).lower())
    if not tokens:  # только знаки и цифры
        return True
    return all(t in keep for t in tokens)


def load_ext(path):
    """Загрузить модуль-дополнение проекта; вернуть None, если путь не задан."""
    if not path:
        return None
    spec = importlib.util.spec_from_file_location("leakcheck_ext", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"дополнение не загружается: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for rel in getattr(mod, "VERIFY_TARGETS", ()):
        if not os.path.exists(rel):
            raise SystemExit(f"дополнение {os.path.basename(path)} ссылается на "
                             f"несуществующий путь: {rel}")
    return mod


def text_of(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def scrub(line):
    """Вырезать данные перед счётом: обратные кавычки и экраны литералов."""
    return ESCAPES.sub(" ", BACKTICK.sub(" ", line))


def find_in_line(text, with_numbers=False, secret=True, extra=()):
    hits = []
    if CJK.search(text):
        hits.append("иероглифы: " + "".join(CJK.findall(text)[:8]))
    hits += [f"смешение: {word}" for word in mixed(text)]
    hits += [f"склейка через точку: {m.group()}" for m in DOT_GLUE.finditer(text)]
    if secret:
        hits += [f"похоже на секрет: {m.group()[:60]}"
                 for m in SECRET.finditer(text)]
    if with_numbers:
        hits += [f"число+слово: {m.group()}" for m in NUMBER_WORD.finditer(text)]
    for title, pattern in extra:
        hits += [f"{title}: {m.group()}" for m in pattern.finditer(text)]
    return hits


def prose(paths, with_numbers=False, secret=True, ext=None, show_latin=False):
    """Мерка смешения по текстовым файлам: README, промпт, тело коммита, отчёт.

    Возвращает код выхода: 0 чисто, 1 есть находки, 2 вход пуст или его не
    прочитать. Классы дополнения (`EXTRA`) добираются к трём базовым, если
    передан загруженный модуль. Оборот «число + слово» включается только
    `with_numbers`: в русской прозе таких мест сотни легальных, и без флага
    они дали бы поток шума.
    """
    extra = tuple(getattr(ext, "EXTRA", ())) if ext else ()
    bad = empty = 0
    for path in paths:
        try:
            body = text_of(path)
        except OSError as error:
            # Сломанный путь — это не находка: код 1 зарезервирован под сигнал
            # «дефект найден», и ритуал коммита иначе прочитал бы опечатку в
            # имени файла как удачную проверку.
            print(f"{path} НЕЧИТАЕМ: {error}\n    результат неинформативен")
            empty = 2
            continue
        if not body.strip():
            print(f"{path} ПУСТО — результат неинформативен")
            empty = 2
            continue
        for n, line in enumerate(body.splitlines(), 1):
            for hit in find_in_line(scrub(line), with_numbers, secret, extra):
                bad += 1
                print(f"{path}:{n}: {hit}\n    {line.rstrip()}")
        if show_latin:
            tokens = {}
            for n, line in enumerate(BACKTICK.sub(" ", body).splitlines(), 1):
                for tok in LATIN_TOKEN.findall(line):
                    tokens.setdefault(tok.lower(), []).append(n)
            for tok in sorted(tokens):
                print(f"{path} ЛАТ {tok} {tokens[tok][:6]}")
    print("\n" + ("Чисто." if not bad and not empty else
                  ("ЕСТЬ НАХОДОК: " + num(bad, "находка", "находки", "находок")
                   if bad else "Мерка ничего не проверила: вход пуст или нечитаем")))
    # Код выхода — сигнал, а не счётчик: ровно 1 на находки и 2 на пустой вход.
    return 1 if bad else empty


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", metavar="ФАЙЛ",
                    help="текстовые файлы для общей мерки")
    ap.add_argument("--prose", nargs="+", default=None, metavar="ФАЙЛ",
                    help="то же, что позиционные файлы (для совместимости)")
    ap.add_argument("--prose-num", action="store_true",
                    help="печатать и оборот «число + слово»")
    ap.add_argument("--latin", action="store_true",
                    help="дополнительно напечатать латинские токены на ручной смотр")
    ap.add_argument("--no-secret", action="store_true",
                    help="не искать вкрапления, похожие на секрет")
    ap.add_argument("--ext", default=None, metavar="ФАЙЛ",
                    help="модуль с проектными классами, данными и режимами")
    ap.add_argument("--mode", default=None, metavar="ИМЯ",
                    help="режим из дополнения (словарь, покрытие и прочее)")
    ap.add_argument("--version", action="store_true", help="версия ядра")
    # Первый проход терпит незнакомые флаги: их может объявить дополнение, и
    # только после загрузки дополнения видно, законен ли флаг без `--mode`.
    args, rest = ap.parse_known_args(argv)
    if args.version:
        print(f"leakcheck {__version__}")
        return 0

    ext = load_ext(args.ext)
    if args.mode:
        modes = getattr(ext, "MODES", {}) if ext else {}
        if args.mode not in modes:
            raise SystemExit(f"режима {args.mode} нет: дополнение подключает "
                             f"{sorted(modes) or 'None'}")
        # Проектные флаги знает только само дополнение: они объявляются им и
        # дописываются сюда, иначе `--ru` не распарсится.
        for flags, kwargs in (getattr(ext, "ADD_ARGUMENTS", []) if ext else []):
            ap.add_argument(*flags, **kwargs)
        args = ap.parse_args(argv)
        return modes[args.mode](sys.modules[__name__], args) or 0
    unknown = [a for a in rest if a.startswith("--")]
    if unknown:
        raise SystemExit(f"флаг {unknown[0]} без `--mode`: его умеет только "
                         f"дополнение, передайте --ext ФАЙЛ и --mode ИМЯ")
    paths = list(args.paths) + list(args.prose or [])
    if not paths:
        ap.print_usage()
        return 2
    return prose(paths, args.prose_num, not args.no_secret, ext, args.latin)


if __name__ == "__main__":
    sys.exit(main())
