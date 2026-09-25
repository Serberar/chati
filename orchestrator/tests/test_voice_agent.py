"""
Pruebas del agente de voz con los modelos reales (Piper + faster-whisper),
no mocks: lo que se prueba es que el redondeo texto -> voz -> texto conserva
el significado, que es la garantia real que nos importa. La primera carga de
los modelos tarda unos segundos, por eso va marcado como 'slow'.
"""

import pytest

from agents.voice_agent import VoiceAgent


@pytest.fixture(scope="module")
def voice_agent():
    return VoiceAgent()


@pytest.mark.slow
def test_tts_produces_valid_audio(tmp_path, voice_agent):
    out_path = tmp_path / "test.wav"
    voice_agent.speak("Hola, esto es una prueba.", str(out_path))

    assert out_path.exists()
    assert out_path.stat().st_size > 1000  # un wav real, no un archivo vacio o corrupto


@pytest.mark.slow
def test_tts_then_stt_roundtrip_preserves_meaning(tmp_path, voice_agent):
    text = "El cielo esta despejado hoy en Zaragoza."
    audio_path = tmp_path / "roundtrip.wav"
    voice_agent.speak(text, str(audio_path))

    transcript = voice_agent.transcribe(str(audio_path))

    assert transcript, "deberia devolver algo, no una cadena vacia"
    # no exigimos coincidencia exacta (STT no es perfecto), si que las
    # palabras clave sobrevivan al redondeo texto->voz->texto
    lowered = transcript.lower()
    assert "cielo" in lowered
    assert "zaragoza" in lowered
