import json

import pytest

import deep_search


def test_plan_normalizes_what_the_model_proposes():
    reply = json.dumps({"entendido": "Casas en venta en La Victoria o Giron", "tipo": "listado",
                        "criterios": ["en venta", "La Victoria, Giron o barrios vecinos"],
                        "campos": ["precio", "m2", "habitaciones", "barrio"],
                        "webs": ["https://www.idealista.com/", "fotocasa.es", "no es una web"],
                        "busquedas": ["casas venta Valladolid La Victoria", "pisos Giron Valladolid"]})
    plan = deep_search.plan_search(lambda p: reply, "buscame casas")
    assert plan["tipo"] == "listado"
    assert plan["webs"] == ["idealista.com", "fotocasa.es"]
    assert plan["campos"] == ["precio", "m2", "habitaciones", "barrio"]
    # sin respuesta util del modelo: se busca lo que pidio tal cual
    fallback = deep_search.plan_search(lambda p: "no se", "coches electricos baratos")
    assert fallback["busquedas"] == ["coches electricos baratos"] and fallback["tipo"] == "listado"


def _page(url, text, links=()):
    return {"url": url, "text": text, "links": list(links), "image": None, "product": {}}


PLAN = {"entendido": "casas", "tipo": "listado", "criterios": ["venta"], "campos": ["precio", "barrio"],
        "webs": [], "busquedas": ["casas"]}


def test_extract_items_maps_links_and_drops_missing_data():
    page = _page("https://portal.es/venta", "Piso Portillo 150.000 € La Victoria. Chalet Giron 320.000 €. " * 5,
                 [("Piso en calle Portillo, La Victoria", "https://portal.es/inmueble/1"),
                  ("Aviso legal", "https://portal.es/legal"),
                  ("Chalet en Giron con jardin y garaje", "https://portal.es/inmueble/2"),
                  ("Otro portal externo con texto largo", "https://otra.com/x")])
    reply = json.dumps({"pagina": "listado", "r": [
        {"t": "Piso Portillo", "v": ["150.000 €", "La Victoria"], "e": 0, "p": 9},
        {"t": "Chalet Giron", "v": ["320.000 €", None], "e": 99, "p": "8"}]})
    items = deep_search.extract_items(lambda p: reply, PLAN, page)
    assert items[0]["url"] == "https://portal.es/inmueble/1" and items[0]["datos"]["barrio"] == "La Victoria"
    assert items[1]["url"] == "https://portal.es/inmueble/2"  # enlace que no existe: el que se parece al titulo
    assert items[1]["datos"] == {"precio": "320.000 €"} and items[1]["encaje"] == 8


class FakeBrowser:
    def __init__(self, pages, results):
        self.pages, self.results = pages, results

    def bing(self, query, n):
        return self.results

    def fetch_page(self, url):
        if url not in self.pages:
            raise deep_search.Blocked(url)
        return self.pages[url]


def test_listing_search_filters_dedupes_and_summarizes():
    pages = {"https://a.es/l": _page("https://a.es/l", "LISTADO A Piso 1 por 100, Alquiler 700/mes " * 10),
             "https://b.es/l": _page("https://b.es/l", "LISTADO B Piso 2 por 150 " * 20),
             "https://vacia.es": _page("https://vacia.es", "casi nada")}

    def chat(prompt):
        if "Prepara una busqueda" in prompt:
            return json.dumps({**PLAN, "webs": ["b.es"]})
        if "TIPO concreto" in prompt:
            return json.dumps({"si": ["piso", "vivienda"], "no": ["alquiler"]})
        if "LISTADO A" in prompt:
            return json.dumps({"pagina": "listado", "r": [
                {"t": "Piso 1", "v": ["100"], "e": None, "p": 7},
                {"t": "Alquiler", "v": ["700/mes"], "e": None, "p": 2}]})
        if "LISTADO B" in prompt:
            return json.dumps({"pagina": "listado", "r": [
                {"t": "Piso 2", "v": ["150"], "e": None, "p": 9}]})
        if "resultados en total" in prompt:
            return json.dumps({"resumen": "Hay 2 pisos.", "destacados": [{"n": 0, "por_que": "el que mejor encaja"}]})
        raise AssertionError(prompt[:80])

    browser = FakeBrowser(pages, ["https://a.es/l", "https://b.es/l", "https://vacia.es", "https://bloqueada.es",
                                  "https://www.youtube.com/v"])
    res = deep_search.run_search(chat, browser, "casas", lambda *a: None, lambda: False)
    assert [it["titulo"] for it in res["resultados"]] == ["Piso 2", "Piso 1"]  # sin el alquiler, por encaje
    assert res["resumen"] == "Hay 2 pisos."
    assert res["destacados"][0]["titulo"] == "Piso 2"
    assert any("bloqueada.es" in w for w in res["avisos"])


