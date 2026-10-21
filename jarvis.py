#!/usr/bin/env python3
"""
Desktop clap listener: reads the default microphone and logs when two loud transients
(a double clap) are detected within a short time window.

Run:
  python -m pip install -r requirements.txt
  python clap_listen.py

Tuning (constants below):
  SAMPLE_RATE   — usually 44100 or 48000; match your device if needed.
  BLOCK_MS      — analysis window size; smaller = snappier, noisier.
  SPIKE_RATIO   — how many times louder than the noise floor counts as a clap;
                    raise if false triggers; lower if claps are missed.
  COOLDOWN_S    — minimum seconds between double-clap logs (debounce).
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S — allowed time between the two claps.
  RETRIGGER_RATIO — audio must fall below threshold * this before another hit counts.
  NOISE_FLOOR_ALPHA — closer to 1 = slower baseline adaptation to room noise.
  MIN_RMS       — ignore spikes below this absolute level (float audio ~ [-1, 1]).
  SONG_URI      — Spotify or YouTube URL/URI to open on each double clap (empty = log only).
  FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP — if True, launch Cursor without -n (reuse / focus existing instance).
  OPEN_NEW_CURSOR_ON_DOUBLE_CLAP — if True, also launch Cursor with -n (extra new window; runs after focus launch if both).
  CURSOR_OPEN_FULLSCREEN — Windows: after focus/launch, send F11 to enter Cursor/VS Code-style fullscreen (toggle off with F11).
  OPEN_CLAUDE_CODE_IN_CHROME — Claude in Chrome after Spotify (CLAUDE_CODE_URL).
  OPEN_BINANCE_BTC_IN_CHROME — Binance BTC trade page in Chrome (BINANCE_BTC_URL).
  CLAUDE_CHROME_MONITOR / BINANCE_CHROME_MONITOR — 1-based display index (Windows: sorted left-to-top).
  CHROME_SEPARATE_SITE_PROFILES — Windows: if True, uses temp --user-data-dir per site (not your normal profile).
    Default False so Claude/Binance use your usual Chrome profile and logins; enable only if both windows keep
    opening on the same monitor and you accept a separate profile for automation.
  OPEN_CHROME_FULLSCREEN — Fullscreen on the chosen monitor (Windows: new window is detected and snapped with SetWindowPos).
  JARVIS_WELCOME_* — TTS after the song (ElevenLabs). Configure via environment or a `.env`
    file next to this script (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, etc.).
    With JARVIS_WELCOME_CACHE_ENABLED, audio is saved under `.cache/jarvis_welcome/` (WAV) and
    replayed when phrase + voice + model + format match—no repeat API call. Delete that folder
    or set JARVIS_WELCOME_CACHE_ENABLED=False to force a fresh fetch.
  The welcome sequence runs only once per process. The assistant speaks in the background so Cursor
    opens without waiting for playback to finish (restart the script to run again).
"""

from __future__ import annotations

import collections
import hashlib
import logging
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import wave
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd
import speech_recognition as sr

load_dotenv(Path(__file__).resolve().parent / ".env")

# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 7.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.012
QUIET_GATE_MULT = 2.2  # update noise floor only when below floor * this
# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# Volumizer / Audio Gain:
# Software multiplier and soft-knee limiter for TTS playback.
# 2.0 - 3.0 gives significantly higher perceived volume without distortion.
JARVIS_VOICE_GAIN = float(os.environ.get("JARVIS_VOICE_GAIN", "2.5"))

# Voice commands & speech recognition:
VOICE_COMMANDS_ENABLED = (
    os.environ.get("JARVIS_VOICE_COMMANDS_ENABLED", "true").lower() in ("1", "true", "yes")
)
JARVIS_VOICE_RESPONSES = (
    os.environ.get("JARVIS_VOICE_RESPONSES", "true").lower() in ("1", "true", "yes")
)
SPEECH_MIN_RMS = float(os.environ.get("JARVIS_SPEECH_MIN_RMS", "0.012"))

# Spotify: "spotify:track:TRACK_ID" or https://open.spotify.com/track/...
# YouTube: https://www.youtube.com/watch?v=...
SONG_URI = os.environ.get(
    "SONG_URI",
    "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=1457e0de8c9b4812",
)

# Workspace & App Settings for Double Clap
OPEN_CHROME = True
CHROME_URL = os.environ.get("CHROME_URL", "").strip()

OPEN_TERMINAL = True

OPEN_SPOTIFY = True
SPOTIFY_SPECIAL_WORKSPACE = os.environ.get("SPOTIFY_SPECIAL_WORKSPACE", "spotify").strip()

# Target workspace where Chrome and Terminal will open (e.g. "1" or "active")
JARVIS_MAIN_WORKSPACE = os.environ.get("JARVIS_MAIN_WORKSPACE", "3").strip()

