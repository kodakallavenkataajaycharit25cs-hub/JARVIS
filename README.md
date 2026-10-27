# J.A.R.V.I.S. — Desktop Clap & Voice Assistant

An intelligent, cross-platform desktop assistant in Python inspired by Iron Man's J.A.R.V.I.S. It continuously listens via your microphone and handles double-clap triggers, natural conversational voice queries, automated Google searches, news dashboards, and realistic ElevenLabs AI voice feedback.

Supports Linux (including Hyprland, Wayland, and X11), Windows, and macOS.

---

## Features & Functionality

### 1. Double-Clap Welcome Flow
- **Trigger**: Double clap (two sharp sound transients within 0.05 to 0.35 seconds).
- **Actions**:
  - Automatically switches to your main workspace (on Hyprland / Linux).
  - Opens Google Chrome and your terminal emulator together.
  - Opens Spotify (special overlay workspace on Hyprland, or native app on Windows/macOS) and starts playback of your configured track.
  - Greets you with an ElevenLabs AI voice welcome debrief.

### 2. Audio Volumizer
- **Purpose**: Solves low speech volume by boosting audio perceived loudness cleanly even when system volume is at 100%.
- **Implementation**: Applies a configurable software gain factor (`JARVIS_VOICE_GAIN`, default 2.5x) and a soft-knee compressor/limiter.
- **Protection**: Ensures that loud syllables saturate smoothly without harsh digital clipping or crackle.
- **Caching**: Both cached WAV audio files and new ElevenLabs TTS audio are amplified seamlessly.

### 3. Continuous Voice Commands

#### Wake Words & Attention Calls
- **Trigger words**:
  - `"Hey Jarvis"`
  - `"Jarvis"`
  - `"Hello Jarvis"`
  - `"Hi Jarvis"`
  - `"Ok Jarvis"`
  - `"Can you hear me"` / `"Are you there"`
- **Response**: Jarvis acknowledges out loud with cycling phrases:
  - *"At your service, sir."*
  - *"Yes, sir?"*
  - *"How may I assist you, sir?"*
  - *"Always at your service, sir."*
  - *"Online and ready, sir."*
- **Non-repeating shuffle**: Phrases cycle randomly without repeating the same acknowledgment twice in a row. Responses are cached locally for zero network latency.

#### News Dashboard
- **Trigger words**: Any phrase containing the word `"news"`:
  - `"What is the news today?"`
  - `"Show me the news"`
  - `"Open news"`
  - `"Tell me the news"`
  - `"Check news"`
- **Actions**: Opens both `https://github.com/feed` and `https://youtube.com` in your browser, and speaks a brief spoken confirmation.

#### Pull Up & Video Launcher
- **Trigger words**: Phrases starting with `"pull up [website/service] and search/play [topic]"` or `"pull up [website]"`:
  - `"Pull up youtube and search machine learning tutorial"`
  - `"Pull up youtube and play lofi hip hop"`
  - `"Pull up github and search jarvis"`
  - `"Pull up reddit and search linux gaming"`
  - `"Pull up google and search weather in Tokyo"`
  - `"Pull up youtube"`
  - `"Pull up netflix"`
- **Actions**: Automatically launches the requested platform or queries the specific video/topic in your browser, and dynamically acknowledges out loud with natural, varied responses (e.g. *"Coming right up, sir."*, *"Searching YouTube for [topic] right away, sir."*, *"Pulling that up for you right away, sir."*, *"On it, sir. Opening [site] now."*).

#### Music Player (Play Any Song)
- **Trigger words**: Phrases starting with `"play [song name]"`:
  - `"Play Starboy by The Weeknd"`
  - `"Play Bohemian Rhapsody"`
  - `"Play song Hotel California"`
  - `"Play track Interstellar theme"`
  - `"Play lofi hip hop on youtube"`
- **Actions**: Automatically launches and searches for the track on Spotify (or YouTube if specified with "on youtube"), and speaks a confirmation (*"Playing [song], sir."*).

#### Questions & Queries
- **Trigger words**: Questions starting with or containing:
  - `"what"`, `"what's"`
  - `"why"`, `"why's"`
  - `"how"`, `"how's"`
  - `"who"`, `"who's"`
  - `"where"`, `"where's"`
  - `"when"`, `"when's"`
  - `"which"`
- **Examples**:
  - *"Why is this arduino having this issue?"*
  - *"Tell me what is a reinforcement learning model"*
  - *"Can you tell me how to compile a linux kernel?"*
  - *"What is quantum computing?"*
