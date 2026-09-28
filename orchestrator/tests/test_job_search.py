import base64
import json

import pytest

import job_search


def test_clean_prefs_accepts_a_bare_site_address_and_rejects_nonsense():
    prefs = job_search.clean_prefs({"puestos": ["  contable ", "", "x" * 100], "lugar": "Valencia",
                                    "sites": [{"url": "www.infoempleo.com/"}]})
    assert prefs["puestos"] == ["contable", "x" * 60]
    assert prefs["sites"] == [{"name": "infoempleo.com", "url": "infoempleo.com", "enabled": True}]
    with pytest.raises(ValueError):
        job_search.clean_prefs({"sites": [{"url": "no es una web"}]})


def test_score_is_computed_from_separate_criteria():
    good = job_search.score_offer({"encaje": 9, "requisitos": "cumple", "zona": "si"})
    far = job_search.score_offer({"encaje": 9, "requisitos": "cumple", "zona": "no"})
    weak = job_search.score_offer({"encaje": 3, "requisitos": "no cumple", "zona": "si"})
    assert good > far and good > weak and 0 <= weak <= 100
    assert job_search.score_offer({"encaje": "nada"}) >= 0


def test_bing_redirect_links_are_turned_into_the_real_address():
    real = "https://www.infoempleo.com/ofertasdetrabajo/contable/valencia/123/"
    encoded = base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    assert job_search._unbing(f"https://www.bing.com/ck/a?!&&p=x&u=a1{encoded}&ntb=1") == real
    assert job_search._unbing(real) == real


def test_pick_offer_links_maps_the_numbers_back_to_urls():
    links = [("Politica de privacidad del sitio", "https://x/p"), ("Administrativo contable - Valencia", "https://x/o1"),
             ("corto", "https://x/c"), ("Auxiliar de facturacion en Paterna", "https://x/o2")]
    chat = lambda prompt: '{"ofertas": [1, 2, 99]}'
    assert job_search.pick_offer_links(chat, links, 5) == ["https://x/o1", "https://x/o2"]


def _eval_reply(tipo="oferta", titulo="Contable", empresa="ACME", encaje=8, zona="si"):
    return json.dumps({"tipo": tipo, "titulo": titulo, "empresa": empresa, "ubicacion": "Valencia",
                       "encaje": encaje, "requisitos": "cumple", "zona": zona, "motivos": "encaja"})


class FakeBrowser:
    def __init__(self, pages, blocked=()):
        self.pages, self.blocked = pages, set(blocked)

    def fetch(self, url):
        if url in self.blocked:
            raise job_search.Blocked(url)
        text, links = self.pages[url]
        return url, text, links

    def bing(self, query, n):
        return []


PREFS = {**job_search.DEFAULT_PREFS, "puestos": ["contable"], "lugar": "Valencia", "internet": False,
         "max_ofertas": 5, "sites": [{"name": "Web", "url": "https://web/buscar?q={puesto}", "enabled": True},
                                     {"name": "Bloqueada", "url": "https://bloq/?q={puesto}", "enabled": True}]}


def test_run_search_scores_follows_listings_dedupes_and_reports_blocked_sites():
    pages = {
        "https://web/buscar?q=contable": ("resultados", [("Oferta contable uno", "https://web/o1"),
                                                         ("Listado de ofertas en Valencia", "https://web/lista"),
                                                         ("Oferta contable repetida", "https://web/o1-copia")]),
        "https://web/o1": ("OFERTA1", []),
        "https://web/o1-copia": ("OFERTA1 COPIA", []),
        "https://web/lista": ("LISTA", [("Oferta contable tres", "https://web/o3")]),
        "https://web/o3": ("OFERTA3", []),
    }

    def chat(prompt):
        if "numero: texto" in prompt:  # elegir enlaces
            return '{"ofertas": [0, 1, 2]}' if "Oferta contable uno" in prompt else '{"ofertas": [0]}'
        if "Escribe una carta" in prompt:
            return "Estimada empresa: ..."
        if "OFERTA3" in prompt:
            return _eval_reply(titulo="Contable senior", encaje=10)
        if "LISTA" in prompt:
            return _eval_reply(tipo="listado")
        return _eval_reply(encaje=6)  # o1 y su copia: mismo puesto y empresa

    browser = FakeBrowser(pages, blocked={"https://bloq/?q=contable"})
    offers, warnings = job_search.run_search(chat, browser, "CV", PREFS, {"https://web/o1"},
                                             lambda *a: None, lambda: False)
    assert [o["url"] for o in offers] == ["https://web/o3", "https://web/o1"]  # mejor primero, sin la copia
    assert offers[0]["nueva"] and not offers[1]["nueva"]
    assert offers[0]["carta"]
    assert any("Bloqueada" in w and "robot" in w for w in warnings)


def test_run_search_can_be_cancelled():
    browser = FakeBrowser({"https://web/buscar?q=contable": ("x", [])})
    with pytest.raises(job_search.Cancelled):
        job_search.run_search(lambda p: "{}", browser, "CV", PREFS, set(), lambda *a: None, lambda: True)


def test_start_needs_a_cv(monkeypatch):
    monkeypatch.setattr(job_search.profile_store, "cv_filename", lambda user_id=None: None)
    monkeypatch.setattr(job_search.profile_store, "get_cv_bytes", lambda *a, **k: None)
    with pytest.raises(ValueError, match="CV"):
        job_search.start("u1", b"k" * 32, 1, lambda p: "")
