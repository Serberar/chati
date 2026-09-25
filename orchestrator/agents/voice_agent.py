import wave

from faster_whisper import WhisperModel
from piper import PiperVoice

from paths import MODELS_DIR

PIPER_MODEL = MODELS_DIR / "voice" / "piper" / "es_ES-davefx-medium.onnx"


class VoiceAgent:
    def __init__(self):
        self._whisper = None
        self._piper = None

    @property
    def whisper(self) -> WhisperModel:
        if self._whisper is None:
            # cpu porque la GPU la necesitan imagen/video. large-v3-turbo en vez de
            # medium: probado en vivo 2026-09-24, mas preciso (acierta "Covarrubias"
            # y "microservicios" donde medium fallaba) Y mas rapido en transcripcion
            # (~5.8s vs ~7.5s en la misma muestra) pese a ser un modelo mayor, porque
            # turbo tiene menos capas de decoder.
            self._whisper = WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
        return self._whisper

    @property
    def piper(self) -> PiperVoice:
        if self._piper is None:
            self._piper = PiperVoice.load(str(PIPER_MODEL))
        return self._piper

    def transcribe(self, audio_path: str) -> str:
        segments, _info = self.whisper.transcribe(audio_path, language="es")
        return " ".join(seg.text.strip() for seg in segments).strip()

    def speak(self, text: str, out_path: str) -> None:
        with wave.open(out_path, "wb") as wav_file:
            self.piper.synthesize_wav(text, wav_file)