- **Actions**: Cleans conversational prefixes (such as "tell me" or "can you") and searches Google for the query in your browser.

#### Look Up & Research
- **Trigger words**: Phrases starting with:
  - `"look up [topic]"`
  - `"search for [topic]"`
  - `"search [topic]"`
  - `"google [topic]"`
  - `"find out [topic]"`
  - `"explain [topic]"`
  - `"tell me about [topic]"`
- **Examples**:
  - *"Look up raspberry pi 5 pinout"*
  - *"Search for Python async tutorial"*
  - *"Explain how backpropagation works"*
- **Actions**: Extracts the topic and opens the Google search results in your browser.

#### Echo Prevention
- When Jarvis speaks through your speakers, microphone listening is automatically paused so Jarvis never transcribes or loops its own voice.

---

## Cross-Platform Support

| Feature | Linux | Windows | macOS |
| :--- | :--- | :--- | :--- |
| Audio Capture | ALSA / PulseAudio / PipeWire | WASAPI / DirectSound | CoreAudio |
| Browser | google-chrome-stable / chromium | Auto-detects Program Files / AppData | /Applications/Google Chrome.app |
| Terminal | kitty, alacritty, foot, gnome-terminal | Windows Terminal (wt.exe), PowerShell, CMD | iTerm, Terminal |
| Spotify | Hyprland special workspace + DBus MPRIS | Native Spotify URI handler | Native open -a Spotify |
| Voice Commands | Supported | Supported | Supported |
| Audio Volumizer | Supported | Supported | Supported |

If a specific application (such as Kitty or Hyprland) is not installed on your system, the script falls back gracefully to standard system defaults and Python's built-in `webbrowser`.

---

## Prerequisites & Requirements

### 1. PortAudio System Library
Microphone audio streaming requires PortAudio:
- **Arch Linux**: `sudo pacman -S --needed portaudio`
- **Ubuntu / Debian**: `sudo apt update && sudo apt install -y portaudio19-dev`
- **Fedora**: `sudo dnf install portaudio-devel`
- **macOS**: `brew install portaudio`
- **Windows**: Included automatically by sounddevice wheels.

### 2. ElevenLabs Text-to-Speech (TTS)
To enable Jarvis's voice output:
1. **Get an API Key**:
   - Create an account at [ElevenLabs](https://elevenlabs.io).
   - Click on your profile icon in the bottom-left corner and select **API Keys**.
   - Create a new API key and copy it.
2. **Find a Voice ID**:
   - Go to **Voices** -> **Explore** or **My Voices** in ElevenLabs.
   - Choose any voice (such as Adam, Antony, or clone a custom Jarvis voice).
   - Click the voice card and copy the **Voice ID** string (e.g. `pNInz6obpgDQGcFmaJgB`).
3. **Model Selection**:
   - Defaults to `eleven_multilingual_v2`. You can also configure `eleven_turbo_v2_5` for lower latency.

---

## Installation & Setup

```bash
# 1. Clone the repository
git clone https://github.com/kodakallavenkataajaycharit25cs-hub/JARVIS.git
cd JARVIS

# 2. Create and activate a virtual environment
python3 -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
# .venv\Scripts\Activate.ps1

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Copy the environment configuration template
cp .env.example .env
```

---

## Configuration (.env)

Edit `.env` to configure your API keys and preferences:

```env
# ElevenLabs AI Voice
ELEVENLABS_API_KEY=your_elevenlabs_api_key_here
ELEVENLABS_VOICE_ID=your_voice_id_here
ELEVENLABS_MODEL_ID=eleven_multilingual_v2

# Audio Volumizer (gain factor, default: 2.5)
JARVIS_VOICE_GAIN=2.5

# Voice Commands
JARVIS_VOICE_COMMANDS_ENABLED=true
JARVIS_VOICE_RESPONSES=true
JARVIS_SPEECH_MIN_RMS=0.012

# Double-Clap & App Settings (Linux / Hyprland)
JARVIS_MAIN_WORKSPACE=1
SPOTIFY_SPECIAL_WORKSPACE=spotify
SONG_URI=https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz
CHROME_URL=
```

---

## Running Jarvis

```bash
source .venv/bin/activate
python jarvis.py
```

- Double clap to run your welcome sequence.
- Speak commands naturally into your microphone at any time.
- Stop with `Ctrl+C`.
