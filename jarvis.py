#!/usr/bin/env python3
"""
J.A.R.V.I.S. — Desktop Clap & Voice Assistant
Listens via microphone for:
  1. Double-clap welcome flow (Chrome, Terminal, Spotify, ElevenLabs AI greeting).
  2. Continuous voice commands (News, Google Search, Play Any Song, Acknowledgment greetings).
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

# --- Audio Capture & Clap Tuning --------------------------------------------
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
QUIET_GATE_MULT = 2.2
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# --- Volumizer / Audio Gain -------------------------------------------------
# Boosts perceived TTS volume cleanly even when master volume is 100%.
JARVIS_VOICE_GAIN = float(os.environ.get("JARVIS_VOICE_GAIN", "2.5"))

# --- Voice Commands & Speech Recognition ------------------------------------
VOICE_COMMANDS_ENABLED = (
    os.environ.get("JARVIS_VOICE_COMMANDS_ENABLED", "true").lower() in ("1", "true", "yes")
)
JARVIS_VOICE_RESPONSES = (
    os.environ.get("JARVIS_VOICE_RESPONSES", "true").lower() in ("1", "true", "yes")
)
SPEECH_MIN_RMS = float(os.environ.get("JARVIS_SPEECH_MIN_RMS", "0.012"))

# --- Double-Clap & App Settings ---------------------------------------------
SONG_URI = os.environ.get(
    "SONG_URI",
    "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz",
)
OPEN_CHROME = True
CHROME_URL = os.environ.get("CHROME_URL", "").strip()

OPEN_TERMINAL = True
OPEN_SPOTIFY = True
SPOTIFY_SPECIAL_WORKSPACE = os.environ.get("SPOTIFY_SPECIAL_WORKSPACE", "spotify").strip()
JARVIS_MAIN_WORKSPACE = os.environ.get("JARVIS_MAIN_WORKSPACE", "1").strip()

# --- ElevenLabs Voice Welcome -----------------------------------------------
JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = (
    "Welcome home, sir. All systems are operational and at your command."
)
JARVIS_AFTER_SONG_DELAY_S = 1.0
JARVIS_WELCOME_CACHE_ENABLED = True

# --- Logging ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("JARVIS")

# Synchronization event: paused while Jarvis speaks to avoid feedback loop
jarvis_is_speaking = threading.Event()


# --- Audio Helpers ----------------------------------------------------------
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
    s = spec.strip()
    if s.isdigit():
        return int(s)
    lower = s.lower()
    for i, dev in _input_devices():
        if lower in dev["name"].lower():
            return i
    raise ValueError(f"No input device matching {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    peak = 0.0
    blocks_to_read = max(1, int(INPUT_PROBE_S / (BLOCK_MS / 1000)))
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            for _ in range(blocks_to_read):
                data, _ = stream.read(blocksize)
                r = rms_mono(data)
                if r > peak:
                    peak = r
        return peak
    except Exception:
        return None


def _choose_input_device(blocksize: int) -> int:
    override = os.environ.get("JARVIS_INPUT_DEVICE")
    if override:
        try:
            idx = _resolve_input_device_index(override)
            log.info("Using microphone override: [%d] %s", idx, sd.query_devices(idx)["name"])
            return idx
        except Exception as e:
            log.warning("Could not apply JARVIS_INPUT_DEVICE=%r: %s", override, e)

    inputs = _input_devices()
    if not inputs:
        log.error("No audio input devices found.")
        raise SystemExit(1)

    default_idx = sd.default.device[0]
    if default_idx is not None and default_idx >= 0:
        try:
            d = sd.query_devices(default_idx)
            if d["max_input_channels"] >= 1:
                p = _probe_input_max_rms(default_idx, blocksize)
                if p is not None and p >= INPUT_SILENT_RMS:
                    log.info("Using default microphone: [%d] %s", default_idx, d["name"])
                    return default_idx
        except Exception:
            pass

    for idx, d in inputs:
        p = _probe_input_max_rms(idx, blocksize)
        if p is not None and p >= INPUT_SILENT_RMS:
            log.info("Auto-selected active microphone: [%d] %s", idx, d["name"])
            return idx

    fallback_idx, fallback_dev = inputs[0]
    log.warning("Falling back to microphone: [%d] %s", fallback_idx, fallback_dev["name"])
    return fallback_idx


# --- Volumizer & Audio Output -----------------------------------------------
def volumize_audio(pcm_f: np.ndarray, gain: float | None = None) -> np.ndarray:
    """Apply software gain boost with a soft-knee limiter to prevent clipping."""
    if gain is None:
        gain = JARVIS_VOICE_GAIN
    if gain <= 0:
        return pcm_f

    boosted = pcm_f * gain
    peak = float(np.max(np.abs(boosted))) if boosted.size > 0 else 0.0
    if peak <= 0.95:
        return boosted

    # Soft-knee limiting: values above 0.7 are gently saturated via tanh
    abs_samples = np.abs(boosted)
    mask = abs_samples > 0.7
    if np.any(mask):
        excess = abs_samples[mask] - 0.7
        compressed = 0.7 + 0.28 * np.tanh(excess / max(peak - 0.7, 1e-6))
        boosted[mask] = np.sign(boosted[mask]) * compressed

    return np.clip(boosted, -0.99, 0.99)


def _play_audio_blocking(pcm_f: np.ndarray, rate: int) -> bool:
    boosted = volumize_audio(pcm_f)
    jarvis_is_speaking.set()
    try:
        sd.play(boosted, rate)
        sd.wait()
    except Exception as e:
        log.warning("Audio playback error: %s", e)
        return False
    finally:
        time.sleep(0.3)
        jarvis_is_speaking.clear()
    return True


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return False
            raw = wf.readframes(wf.getnframes())
            rate = wf.getframerate()
    except Exception as e:
        log.warning("Could not read WAV file %s: %s", path, e)
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


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = 24000
    if fmt.startswith("pcm_"):
        try:
            rate = int(fmt.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return voice, model, fmt, rate


def _jarvis_welcome_cache_dir() -> Path:
    return Path(__file__).resolve().parent / ".cache" / "jarvis_welcome"


def _jarvis_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _jarvis_welcome_cache_dir() / f"{digest}.wav"


def speak_phrase(text: str) -> None:
    """Asynchronously speaks text through ElevenLabs TTS."""
    if not JARVIS_VOICE_RESPONSES or not text.strip():
        return
    threading.Thread(target=_speak_worker, args=(text,), daemon=True).start()


def _speak_worker(text: str) -> None:
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
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
        pcm_i16 = np.frombuffer(raw, dtype=np.int16)
        pcm_f = pcm_i16.astype(np.float32) / 32768.0
        _play_audio_blocking(pcm_f, pcm_rate)
    except Exception as e:
        log.warning("Voice response error: %s", e)


def say_jarvis_welcome() -> None:
    if not JARVIS_WELCOME_ENABLED or not JARVIS_WELCOME_PHRASE.strip():
        return
    speak_phrase(JARVIS_WELCOME_PHRASE.strip())


# --- Platform Integrations & App Launchers ----------------------------------
def _is_hyprland() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def _hyprctl(args: list[str]) -> str:
    try:
        res = subprocess.run(["hyprctl", *args], capture_output=True, text=True, check=False)
        return res.stdout
    except Exception as e:
        log.warning("hyprctl error: %s", e)
        return ""


def _is_special_spotify_visible() -> bool:
    if not _is_hyprland():
        return False
    try:
        import json

        out = _hyprctl(["monitors", "-j"])
        monitors = json.loads(out)
        for m in monitors:
            if m.get("specialWorkspace", {}).get("name") == f"special:{SPOTIFY_SPECIAL_WORKSPACE}":
                return True
    except Exception:
        pass
    return False


def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if base:
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


def open_chrome_window(url: str | None = None) -> None:
    chrome = _chrome_executable()
    if not chrome:
        if url:
            webbrowser.open(url)
        return

    home_dir = str(Path.home())
    ws = JARVIS_MAIN_WORKSPACE if (JARVIS_MAIN_WORKSPACE and JARVIS_MAIN_WORKSPACE.lower() != "active") else ""
    url_arg = f" {url.strip()}" if (url and url.strip()) else ""

    if _is_hyprland() and ws:
        cmd = f"[workspace {ws}] {chrome} --new-window{url_arg}"
        log.info("Launching Chrome on workspace %s: %s", ws, cmd)
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


def open_terminal() -> None:
    home_dir = str(Path.home())
    if sys.platform == "win32":
        term = shutil.which("wt.exe") or shutil.which("powershell.exe") or shutil.which("cmd.exe")
        if term:
            try:
                subprocess.Popen([term], cwd=home_dir)
                return
            except OSError:
                pass
    elif sys.platform == "darwin":
        for app in ("iTerm", "Terminal"):
            try:
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

    ws = JARVIS_MAIN_WORKSPACE if (JARVIS_MAIN_WORKSPACE and JARVIS_MAIN_WORKSPACE.lower() != "active") else ""
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
            log.warning("Could not launch Chrome: %s", e)

    for u in urls:
        webbrowser.open(u)


def search_google(query: str) -> None:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}"
    log.info("Searching Google for query: %r", query)
    open_in_browser(url)


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


def open_and_play_spotify(uri: str) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", "Spotify", uri])
            return
        except OSError:
            play_song(uri)
            return
    elif sys.platform == "win32":
        play_song(uri)
        return

    # Linux native Spotify
    is_running = subprocess.run(["pgrep", "-x", "spotify"], capture_output=True).returncode == 0
    if not is_running:
        log.info("Starting Spotify...")
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

    if _is_hyprland() and not _is_special_spotify_visible():
        log.info("Toggling special workspace '%s'", SPOTIFY_SPECIAL_WORKSPACE)
        _hyprctl(["dispatch", "togglespecialworkspace", SPOTIFY_SPECIAL_WORKSPACE])

    u = uri.strip()
    track_uri = None
    if u.startswith("spotify:track:") or u.startswith("spotify:search:"):
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

    subprocess.run(
        [
            "dbus-send",
            "--dest=org.mpris.MediaPlayer2.spotify",
            "/org/mpris/MediaPlayer2",
            "org.mpris.MediaPlayer2.Player.Play",
        ],
        capture_output=True,
    )


def play_any_song(query: str) -> None:
    """Plays any song requested by name via Spotify or YouTube."""
    clean = query.strip()
    if not clean:
        return

    # Check if user explicitly asked for YouTube
    play_on_youtube = False
    lower = clean.lower()
    for yt_suffix in (" on youtube", " in youtube"):
        if lower.endswith(yt_suffix):
            clean = clean[: -len(yt_suffix)].strip()
            play_on_youtube = True
            break

    encoded = urllib.parse.quote_plus(clean)
    log.info("Playing song: %r (on_youtube=%s)", clean, play_on_youtube)

    if play_on_youtube:
        url = f"https://www.youtube.com/results?search_query={encoded}"
        open_in_browser(url)
        return

    # Spotify search
    spotify_search_uri = f"spotify:search:{encoded}"
    spotify_web_url = f"https://open.spotify.com/search/{encoded}"

    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", "Spotify", spotify_search_uri])
            return
        except OSError:
            open_in_browser(spotify_web_url)
            return
    elif sys.platform == "win32":
        try:
            os.startfile(spotify_search_uri)
            return
        except OSError:
            open_in_browser(spotify_web_url)
            return

    # Linux
    if shutil.which("spotify"):
        open_and_play_spotify(spotify_search_uri)
    else:
        open_in_browser(spotify_web_url)


# --- Voice Commands & Parsing -----------------------------------------------
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
    available = [i for i in range(len(JARVIS_GREETING_RESPONSES)) if i != _last_greeting_index]
    idx = random.choice(available)
    _last_greeting_index = idx
    return JARVIS_GREETING_RESPONSES[idx]


def parse_voice_command(text: str) -> dict | None:
    raw = text.strip()
    if not raw:
        return None
    lower = raw.lower()

    # 1. Strip wake words & polite conversational prefixes
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

    # If only the wake word or attention greeting was spoken
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

    # 2. News command: phrase contains 'news'
    if re.search(r"\bnews\b", cleaned_lower):
        return {
            "type": "news",
            "urls": ["https://github.com/feed", "https://youtube.com"],
            "speech": "Opening your News feeds, sir.",
            "original": raw,
        }

    # 3. Play song command: "play [song]"
    if cleaned_lower.startswith("play "):
        song = raw_cleaned[len("play "):].strip(" ,.:;?!")
        for pfx in ("song ", "track "):
            if song.lower().startswith(pfx):
                song = song[len(pfx):].strip(" ,.:;?!")
        if song:
            return {
                "type": "play_song",
                "song": song,
                "speech": f"Playing {song}, sir.",
                "original": raw,
            }

    # 4. Explicit search commands
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

    # 5. Question inquiries: any question word in sentence
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

    if cmd_type == "play_song":
        play_any_song(parsed["song"])
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
            log.warning("Speech recognition worker error: %s", e)
        finally:
            q.task_done()


# --- Double-Clap Action -----------------------------------------------------
def run_double_clap_actions() -> None:
    """Runs the double-clap welcome sequence: switches workspace, launches apps, plays greeting."""
    if _is_hyprland() and JARVIS_MAIN_WORKSPACE and JARVIS_MAIN_WORKSPACE.lower() != "active":
        log.info("Switching to workspace: %s", JARVIS_MAIN_WORKSPACE)
        _hyprctl(["dispatch", "workspace", JARVIS_MAIN_WORKSPACE])
        time.sleep(0.15)

    if OPEN_CHROME:
        open_chrome_window(CHROME_URL)
        time.sleep(0.2)

    if OPEN_TERMINAL:
        open_terminal()
        time.sleep(0.5)

    if OPEN_SPOTIFY:
        open_and_play_spotify(SONG_URI)
    elif SONG_URI.strip():
        play_song(SONG_URI)

    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()


# --- Main Loop --------------------------------------------------------------
def main() -> int:
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True
    welcome_sequence_done = False

    log.info(
        "Listening (double-clap: %.2f–%.2fs apart, rate=%d, block=%d ms, "
        "spike_ratio=%.1f, cooldown=%.2fs). Press Ctrl+C to stop.",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SAMPLE_RATE,
        BLOCK_MS,
        SPIKE_RATIO,
        COOLDOWN_S,
    )
    if _is_hyprland():
        log.info("Hyprland active: Chrome and Terminal open on workspace '%s'", JARVIS_MAIN_WORKSPACE)
        if OPEN_SPOTIFY:
            log.info("Spotify special workspace overlay: 'special:%s'", SPOTIFY_SPECIAL_WORKSPACE)

    if JARVIS_WELCOME_ENABLED:
        ev, em, ef, er = elevenlabs_env_config()
        log.info("ElevenLabs AI voice ready: voice=%s, model=%s, format=%s", ev or "(unset)", em, ef)

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
        threading.Thread(target=speech_recognition_worker, args=(speech_queue,), daemon=True).start()
        log.info("Voice listener active (News, Music Player, Google Search, Questions).")
        log.info("Audio volumizer active (gain=%.1fx).", JARVIS_VOICE_GAIN)

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
                    log.warning("Input overflow; consider increasing BLOCK_MS")

                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (1.0 - NOISE_FLOOR_ALPHA) * level
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
                                    "Double clap detected (gap=%.3fs, rms=%.5f) -> running welcome sequence",
                                    gap,
                                    level,
                                )
                                threading.Thread(target=run_double_clap_actions, daemon=True).start()
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
        log.info("Jarvis stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("PortAudio error: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
