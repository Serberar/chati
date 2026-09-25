import plans


def test_get_plan_empty_for_unknown_session():
    assert plans.get_plan("sesion-que-no-existe") == []


def test_set_and_get_plan_roundtrip():
    pasos = [
        {"texto": "Leer el archivo", "estado": "hecho"},
        {"texto": "Escribir el resumen", "estado": "en_progreso"},
    ]
    plans.set_plan("s1", pasos)

    assert plans.get_plan("s1") == pasos


def test_set_plan_drops_empty_step_text():
    plans.set_plan("s2", [{"texto": "  ", "estado": "pendiente"}, {"texto": "paso real", "estado": "pendiente"}])

    result = plans.get_plan("s2")

    assert result == [{"texto": "paso real", "estado": "pendiente"}]


def test_set_plan_normalizes_invalid_state_to_pendiente():
    plans.set_plan("s3", [{"texto": "paso", "estado": "algo-invalido"}])

    assert plans.get_plan("s3") == [{"texto": "paso", "estado": "pendiente"}]


def test_set_plan_overwrites_previous_plan_for_same_session():
    plans.set_plan("s4", [{"texto": "version 1", "estado": "pendiente"}])
    plans.set_plan("s4", [{"texto": "version 2", "estado": "hecho"}])

    assert plans.get_plan("s4") == [{"texto": "version 2", "estado": "hecho"}]


def test_plans_are_isolated_per_session():
    plans.set_plan("sesion-a", [{"texto": "a", "estado": "pendiente"}])
    plans.set_plan("sesion-b", [{"texto": "b", "estado": "pendiente"}])

    assert plans.get_plan("sesion-a") == [{"texto": "a", "estado": "pendiente"}]
    assert plans.get_plan("sesion-b") == [{"texto": "b", "estado": "pendiente"}]