# Cursor: set to True if you also want Cursor opened/focused
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = False
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = False
CURSOR_OPEN_FULLSCREEN = False

# Google Chrome extras (legacy: Claude & Binance in Chrome, disabled by default)
OPEN_CLAUDE_CODE_IN_CHROME = False
OPEN_BINANCE_BTC_IN_CHROME = False
OPEN_CHROME_FULLSCREEN = False
# False = default Chrome profile (your normal user, extensions, cookies). True = temp dirs under %TEMP% per site.
CHROME_SEPARATE_SITE_PROFILES = False
# Which physical screen (1 = leftmost/top-first after sorting). Windows only; ignored elsewhere.
CLAUDE_CHROME_MONITOR = 1
BINANCE_CHROME_MONITOR = 3

JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = (
    "Welcome home, sir. Congratulations on the opening ceremonies. They were such a success, as was your senate hearing. And may I say how refreshing it is to finally see you in a video with your clothing on, sir."
    )
# Seconds after launching SONG_URI before speaking (gives Spotify/browser time to start).
JARVIS_AFTER_SONG_DELAY_S = 1.0
# Save ElevenLabs PCM as WAV under .cache/jarvis_welcome/; replay skips the API when the key matches.
JARVIS_WELCOME_CACHE_ENABLED = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clap_listen")


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set JARVIS_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using JARVIS_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check Windows input level or try another JARVIS_INPUT_DEVICE.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None:
            log.info(
                "Using default microphone [%d]: %s",
                default,
                default_name,
            )
            return default
        log.warning(
            "Default mic [%d] %s could not be opened; scanning other inputs...",
            default,
            default_name,
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _jarvis_welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "jarvis_welcome"


def _jarvis_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _jarvis_welcome_cache_dir() / f"{digest}.wav"


jarvis_is_speaking = threading.Event()


def volumize_audio(pcm_f: np.ndarray, gain: float | None = None) -> np.ndarray:
    """
    Apply software volume boost / dynamic limiter to audio.
    gain can be configured via JARVIS_VOICE_GAIN (default: 2.5).
    Uses soft-knee compression to avoid harsh digital clipping while boosting loudness.
    """
    if gain is None:
        try:
            gain = float(os.environ.get("JARVIS_VOICE_GAIN", "2.5"))
        except ValueError:
            gain = 2.5

    if gain <= 0:
        return pcm_f

    boosted = pcm_f * gain
    peak = float(np.max(np.abs(boosted))) if boosted.size > 0 else 0.0
    if peak <= 0.95:
        return boosted

    # Soft-knee limiter: keep below 0.7 linear, smoothly compress peaks above 0.7
    abs_samples = np.abs(boosted)
    mask = abs_samples > 0.7
    if np.any(mask):
        excess = abs_samples[mask] - 0.7
        compressed = 0.7 + 0.28 * np.tanh(excess / max(peak - 0.7, 1e-6))
        boosted[mask] = np.sign(boosted[mask]) * compressed

    return np.clip(boosted, -0.99, 0.99)


def _play_audio_blocking(pcm_f: np.ndarray, rate: int) -> bool:
    """Play audio with volumizer applied and track jarvis_is_speaking status."""
    boosted = volumize_audio(pcm_f)
    jarvis_is_speaking.set()
    try:
        sd.play(boosted, rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play audio: %s", e)
        return False
    finally:
        # Give a small pause for echo / room reverberation to settle
        time.sleep(0.3)
        jarvis_is_speaking.clear()
    return True


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    return _play_audio_blocking(pcm_f, rate)


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def speak_phrase(text: str) -> None:
    """Asynchronously speak a phrase with ElevenLabs TTS (if configured)."""
    if not JARVIS_VOICE_RESPONSES or not text.strip():
        return
    threading.Thread(target=_speak_phrase_worker, args=(text,), daemon=True).start()


def _speak_phrase_worker(text: str) -> None:
    text = text.strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not vid or not api_key:
        return

    cache_path = _jarvis_welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        if _play_pcm_wav_file(cache_path):
            return

    try:
        from elevenlabs.client import ElevenLabs

        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid,
            text=text,
            model_id=model_id,
            output_format=output_format,
        )
        raw = b"".join(chunks)
        if not raw:
            return
        if JARVIS_WELCOME_CACHE_ENABLED:
            try:
                _save_pcm_wav_file(cache_path, raw, pcm_rate)
            except OSError:
                pass
        pcm_i16 = np.frombuffer(raw, dtype=np.int16)
        pcm_f = pcm_i16.astype(np.float32) / 32768.0
        _play_audio_blocking(pcm_f, pcm_rate)
    except Exception as e:
        log.warning("Voice response failed: %s", e)


def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return
    text = JARVIS_WELCOME_PHRASE.strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return

    cache_path = _jarvis_welcome_cache_path(text, vid, model_id, output_format)
    if JARVIS_WELCOME_CACHE_ENABLED and cache_path.is_file():
        log.info("Playing welcome from cache: %s", cache_path)
        if _play_pcm_wav_file(cache_path):
            return
        log.warning("Cache miss after read failure; fetching from ElevenLabs.")

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set ELEVENLABS_API_KEY in the environment for ElevenLabs TTS.")
        return
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return
    try:
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid,
            text=text,
            model_id=model_id,
            output_format=output_format,
        )
        raw = b"".join(chunks)
    except Exception as e:
        log.warning("ElevenLabs TTS failed: %s", e)
        return
    if not raw:
        log.warning("ElevenLabs returned empty audio.")
        return
    if JARVIS_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved welcome audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save welcome cache: %s", e)
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    _play_audio_blocking(pcm_f, pcm_rate)