def test_question_gives_a_report_with_sources():
    pages = {"https://gob.es/ayudas": _page("https://gob.es/ayudas", "AYUDAS " * 50)}

    def chat(prompt):
        if "Prepara una busqueda" in prompt:
            return json.dumps({"entendido": "ayudas primera vivienda", "tipo": "informe", "busquedas": ["ayudas"]})
        if "¿Que dice esta pagina" in prompt:
            return json.dumps({"util": True, "titulo": "Ayudas", "datos": ["Aval del 20% para menores de 35"]})
        if "Informacion encontrada" in prompt:
            assert "[1] Ayudas (gob.es): Aval del 20% para menores de 35" in prompt
            return "Hay un aval del 20% [1]."
        raise AssertionError(prompt[:80])

    res = deep_search.run_search(chat, FakeBrowser(pages, ["https://gob.es/ayudas"]), "ayudas",
                                 lambda *a: None, lambda: False)
    assert res["tipo"] == "informe" and res["informe"] == "Hay un aval del 20% [1]."
    assert res["fuentes"][0]["url"] == "https://gob.es/ayudas" and res["resultados"] == []


def test_refining_keeps_the_original_request():
    prev = {"consulta": "casas en La Victoria"}
    assert deep_search.full_request("solo con ascensor", prev, "solo con ascensor") == \
        "casas en La Victoria\nAdemas: solo con ascensor"
    assert deep_search.full_request("casas", None, "") == "casas"


def test_start_needs_a_request():
    with pytest.raises(ValueError):
        deep_search.start("u1", b"k" * 32, 1, "  ", lambda p: "")


def test_results_that_are_not_in_the_page_are_dropped():
    """Con una pagina casi vacia, el modelo se invento 15 anuncios (2026-09-29)."""
    page = _page("https://portal.es/v", "Cookies. Aceptar. Piso en calle Real 120.000 euros. " * 6)
    reply = json.dumps({"pagina": "listado", "r": [
        {"t": "Piso calle Real", "v": ["120.000 euros", None], "e": None, "p": 9},
        {"t": "Chalet inventado", "v": ["999.999 €", "Barrio falso"], "e": None, "p": 10}]})
    items = deep_search.extract_items(lambda p: reply, PLAN, page)
    assert [it["titulo"] for it in items] == ["Piso calle Real"]


def test_same_listing_in_two_portals_is_shown_once():
    a = {"titulo": "Piso Venus", "datos": {"precio": "95.000 €", "m2": "56"}, "url": "https://idealista.com/1",
         "fuente": "idealista.com", "encaje": 10, "nota": ""}
    b = {**a, "titulo": "Casa en La Victoria", "datos": {"precio": "95.000\u00a0€", "m2": "56"},
         "url": "https://yaencontre.com/9", "fuente": "yaencontre.com"}
    c = {**a, "titulo": "Otro piso", "datos": {"precio": "120.000 €", "m2": "70"}, "url": "https://idealista.com/2"}
    out = deep_search._dedupe([a, b, c, dict(a)])
    assert [it["url"] for it in out] == ["https://idealista.com/1", "https://idealista.com/2"]
    assert out[0]["tambien_en"] == ["yaencontre.com"]


def test_numbers_are_read_from_any_kind_of_value():
    cases = {"14.900 €": 14900, "1.150.000\u00a0€": 1150000, "146.000 km": 146000, "7990": 7990,
             "02/2020": 2020, "120 m²": 120, "3 habs": 3, 12000: 12000}
    for value, expected in cases.items():
        assert deep_search.parse_number(value) == expected, value
    assert deep_search.parse_number(None) is None and deep_search.parse_number("sin precio") is None


def test_numeric_limits_are_checked_by_code_not_by_the_model():
    limits = [{"campo": "precio", "min": None, "max": 12000}, {"campo": "km", "min": None, "max": 150000}]
    ok = {"datos": {"precio": "11.900 €", "km": "120.164 km"}}
    too_expensive = {"datos": {"precio": "25.000 €", "km": "135.000"}}
    no_km = {"datos": {"precio": "9.500 €"}}
    assert deep_search.within_limits(ok, limits) and deep_search.within_limits(no_km, limits)
    assert not deep_search.within_limits(too_expensive, limits)


def test_values_with_the_thousands_cut_off_are_not_trusted():
    limits = [{"campo": "precio", "min": None, "max": 12000}, {"campo": "km", "min": None, "max": 150000}]
    assert not deep_search.within_limits({"datos": {"precio": "18.2", "km": "90.000"}}, limits)
    assert not deep_search.within_limits({"datos": {"precio": "2.200 €", "km": "237 kms"}}, limits)
    assert deep_search.within_limits({"datos": {"precio": "2.200 €", "km": "108.000 kms"}}, limits)
    # limites pequeños (habitaciones) no se tocan
    assert deep_search.within_limits({"datos": {"hab": "2"}}, [{"campo": "hab", "min": None, "max": 4}])


def test_a_link_that_does_not_match_the_title_is_not_trusted():
    links = [("Mi cuenta", "https://a.es/account"), ("Kia Sportage 1.6 T-GDi 2022 Drive", "https://a.es/kia")]
    assert deep_search._pick_link(links, 0, "Kia Sportage 1.6 T-GDi MHEV") == "https://a.es/kia"
    assert deep_search._pick_link(links, 1, "Kia Sportage") == "https://a.es/kia"
    assert deep_search._pick_link(links, 0, "Volvo V60") is None


