from unittest.mock import MagicMock

import pytest

import prompt_writer


def _ollama(answer):
    ollama = MagicMock()
    ollama.chat.return_value = answer
    return ollama


def test_image_prompt_reads_description_and_format():
    ollama = _ollama('{"prompt": "A realistic photo of a dog in the snow.", "formato": "vertical"}')
    prompt, size = prompt_writer.image_prompt(ollama, "qwen3:8b", "un perro en la nieve")
    assert prompt == "A realistic photo of a dog in the snow."
    assert size == (832, 1216)
    assert "un perro en la nieve" in ollama.chat.call_args.args[1][0]["content"]


@pytest.mark.parametrize("answer", ["no se", '{"formato": "rarisimo"}', '{"prompt": ""}'])
def test_image_prompt_falls_back_to_the_request_as_is(answer):
    prompt, size = prompt_writer.image_prompt(_ollama(answer), "qwen3:8b", "un perro")
    assert prompt == "un perro"
    assert size == (1024, 1024)


@pytest.mark.parametrize("text,expected", [
    ("A realistic phone wallpaper featuring mountains, soft light.", "A realistic image of mountains, soft light."),
    ("A friendly dragon for my nephew, cartoon style.", "A friendly dragon, cartoon style."),
    ("A cake for my little sister with candles.", "A cake with candles."),
    ("A bouquet of roses for the table.", "A bouquet of roses for the table."),
])
def test_purpose_phrases_are_removed(text, expected):
    assert prompt_writer._clean(text) == expected


def test_video_prompt_from_a_photo_asks_to_keep_the_people():
    ollama = _ollama('{"prompt": "The people in the image dance slowly."}')
    assert prompt_writer.video_prompt(ollama, "qwen3:8b", "anímanos bailando", from_image=True) == \
        "The people in the image dance slowly."
    assert "FOTO que ya existe" in ollama.chat.call_args.args[1][0]["content"]


@pytest.mark.live
@pytest.mark.parametrize("request_text,size", [
    ("un perro en la nieve", (1024, 1024)),
    ("hazme un fondo de pantalla para el movil con montañas", (832, 1216)),
    ("un paisaje panoramico de la costa", (1216, 832)),
])
def test_image_prompt_with_the_real_model(request_text, size):
    from agents.ollama_client import OllamaClient
    prompt, got = prompt_writer.image_prompt(OllamaClient("http://127.0.0.1:11434"), "qwen3:8b", request_text)
    assert got == size
    assert prompt.isascii() and len(prompt.split()) >= 8
    assert "wallpaper" not in prompt.lower()
