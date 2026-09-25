import tools


def test_ejecutar_python_returns_printed_output():
    result = tools.ejecutar_python("print(2 + 2)")
    assert result.strip() == "4"


def test_ejecutar_python_reports_syntax_errors():
    result = tools.ejecutar_python("esto no es python valido !!!")
    assert "error" in result.lower()


def test_ejecutar_python_reports_runtime_errors():
    result = tools.ejecutar_python("print(1 / 0)")
    assert "error" in result.lower()
    assert "ZeroDivisionError" in result


def test_ejecutar_python_times_out_on_infinite_loop(monkeypatch):
    monkeypatch.setattr(tools, "PYTHON_EXEC_TIMEOUT", 1)  # no esperar 10s reales en el test
    result = tools.ejecutar_python("while True:\n    pass")
    assert "tardo mas de" in result


def test_ejecutar_python_no_output_gives_a_hint():
    result = tools.ejecutar_python("x = 1 + 1")
    assert "no imprimio nada" in result


def test_ejecutar_python_truncates_huge_output():
    result = tools.ejecutar_python("print('a' * 100000)")
    assert len(result) <= tools.PYTHON_OUTPUT_LIMIT + len("\n... (salida truncada)")
    assert "truncada" in result


def test_ejecutar_python_does_not_leak_files_outside_tempdir():
    # el directorio de trabajo del codigo ejecutado debe ser temporal, no el
    # directorio real del proyecto
    result = tools.ejecutar_python("import os; print(os.getcwd())")
    assert "orchestrator" not in result.lower()