def test_limits_the_user_did_not_write_are_ignored():
    campos = ["precio", "habitaciones"]
    raw = [{"campo": "precio", "min": 50000, "max": 200000}, {"campo": "habitaciones", "min": 3, "max": None}]
    assert deep_search._clean_limits(raw, campos, "casas en La Victoria") == []
    assert deep_search._clean_limits(raw, campos, "casas de hasta 200.000 euros con 3 habitaciones") == [
        {"campo": "precio", "min": None, "max": 200000.0}, {"campo": "habitaciones", "min": 3.0, "max": None}]
    assert deep_search._clean_limits([{"campo": "precio", "max": 200000}], campos, "hasta 200 mil") == [
        {"campo": "precio", "min": None, "max": 200000.0}]


def _item(titulo, **datos):
    return {"titulo": titulo, "datos": datos, "url": "u-" + titulo, "fuente": "f.es", "encaje": 10, "nota": ""}


def test_results_of_another_kind_are_dropped():
    """Pidio casas y salian pisos (2026-09-29)."""
    kind = {"si": ["casa", "chalet", "adosado", "villa"], "no": ["piso", "apartamento", "atico"]}
    assert deep_search.matches_kind(_item("Casa adosada con 2 baños en Girón"), kind)
    assert deep_search.matches_kind(_item("Villa en Girón"), kind)
    assert deep_search.matches_kind(_item("Obra nueva en Girón"), kind)  # no dice nada: se deja
    assert not deep_search.matches_kind(_item("Piso en Calle Dársena, 34, La Victoria"), kind)
    assert not deep_search.matches_kind(_item("Ático con terraza"), kind)
    assert deep_search.matches_kind(_item("Casa o piso en La Victoria"), kind)
    assert deep_search.matches_kind(_item("Piso en Girón"), {"si": [], "no": []})


def test_the_same_listing_written_differently_is_shown_once():
    a = _item("Casa adosada con 2 baños", precio="260.000 €", habitaciones="3", m2="169", barrio="Girón")
    b = {**_item("Villa en Girón", precio="260000", habitaciones="3 habs", m2="169 m²",
                 barrio="Valladolid Capital, Zona de Girón", descripcion="TECNOCASA Agencia Inmobiliaria"),
         "fuente": "trovit.es"}
    c = _item("Otra casa", precio="199.900", habitaciones="3", m2="120")
    out = deep_search._dedupe([a, b, c])
    assert [it["titulo"] for it in out] == ["Casa adosada con 2 baños", "Otra casa"]
    assert out[0]["tambien_en"] == ["trovit.es"]


def test_a_limit_named_a_bit_differently_still_applies():
    campos = ["precio", "habitaciones", "barrio"]
    limits = deep_search._clean_limits([{"campo": "habitaciones mínimas", "min": 3}], campos, "al menos 3 habitaciones")
    assert limits == [{"campo": "habitaciones", "min": 3.0, "max": None}]


def test_numeric_limits_are_read_from_what_the_user_wrote():
    campos = ["precio", "habitaciones", "barrio", "m2", "kilometros"]
    lim = lambda text: {l["campo"]: (l["min"], l["max"]) for l in deep_search._limits_from_text(text, campos)}
    assert lim("casas con un precio máximo de 260.000 euros y al menos 3 habitaciones") == {
        "precio": (None, 260000), "habitaciones": (3, None)}
    assert lim("coche de menos de 12000 euros y menos de 150000 km") == {"precio": (None, 12000), "kilometros": (None, 150000)}
    assert lim("pisos entre 80 y 120 m2 hasta 200 mil euros") == {"m2": (80, 120), "precio": (None, 200000)}
    assert lim("casas baratas en Girón") == {}


def test_what_the_user_wrote_wins_over_what_the_model_proposes():
    merged = deep_search._merge_limits([{"campo": "precio", "min": None, "max": 260000.0}],
                                       [{"campo": "precio", "min": 50000.0, "max": 200000.0},
                                        {"campo": "m2", "min": 90.0, "max": None}])
    assert merged == [{"campo": "precio", "min": None, "max": 260000.0}, {"campo": "m2", "min": 90.0, "max": None}]


def test_generic_words_do_not_count_as_the_kind():
    assert deep_search._clean_kind({"si": ["Casas", "viviendas", "inmuebles"], "no": ["piso", "casas"]}) == {
        "si": ["casas"], "no": ["piso"]}


def test_the_kind_is_decided_by_how_the_title_starts():
    kind = {"si": ["casa", "chalet", "villa"], "no": ["piso", "apartamento"]}
    assert not deep_search.matches_kind(_item("Piso en Calle del Quejigo, Girón - Villa del Prado, Valladolid"), kind)
    assert deep_search.matches_kind(_item("Casa adosada en Girón, cerca de pisos nuevos"), kind)