def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    try:
        if sys.platform == "win32":
            os.startfile(u)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    elif sys.platform == "darwin":
        mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(mac_chrome):
            return mac_chrome
    return (
        shutil.which("google-chrome-stable")
        or shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("chrome")
    )


def _is_hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def _hyprctl(args: list[str]) -> str:
    try:
        res = subprocess.run(
            ["hyprctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout
    except Exception as e:
        log.warning("hyprctl command failed: %s", e)
        return ""


def _is_special_spotify_visible() -> bool:
    if not _is_hyprland():
        return False
    try:
        import json
        out = _hyprctl(["monitors", "-j"])
        monitors = json.loads(out)
        for m in monitors:
            special_name = m.get("specialWorkspace", {}).get("name", "")
            if special_name == f"special:{SPOTIFY_SPECIAL_WORKSPACE}":
                return True
    except Exception as e:
        log.debug("Error checking special workspace status: %s", e)
    return False


def open_terminal() -> None:
    home_dir = str(Path.home())
    if sys.platform == "win32":
        term = shutil.which("wt.exe") or shutil.which("powershell.exe") or shutil.which("cmd.exe")
        if term:
            try:
                log.info("Launching Windows terminal: %s", term)
                subprocess.Popen([term], cwd=home_dir)
                return
            except OSError as e:
                log.warning("Could not open Windows terminal: %s", e)
    elif sys.platform == "darwin":
        for app in ("iTerm", "Terminal"):
            try:
                log.info("Launching macOS terminal: %s", app)
                subprocess.Popen(["open", "-a", app])
                return
            except OSError:
                pass

    term = (
        os.environ.get("TERMINAL")
        or shutil.which("kitty")
        or shutil.which("alacritty")
        or shutil.which("foot")
        or shutil.which("wezterm")
        or shutil.which("gnome-terminal")
        or shutil.which("x-terminal-emulator")
    )
    if not term:
        log.warning("No terminal emulator found.")
        return

    ws = (
        JARVIS_MAIN_WORKSPACE
        if (JARVIS_MAIN_WORKSPACE and JARVIS_MAIN_WORKSPACE.lower() != "active")
        else ""
    )

    if _is_hyprland() and ws:
        if "kitty" in os.path.basename(term).lower():
            cmd = f"[workspace {ws}] {term} --directory {home_dir}"
        else:
            cmd = f"[workspace {ws}] {term}"
        log.info("Launching terminal via Hyprland on workspace %s: %s", ws, cmd)
        _hyprctl(["dispatch", "exec", cmd])
        return

    args = [term]
    if "kitty" in os.path.basename(term).lower():
        args.extend(["--directory", home_dir])

    log.info("Launching terminal: %s (cwd=%s)", term, home_dir)
    try:
        subprocess.Popen(
            args,
            cwd=home_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning("Could not open terminal: %s", e)


def open_chrome_window(url: str | None = None) -> None:
    chrome = _chrome_executable()
    if not chrome:
        log.warning("Chrome executable not found.")
        return

    home_dir = str(Path.home())
    ws = (
        JARVIS_MAIN_WORKSPACE
        if (JARVIS_MAIN_WORKSPACE and JARVIS_MAIN_WORKSPACE.lower() != "active")
        else ""
    )
    url_arg = f" {url.strip()}" if (url and url.strip()) else ""

    if _is_hyprland() and ws:
        cmd = f"[workspace {ws}] {chrome} --new-window{url_arg}"
        log.info("Launching Chrome via Hyprland on workspace %s: %s", ws, cmd)
        _hyprctl(["dispatch", "exec", cmd])
        return

    args = [chrome, "--new-window"]
    if url and url.strip():
        args.append(url.strip())
    log.info("Launching Chrome: %s", " ".join(args))
    try:
        subprocess.Popen(
            args,
            cwd=home_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning("Could not open Chrome: %s", e)


def open_in_browser(urls: list[str] | str) -> None:
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        return

    chrome = _chrome_executable()
    if chrome:
        try:
            log.info("Opening URL(s) in Chrome: %s", " ".join(urls))
            subprocess.Popen(
                [chrome] + urls,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError as e:
            log.warning("Could not launch Chrome for URLs: %s", e)

    log.info("Opening URLs in default browser: %s", urls)
    for u in urls:
        webbrowser.open(u)


def search_google(query: str) -> None:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}"
    log.info("Searching Google for query: %r (%s)", query, url)
    open_in_browser(url)


JARVIS_GREETING_RESPONSES = [
    "At your service, sir.",
    "Yes, sir?",
    "How may I assist you, sir?",
    "Always at your service, sir.",
    "Online and ready, sir.",
]

_last_greeting_index = -1


def get_next_greeting_phrase() -> str:
    global _last_greeting_index
    available = [
        i for i in range(len(JARVIS_GREETING_RESPONSES)) if i != _last_greeting_index
    ]
    idx = random.choice(available)
    _last_greeting_index = idx
    return JARVIS_GREETING_RESPONSES[idx]


def parse_voice_command(text: str) -> dict | None:
    raw = text.strip()
    if not raw:
        return None
    lower = raw.lower()

    # 1. Clean wake words / polite prefixes
    cleaned_lower = lower
    wake_prefixes = (
        "hey jarvis",
        "ok jarvis",
        "hello jarvis",
        "hi jarvis",
        "yo jarvis",
        "jarvis",
        "can you please",
        "could you please",
        "can you",
        "could you",
        "please",
    )
    is_wake_called = False
    for prefix in wake_prefixes:
        if cleaned_lower == prefix:
            is_wake_called = True
            cleaned_lower = ""
            break
        elif cleaned_lower.startswith(prefix):
            rest = cleaned_lower[len(prefix):]
            if rest and rest[0] in " ,.:;?!":
                is_wake_called = True
                cleaned_lower = rest.strip(" ,.:;?!")
                break

    offset = len(lower) - len(cleaned_lower)
    raw_cleaned = raw[offset:].strip(" ,.:;?!")

    # If only the wake word or an attention call was spoken
    greeting_fillers = (
        "",
        "hello",
        "hi",
        "hey",
        "are you there",
        "you there",
        "can you hear me",
        "hear me",
    )
    if is_wake_called and cleaned_lower in greeting_fillers:
        return {
            "type": "greeting",
            "speech": get_next_greeting_phrase(),
            "original": raw,
        }

    # 2. News command: check for 'news' in the phrase
    if re.search(r"\bnews\b", cleaned_lower):
        return {
            "type": "news",
            "urls": ["https://github.com/feed", "https://youtube.com"],
            "speech": "Opening your News feeds, sir.",
            "original": raw,
        }

    # 3. Explicit look up / search commands:
    search_prefixes = (
        "look up",
        "search for",
        "search",
        "google for",
        "google",
        "find out",
        "tell me about",
        "explain",
    )
    for prefix in search_prefixes:
        if cleaned_lower == prefix or cleaned_lower.startswith(prefix + " "):
            query = raw_cleaned[len(prefix):].strip(" ,.:;?!")
            if not query:
                query = raw_cleaned
            return {
                "type": "search",
                "query": query,
                "speech": "Looking that up for you, sir.",
                "original": raw,
            }

    # 4. Questions: check if ANY question word appears in words
    question_starters = {
        "what",
        "what's",
        "whats",
        "why",
        "why's",
        "whys",
        "how",
        "how's",
        "hows",
        "who",
        "who's",
        "whos",
        "where",
        "where's",
        "wheres",
        "when",
        "when's",
        "whens",
        "which",
    }
    words = re.findall(r"[a-zA-Z0-9']+", cleaned_lower)
    if any(w in question_starters for w in words):
        query = raw_cleaned
        if cleaned_lower.startswith("tell me "):
            query = raw_cleaned[len("tell me "):].strip(" ,.:;?!")
        return {
            "type": "search",
            "query": query,
            "speech": "Searching Google, sir.",
            "original": raw,
        }

    return None


def handle_voice_command(text: str) -> bool:
    parsed = parse_voice_command(text)
    if not parsed:
        log.info("Voice utterance not recognized as command: %r", text)
        return False

    cmd_type = parsed["type"]
    log.info("Executing voice command [%s] for %r", cmd_type, parsed.get("original", text))

    if cmd_type == "greeting":
        if parsed.get("speech"):
            speak_phrase(parsed["speech"])
        return True

    if cmd_type == "news":
        open_in_browser(parsed["urls"])
        if parsed.get("speech"):
            speak_phrase(parsed["speech"])
        return True

    if cmd_type == "search":
        search_google(parsed["query"])
        if parsed.get("speech"):
            speak_phrase(parsed["speech"])
        return True

    return False


def speech_recognition_worker(q: queue.Queue[np.ndarray]) -> None:
    r = sr.Recognizer()
    r.energy_threshold = 300
    r.dynamic_energy_threshold = False
    while True:
        audio_chunk = q.get()
        try:
            pcm16 = (np.clip(audio_chunk, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            audio_source = sr.AudioData(pcm16, SAMPLE_RATE, 2)
            try:
                transcription = r.recognize_google(audio_source)
                log.info("Voice heard: %r", transcription)
                handle_voice_command(transcription)
            except sr.UnknownValueError:
                pass
            except sr.RequestError as e:
                log.warning("Google Speech Recognition network error: %s", e)
        except Exception as e:
            log.warning("Error in speech recognition worker: %s", e)
        finally:
            q.task_done()


def open_and_play_spotify(uri: str) -> None:
    if sys.platform == "darwin":
        try:
            log.info("Launching Spotify on macOS: %s", uri)
            subprocess.Popen(["open", "-a", "Spotify", uri])
            return
        except OSError:
            play_song(uri)
            return
    elif sys.platform == "win32":
        log.info("Launching Spotify on Windows: %s", uri)
        play_song(uri)
        return

    # 1. Ensure Spotify process is running (Linux)
    is_running = subprocess.run(
        ["pgrep", "-x", "spotify"],
        capture_output=True,
    ).returncode == 0

    if not is_running:
        log.info("Spotify not running; starting Spotify...")
        cache_pattern = str(Path.home() / ".cache/spotify/Singleton*")
        subprocess.run(f"rm -f {cache_pattern}", shell=True, check=False)
        try:
            subprocess.Popen(
                ["spotify"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.2)
        except OSError as e:
            log.warning("Could not launch Spotify: %s", e)

    # 2. On Hyprland, toggle special workspace if not already visible
    if _is_hyprland():
        if not _is_special_spotify_visible():
            log.info("Toggling special workspace '%s'", SPOTIFY_SPECIAL_WORKSPACE)
            _hyprctl(["dispatch", "togglespecialworkspace", SPOTIFY_SPECIAL_WORKSPACE])

    # 3. Play track via MPRIS / DBus
    u = uri.strip()
    track_uri = None
    if u.startswith("spotify:track:"):
        track_uri = u
    elif "open.spotify.com/track/" in u:
        track_id = u.split("open.spotify.com/track/")[1].split("?")[0]
        track_uri = f"spotify:track:{track_id}"

    if track_uri:
        time.sleep(0.4)
        res = subprocess.run(
            [
                "dbus-send",
                "--print-reply",
                "--dest=org.mpris.MediaPlayer2.spotify",
                "/org/mpris/MediaPlayer2",
                "org.mpris.MediaPlayer2.Player.OpenUri",
                f"string:{track_uri}",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            log.info("Sent track %s to Spotify via DBus", track_uri)
            return

    # Fallback to play command
    subprocess.run(
        [
            "dbus-send",
            "--dest=org.mpris.MediaPlayer2.spotify",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player.Play",
        ],
        capture_output=True,
    )


def _win32_sorted_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Each monitor as (left, top, right, bottom), sorted left-to-right then top-to-bottom."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    collected: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    def _cb(_hm, _hdc, lprc, _lp):
        r = lprc.contents
        collected.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    collected.sort(key=lambda t: (t[0], t[1]))
    return collected


def _chrome_monitor_top_left(one_based_index: int) -> tuple[int, int]:
    """Top-left corner on virtual desktop for monitor N (1-based)."""
    l, t, _, _ = _chrome_monitor_bounds(one_based_index)
    return (l, t)


def _chrome_monitor_bounds(one_based_index: int) -> tuple[int, int, int, int]:
    """Monitor N as (left, top, right, bottom), 1-based index (sorted like other Chrome helpers)."""
    rects = _win32_sorted_monitor_rects()
    if not rects:
        return (0, 0, 1920, 1080)
    idx = one_based_index - 1
    if idx < 0:
        idx = 0
    if idx >= len(rects):
        log.warning(
            "Monitor %d requested but only %d found; using last monitor.",
            one_based_index,
            len(rects),
        )
        idx = len(rects) - 1
    return rects[idx]


def _chrome_monitor_pixel_size(one_based_index: int) -> tuple[int, int]:
    l, t, r, b = _chrome_monitor_bounds(one_based_index)
    return (max(320, r - l), max(240, b - t))


def _chrome_window_size() -> tuple[int, int]:
    w = (os.environ.get("CHROME_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("CHROME_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _chrome_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "clap-trigger-chrome" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _chrome_new_window_wait_timeout_s() -> float:
    try:
        return max(3.0, float((os.environ.get("CHROME_NEW_WINDOW_WAIT_S") or "25").strip()))
    except ValueError:
        return 25.0


def _chrome_top_level_browser_hwnds_win32() -> set[int]:
    """HWND ints for visible-or-minimized top-level Chrome browser windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "chrome.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 80 or h < 80:
            return True
        found.add(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _wait_new_chrome_hwnd_win32(before: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.12)
        now = _chrome_top_level_browser_hwnds_win32()
        new = now - before
        if not new:
            continue
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        best: int | None = None
        best_area = 0
        for h in new:
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)):
                a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if a > best_area:
                    best_area = a
                    best = h
        if best is not None:
            return best
    return None


def _chrome_snap_window_to_monitor_win32(
    hwnd: int,
    one_based_monitor: int,
    *,
    fullscreen: bool,
    windowed_size: tuple[int, int] | None,
) -> None:
    import ctypes
    from ctypes import wintypes

    ml, mt, mr, mb = _chrome_monitor_bounds(one_based_monitor)
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SW_SHOWMAXIMIZED = 3
    HWND_TOP = 0
    SWP_SHOWWINDOW = 0x0040
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_SHOWWINDOW | SWP_FRAMECHANGED

    user32.ShowWindow(hwnd, SW_RESTORE)
    if fullscreen:
        w, h = mr - ml, mb - mt
        x, y = ml, mt
    else:
        ww, wh = windowed_size or _chrome_window_size()
        w, h = ww, wh
        x = ml + max(0, (mr - ml - w) // 2)
        y = mt + max(0, (mb - mt - h) // 2)
    user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, flags)

    if fullscreen:
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        KEYEVENTF_KEYUP = 0x0002
        VK_F11 = 0x7A
        fg = user32.GetForegroundWindow()
        tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, True)
        user32.SetForegroundWindow(hwnd)
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, False)
        user32.keybd_event(VK_F11, 0, 0, 0)
        user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _open_url_in_chrome(
    url: str,
    *,
    new_window: bool = True,
    label: str = "URL",
    window_position: tuple[int, int] | None = None,
    window_size: tuple[int, int] | None = None,
    fullscreen: bool = False,
    win32_post_fullscreen_monitor: int | None = None,
    user_data_dir: str | None = None,
) -> None:
    u = url.strip()
    if not u:
        return
    chrome = _chrome_executable()
    try:
        if chrome:
            args = [chrome]
            if user_data_dir:
                args.append(f"--user-data-dir={user_data_dir}")
                args.append("--no-first-run")
            if new_window:
                args.append("--new-window")
            if window_position is not None:
                x, y = window_position
                args.append(f"--window-position={x},{y}")
            if window_size:
                args.append(f"--window-size={window_size[0]},{window_size[1]}")
            if fullscreen and not (
                sys.platform == "win32" and win32_post_fullscreen_monitor is not None
            ):
                args.append("--start-fullscreen")
            args.append(u)
            popen_kw: dict = {
                "args": args,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            before: set[int] | None = None
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                before = _chrome_top_level_browser_hwnds_win32()
            subprocess.Popen(**popen_kw)
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                mon = win32_post_fullscreen_monitor
                hwnd = _wait_new_chrome_hwnd_win32(before, _chrome_new_window_wait_timeout_s())
                if hwnd is not None:
                    _chrome_snap_window_to_monitor_win32(
                        hwnd,
                        mon,
                        fullscreen=fullscreen,
                        windowed_size=window_size if not fullscreen else None,
                    )
                else:
                    log.warning(
                        "Chrome: timed out waiting for new window (%s); check "
                        "CHROME_NEW_WINDOW_WAIT_S or close extra Chrome instances.",
                        label,
                    )
        else:
            log.warning("Chrome not found; opening %s in default browser.", label)
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open %s in Chrome: %s", label, e)


def open_claude_in_chrome() -> None:
    if not OPEN_CLAUDE_CODE_IN_CHROME:
        return
    url = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = OPEN_CHROME_FULLSCREEN
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = CLAUDE_CHROME_MONITOR
        pos = _chrome_monitor_top_left(CLAUDE_CHROME_MONITOR)
        if fs:
            size = _chrome_monitor_pixel_size(CLAUDE_CHROME_MONITOR)
        else:
            size = _chrome_window_size()
        if CHROME_SEPARATE_SITE_PROFILES:
            user_data = _chrome_site_user_data_dir("claude")
    elif not fs:
        size = _chrome_window_size()
    else:
        size = None
    _open_url_in_chrome(
        url,
        new_window=True,
        label="Claude",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )


def open_binance_btc_in_chrome() -> None:
    if not OPEN_BINANCE_BTC_IN_CHROME:
        return
    url = (
        os.environ.get("BINANCE_BTC_URL")
        or "https://www.binance.com/en/trade/BTC_USDT"
    ).strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = OPEN_CHROME_FULLSCREEN
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = BINANCE_CHROME_MONITOR
        pos = _chrome_monitor_top_left(BINANCE_CHROME_MONITOR)
        if fs:
            size = _chrome_monitor_pixel_size(BINANCE_CHROME_MONITOR)
        else:
            size = _chrome_window_size()
        if CHROME_SEPARATE_SITE_PROFILES:
            user_data = _chrome_site_user_data_dir("binance")
    elif not fs:
        size = _chrome_window_size()
    else:
        size = None
    _open_url_in_chrome(
        url,
        new_window=True,
        label="Binance BTC",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )


def _cursor_executable() -> str | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for sub in ("Programs\\cursor\\Cursor.exe", "Programs\\Cursor\\Cursor.exe"):
            if local:
                p = os.path.join(local, *sub.split("\\"))
                if os.path.isfile(p):
                    return p
    return shutil.which("cursor")


def _cursor_largest_main_hwnd_win32() -> int | None:
    """Largest top-level Cursor.exe window (visible or minimized)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    candidates: list[tuple[int, wintypes.HWND]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "cursor.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        candidates.append((w * h, hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    if not candidates:
        return None
    return int(max(candidates, key=lambda t: t[0])[1])


def _cursor_foreground_hwnd_win32(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, False)


def _cursor_send_f11_fullscreen_win32(hwnd: int) -> None:
    """F11 toggles Zen/fullscreen in Cursor (Electron)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    VK_F11 = 0x7A
    _cursor_foreground_hwnd_win32(hwnd)
    user32.keybd_event(VK_F11, 0, 0, 0)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _focus_existing_cursor_window_win32() -> bool:
    """Bring an existing Cursor.exe main window to the foreground (no new process)."""
    if sys.platform != "win32":
        return False
    hwnd = _cursor_largest_main_hwnd_win32()
    if hwnd is None:
        return False
    _cursor_foreground_hwnd_win32(hwnd)
    return True


def run_double_clap_actions() -> None:
    """Run outside the mic loop so sleeps do not stall capture."""
    # 1. Main workspace for Chrome and Terminal
    if _is_hyprland() and JARVIS_MAIN_WORKSPACE and JARVIS_MAIN_WORKSPACE.lower() != "active":
        log.info("Switching to main workspace: %s", JARVIS_MAIN_WORKSPACE)
        _hyprctl(["dispatch", "workspace", JARVIS_MAIN_WORKSPACE])
        time.sleep(0.15)

    if OPEN_CHROME:
        open_chrome_window(CHROME_URL)
        time.sleep(0.2)

    if OPEN_TERMINAL:
        open_terminal()
        time.sleep(0.5)

    # 2. Spotify in special workspace (overlaps on top)
    if OPEN_SPOTIFY:
        open_and_play_spotify(SONG_URI)
    elif SONG_URI.strip():
        play_song(SONG_URI)

    # 3. Jarvis voice welcome
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()

    # 4. Optional legacy actions (if enabled)
    if OPEN_CLAUDE_CODE_IN_CHROME:
        open_claude_in_chrome()
    if OPEN_BINANCE_BTC_IN_CHROME:
        open_binance_btc_in_chrome()
    if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP or OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        open_cursor_window()


def open_cursor_window() -> None:
    if not FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP and not OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        return
    exe = _cursor_executable()
    if not exe:
        log.warning(
            "Could not find Cursor (install app or add the `cursor` command to PATH)."
        )
        return
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
            focused = (
                sys.platform == "win32" and _focus_existing_cursor_window_win32()
            )
            if not focused:
                subprocess.Popen([exe], **popen_kw)
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            subprocess.Popen([exe, "-n"], **popen_kw)
    except OSError as e:
        log.warning("Could not start or focus Cursor: %s", e)
        return
    if sys.platform == "win32" and CURSOR_OPEN_FULLSCREEN:
        time.sleep(0.5)
        hwnd = _cursor_largest_main_hwnd_win32()
        if hwnd is not None:
            _cursor_send_f11_fullscreen_win32(hwnd)
        else:
            log.warning("Cursor fullscreen: no Cursor window found to send F11.")


def main() -> int:
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True
    welcome_sequence_done = False

    log.info(
        "Listening (double clap: %.2f–%.2fs apart, rate=%d, block=%d ms, "
        "spike_ratio=%.1f, cooldown=%.2fs). Ctrl+C to stop.",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SAMPLE_RATE,
        BLOCK_MS,
        SPIKE_RATIO,
        COOLDOWN_S,
    )
    if _is_hyprland():
        log.info(
            "Hyprland detected: Chrome and Terminal will open on workspace '%s'",
            JARVIS_MAIN_WORKSPACE,
        )
        if OPEN_SPOTIFY:
            log.info(
                "Spotify will open/toggle in special workspace 'special:%s' (overlapping)",
                SPOTIFY_SPECIAL_WORKSPACE,
            )
    else:
        if OPEN_CHROME:
            log.info("Double clap will open Chrome")
        if OPEN_TERMINAL:
            log.info("Double clap will open Terminal")
        if OPEN_SPOTIFY:
            log.info("Double clap will open Spotify")

    if SONG_URI.strip():
        log.info("Spotify track: %s", SONG_URI.strip())
    if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP or OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        log.info("Double clap will launch/focus Cursor.")
    if OPEN_CLAUDE_CODE_IN_CHROME:
        log.info("Double clap will open Claude in Chrome.")
    if OPEN_BINANCE_BTC_IN_CHROME:
        log.info("Double clap will open Binance in Chrome.")
    if JARVIS_WELCOME_ENABLED:
        ev, em, ef, er = elevenlabs_env_config()
        log.info(
            "After song + %.2fs: %r (ElevenLabs voice=%s, model=%s, format=%s, pcm_rate=%d)",
            JARVIS_AFTER_SONG_DELAY_S,
            JARVIS_WELCOME_PHRASE.strip(),
            ev or "(unset)",
            em,
            ef,
            er,
        )

    input_idx = _choose_input_device(blocksize)

    pre_buffer_len = max(int(0.3 / (BLOCK_MS / 1000)), 3)
    pre_buffer: collections.deque[np.ndarray] = collections.deque(maxlen=pre_buffer_len)

    speech_frames: list[np.ndarray] = []
    is_speaking = False
    silence_blocks = 0
    silence_blocks_threshold = max(int(0.65 / (BLOCK_MS / 1000)), 12)
    min_speech_blocks = max(int(0.35 / (BLOCK_MS / 1000)), 8)
    max_speech_blocks = int(10.0 / (BLOCK_MS / 1000))

    speech_queue: queue.Queue[np.ndarray] = queue.Queue()
    if VOICE_COMMANDS_ENABLED:
        threading.Thread(
            target=speech_recognition_worker,
            args=(speech_queue,),
            daemon=True,
        ).start()
        log.info("Voice command listener active (News, Google Search, Questions: What/Why/How).")
        log.info("Volumizer active (gain=%.1fx, soft limiter enabled).", JARVIS_VOICE_GAIN)

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.warning("Input overflow; try a larger BLOCK_MS")

                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (
                        1.0 - NOISE_FLOOR_ALPHA
                    ) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                retrigger_level = threshold * RETRIGGER_RATIO

                if level < retrigger_level:
                    spike_armed = True

                if (
                    spike_armed
                    and level >= threshold
                    and (now - last_logged_double) >= COOLDOWN_S
                ):
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                    else:
                        gap = now - first_clap_time
                        if gap < MIN_DOUBLE_GAP_S:
                            pass
                        elif gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time = None
                            last_logged_double = now
                            if not welcome_sequence_done:
                                welcome_sequence_done = True
                                log.info(
                                    "Double clap detected (gap=%.3fs, rms=%.5f, "
                                    "noise_floor=%.5f, threshold=%.5f) — running welcome once",
                                    gap,
                                    level,
                                    noise_floor,
                                    threshold,
                                )
                                threading.Thread(
                                    target=run_double_clap_actions, daemon=True
                                ).start()
                        else:
                            first_clap_time = now

                # Voice activity detection & speech capture
                if VOICE_COMMANDS_ENABLED:
                    if jarvis_is_speaking.is_set():
                        is_speaking = False
                        speech_frames.clear()
                        pre_buffer.clear()
                    else:
                        voice_threshold = max(noise_floor * 2.5, SPEECH_MIN_RMS)
                        if level >= voice_threshold:
                            if not is_speaking:
                                is_speaking = True
                                speech_frames = list(pre_buffer)
                            speech_frames.append(data.copy())
                            silence_blocks = 0
                            if len(speech_frames) >= max_speech_blocks:
                                chunk = np.concatenate(speech_frames)
                                speech_queue.put(chunk)
                                speech_frames.clear()
                                is_speaking = False
                        else:
                            if is_speaking:
                                speech_frames.append(data.copy())
                                silence_blocks += 1
                                if silence_blocks >= silence_blocks_threshold:
                                    is_speaking = False
                                    if len(speech_frames) >= min_speech_blocks:
                                        chunk = np.concatenate(speech_frames)
                                        speech_queue.put(chunk)
                                    speech_frames.clear()
                                    silence_blocks = 0
                            else:
                                pre_buffer.append(data.copy())

    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error("If PortAudio fails, install/repair drivers or try another SAMPLE_RATE.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
