import json

import pytest

import shopping
import web_tools


def test_product_data_reads_price_and_rating_from_schema_org():
    ld = json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage"},
        {"@type": "Product", "name": "Auriculares X", "aggregateRating": {"ratingValue": "4.5", "reviewCount": "120"},
         "offers": {"@type": "Offer", "price": "199,90", "priceCurrency": "EUR",
                    "availability": "https://schema.org/InStock"}}]})
    assert web_tools.product_data(["no es json", ld]) == {
        "nombre": "Auriculares X", "precio": 199.9, "moneda": "EUR", "valoracion": 4.5, "opiniones": 120,
        "disponible": True}
    assert web_tools.product_data([json.dumps({"@type": "Article"})]) == {}


def test_clean_request_validates_prices():
    req = shopping.clean_request({"descripcion": "  gafas  ", "precio_min": "10,5", "precio_max": ""})
    assert req["descripcion"] == "gafas" and req["precio_min"] == 10.5 and req["precio_max"] is None
    with pytest.raises(ValueError):
        shopping.clean_request({"precio_min": 50, "precio_max": 10})
    with pytest.raises(ValueError):
        shopping.clean_request({"precio_max": "caro"})


def _offer(**kw):
    base = {"url": "u", "nombre": "x", "tienda": "t", "precio": 100.0, "moneda": "EUR", "envio_espana": "si",
            "coste_envio": 0.0, "estado": "nuevo", "disponible": True, "valoracion": None, "opiniones": None,
            "encaje": 9, "imagen": None}
    return {**base, **kw}


def test_filters_sort_by_total_price_and_flag_suspicious_prices():
    req = shopping.clean_request({"precio_max": 300, "solo_espana": True, "valoraciones": True})
    offers = [
        _offer(url="caro", precio=250.0),
        _offer(url="barato+envio", precio=180.0, coste_envio=30.0),
        _offer(url="barato", precio=190.0),
        _offer(url="chollo", precio=20.0),                       # < 40% de la mediana
        _offer(url="fuera-de-rango", precio=400.0),
        _offer(url="no-envia", precio=150.0, envio_espana="no"),
        _offer(url="mal-valorado", precio=160.0, valoracion=2.1),
        _offer(url="no-es-eso", precio=170.0, encaje=3),
        _offer(url="sin-precio", precio=None),
        _offer(url="agotado", precio=100.0, disponible=False),
    ]
    kept = shopping.apply_filters(offers, req)
    assert [o["url"] for o in kept] == ["chollo", "barato", "barato+envio", "caro", "agotado"]
    assert kept[0]["sospechoso"] and not kept[1]["sospechoso"]


class FakeBrowser:
    def __init__(self, pages, results):
        self.pages, self.results = pages, results

    def bing(self, query, n):
        return self.results

    def fetch_page(self, url):
        text, links = self.pages[url]
        return {"url": url, "text": text, "links": links, "image": None, "product": {}}


def _eval(tipo="producto", **kw):
    data = {"tipo": tipo, "nombre": "Auriculares X", "tienda": "Tienda", "precio": 199, "moneda": "EUR",
            "envio_espana": "si", "coste_envio": 0, "estado": "nuevo", "disponible": True, "valoracion": 4.5,
            "opiniones": 10, "encaje": 9}
    return json.dumps({**data, **kw})


def test_run_search_follows_comparators_filters_and_recommends():
    pages = {
        "https://comparador/x": ("COMPARADOR", [("Auriculares X en Tienda B por 180 €", "https://b/x")]),
        "https://a/x": ("TIENDA A", []),
        "https://b/x": ("TIENDA B", []),
        "https://blog/x": ("ANALISIS", []),
    }

    def chat(prompt):
        if "Prepara la busqueda" in prompt:
            return '{"producto": "Auriculares X", "busquedas": ["auriculares x"], "claves": ["inalambricos"]}'
        if "numero: texto" in prompt:
            return '{"productos": [0]}'
        if "Elige las 2 o 3" in prompt:
            return '{"recomendadas": [{"n": 0, "por_que": "la mas barata"}]}'
        if "COMPARADOR" in prompt:
            return _eval(tipo="listado")
        if "ANALISIS" in prompt:
            return _eval(tipo="otra")
        if "TIENDA B" in prompt:
            return _eval(precio=180, tienda="B")
        return _eval(precio=199, tienda="A")

    browser = FakeBrowser(pages, ["https://comparador/x", "https://a/x", "https://blog/x",
                                  "https://www.youtube.com/watch?v=1"])
    res = shopping.run_search(chat, browser, shopping.clean_request({"descripcion": "auriculares x"}),
                              lambda *a: None, lambda: False)
    assert [o["tienda"] for o in res["ofertas"]] == ["B", "A"]
    assert res["recomendadas"] == [{"url": "https://b/x", "por_que": "la mas barata"}]
    assert res["revisadas"] == 2


def test_run_search_needs_something_to_look_for():
    with pytest.raises(ValueError):
        shopping.run_search(lambda p: "{}", FakeBrowser({}, []), shopping.clean_request({}),
                            lambda *a: None, lambda: False)


def test_start_needs_a_description_or_a_photo():
    with pytest.raises(ValueError, match="foto"):
        shopping.start("u1", b"k" * 32, 1, {"descripcion": " "}, lambda p: "")


def test_prices_read_without_the_decimal_point_are_fixed_or_dropped():
    offers = [_offer(url="a", precio=249.0), _offer(url="b", precio=259.0), _offer(url="c", precio=255.0),
              _offer(url="amazon", precio=24900.0),   # "249€00" leido de corrido
              _offer(url="basura", precio=1210.0)]    # ni asi tiene sentido
    kept = shopping.apply_filters(offers, shopping.clean_request({}))
    assert {o["url"]: o["precio"] for o in kept} == {"a": 249.0, "amazon": 249.0, "c": 255.0, "b": 259.0}


def test_only_exact_matches_are_recommended_when_there_are_enough():
    offers = [_offer(url="imitacion", precio=50.0, encaje=7), _offer(url="exacto1", precio=200.0, encaje=10),
              _offer(url="exacto2", precio=210.0, encaje=9)]
    seen = []

    def chat(prompt):
        seen.append(prompt)
        return '{"recomendadas": [{"n": 0, "por_que": "x"}]}'
    rec = shopping.recommend(chat, {"producto": "p"}, offers, True)
    assert rec == [{"url": "exacto1", "por_que": "x"}]
    assert "imitacion" not in seen[0] and "50.0" not in seen[0]


def test_product_images_never_point_inside_the_house():
    # la tienda elige la imagen y la carga el navegador del usuario (auditoria 2026-10-05)
    assert shopping._public_image("https://8.8.8.8/foto.jpg") == "https://8.8.8.8/foto.jpg"
    for bad in ("https://192.168.1.1/reboot", "http://8.8.8.8/x.jpg", "https://localhost/x.jpg",
                "javascript:alert(1)", None):
        assert shopping._public_image(bad) is None, bad
