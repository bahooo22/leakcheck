"""Гейт ядра меры утечек (`leakcheck.py`).

Перенесён из `Qoder_i18n/tools/test_prose_leaks.py` (семь проверок) и усилен
тем, что ядро получило от прежней глобальной копии: поиск похожих на секрет
вкраплений, режим ручного смотра и код выхода за пустой вход. Последняя проверка
обязана читать ФАЙЛ ЯДРА, а не обёртку проекта: иначе «мера проходит саму себя»
проверяет пять строк реэкспорта и молчит на дефект в правилах.
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leakcheck as S  # noqa: E402


def scan(line, **kw):
    """Одна строка через ту же вычистку данных, что и весь текстовый режим."""
    return S.find_in_line(S.scrub(line), **kw)


class TestProseMeasure(unittest.TestCase):
    def test_catches_glue_in_both_orders(self):
        self.assertTrue(scan("перевели при.search по словарям"))
        self.assertTrue(scan("то же самое Qoder.Он в середине"))

    def test_catches_the_case_that_slipped_past(self):
        """Прежняя мерка видела в точке разделитель — проверка это ловит."""
        self.assertTrue(any("точк" in h for h in scan("дефис при.search")))

    def test_digit_inside_word_is_glue(self):
        """Поиск по соседним символам пропускал вставленную цифру: `задач2Search`
        — обе половины в одном запуске букв и цифр."""
        self.assertTrue(scan("новая задач2Search в заголовке"))

    def test_backtick_content_is_data(self):
        self.assertEqual(scan("файл `main.cjs` и ссылка `read.fs`"), [])

    def test_spaced_abbreviation_is_clean(self):
        self.assertEqual(scan("см. README и т.д. без склейки"), [])

    def test_numbers_are_not_glue(self):
        self.assertEqual(scan("замер 10.10 и 2.5 реплики"), [])

    def test_dash_is_a_separator_not_glue(self):
        self.assertEqual(scan("Git-репозиторий и HTML-артефакт"), [])

    def test_cjk_still_caught(self):
        # иероглифы в исходнике не пишутся: пара U+6B63 и U+5E38 собрана
        # экранированием, как и в других гейтах
        self.assertTrue(S.CJK.search("и тут\u6b63\u5e38ное русское слово"))


class TestSecretAndExitCodes(unittest.TestCase):
    def write(self, body):
        fd, path = tempfile.mkstemp(suffix=".txt", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        self.addCleanup(os.remove, path)
        return path

    def test_secret_is_caught_and_can_be_disabled(self):
        line = "bottoken = AbCdEf1234567890XyZe"
        self.assertTrue(any("секрет" in h for h in scan(line)))
        self.assertEqual(scan(line, secret=False), [])

    def test_empty_input_is_not_clean(self):
        """Молчаливое «Чисто» по пустому файлу проверкой не считается."""
        buf_out = sys.stdout
        import contextlib
        import io
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            code = S.prose([self.write("   \n")])
        sys.stdout = buf_out
        self.assertEqual(code, 2, sink.getvalue())

    def test_unreadable_path_is_not_a_finding(self):
        """Опечатка в имени файла не имеет права вернуться кодом 1: ритуал
        коммита читает 1 как «дефект найден», а traceback с кодом 1 выглядит
        точь-в-точь как удачная ловля."""
        import contextlib
        import io
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            code = S.prose([os.path.join(tempfile.gettempdir(),
                                         "такого-файла-нет.txt")])
        self.assertEqual(code, 2, sink.getvalue())
        self.assertNotIn("ЕСТЬ НАХОДОК", sink.getvalue())

    def test_hit_exit_code_is_a_signal_not_a_counter(self):
        """Код выхода обязан быть 1, а не числом находок: ритуалы проверяют
        именно сигнал, и счётчик молча ломает любую связку `||` и `&&`."""
        import contextlib
        import io
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            code = S.prose([self.write("одна при.search и вторая при.search")])
        self.assertEqual(code, 1, sink.getvalue())

    def test_main_without_paths_is_usage(self):
        import contextlib
        import io
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            code = S.main([])
        self.assertEqual(code, 2)

    def test_version_flag(self):
        import contextlib
        import io
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            code = S.main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(S.__version__, sink.getvalue())


class TestExtensionContract(unittest.TestCase):
    def ext_module(self, body):
        fd, path = tempfile.mkstemp(suffix=".py", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        self.addCleanup(os.remove, path)
        return path

    def test_ext_classes_join_the_base_measure(self):
        path = self.ext_module(
            "import re\n"
            "EXTRA = [('двойная склейка', re.compile(r'[А-Яа-я]:[A-Za-z]'))]\n")
        ext = S.load_ext(path)
        # «текст:Latin» базовая мерка не видит: двоеточие разрывает запуск букв,
        # а точка тут ни при чём. Это ровно тот класс, ради которого --ext и есть.
        self.assertEqual(scan("текст:Latin в строке"), [])
        with_extra = S.find_in_line("текст:Latin в строке",
                                    extra=tuple(ext.EXTRA))
        self.assertTrue(any("двойная склейка" in h for h in with_extra))

    def test_verify_targets_gate_a_dead_extension(self):
        path = self.ext_module("VERIFY_TARGETS = [r'%s']\n"
                               % os.path.join(tempfile.gettempdir(),
                                              "такого-пути-нет"))
        with self.assertRaises(SystemExit):
            S.load_ext(path)

    def test_extension_can_declare_its_own_flags(self):
        """Флаги режима проекта дополнения объявляют сами: иначе `--ru` не
        распарсится, а тихая потеря флага — это мерка, которая ничего не меряла."""
        path = self.ext_module(
            "MODES = {'dict': lambda core, args: 0 if args.ru == 'x' else 1}\n"
            "ADD_ARGUMENTS = [(('--ru',), {'default': None})]\n")
        self.assertEqual(S.main(["--ext", path, "--mode", "dict", "--ru", "x"]), 0)

    def test_project_flag_without_mode_is_refused(self):
        with self.assertRaises(SystemExit):
            S.main(["--ru", "x", "не-важно-какой-файл"])

    def test_unknown_mode_names_itself(self):
        path = self.ext_module("MODES = {}\n")
        with self.assertRaises(SystemExit):
            S.main(["--ext", path, "--mode", "dict", "--prose", __file__])


class TestVerbatimData(unittest.TestCase):
    def test_project_lists_come_from_the_extension(self):
        # ядро не знает брендов: без DATA совпадение с английским — дефект
        self.assertFalse(S.verbatim_ok("Finder"))
        data = {"brand_values": {"finder"}, "keep_tokens": {"git"}}
        self.assertTrue(S.verbatim_ok("Finder", "Finder", data))
        self.assertTrue(S.verbatim_ok("Git", "Git", data))
        # маска по всей строке, а не по подстроке: «and/or» из прозы не пройдёт
        self.assertFalse(S.verbatim_ok("see and/or skip", None, data))

    def test_urls_paths_and_signs_need_no_translation(self):
        self.assertTrue(S.verbatim_ok("https://github.com/org/repo.git"))
        self.assertTrue(S.verbatim_ok("/usr/local/bin"))
        self.assertTrue(S.verbatim_ok("—"))
        self.assertTrue(S.verbatim_ok("{{count}}"))


class TestCountingForms(unittest.TestCase):
    def test_num_russian_forms(self):
        self.assertEqual(S.num(1, "файл", "файла", "файлов"), "1 файл")
        self.assertEqual(S.num(2, "файл", "файла", "файлов"), "2 файла")
        self.assertEqual(S.num(5, "файл", "файла", "файлов"), "5 файлов")
        self.assertEqual(S.num(21, "файл", "файла", "файлов"), "21 файл")

    def test_leaves_flattens_dicts_and_lists(self):
        flat = S.leaves({"a": {"b": 1}, "c": ["x", "y"]})
        self.assertEqual(flat[("a", "b")], 1)
        self.assertEqual(flat[("c",)], "x | y")


class TestSelfCheck(unittest.TestCase):
    def test_measure_passes_on_its_own_source(self):
        """Docstring ядра цитирует примеры дефектов, поэтому они обязаны стоять
        в обратных кавычках: иначе инструмент не проходит свою проверку, а это
        ровно та слепая зона, из-за которой гейт и появился."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = S.prose([os.path.abspath(S.__file__)])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(os.path.abspath(S.__file__),
                         os.path.abspath(os.path.join(
                             os.path.dirname(__file__), "leakcheck.py")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
