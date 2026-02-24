"""Standalone script to test Piggsy voice. Run: python temp.py"""
import os
from pathlib import Path
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()
api_key = os.getenv("ELEVENLABS_API_KEY")
if not api_key:
    print("Missing ELEVENLABS_API_KEY in .env")
    exit(1)

client = ElevenLabs(api_key=api_key)
text = "what kind of... x... is this?"
voice_id = "85LOUMcMhNruPi5cBPC0"
out_path = Path(__file__).resolve().parent / "piggsy.mp3"

print("Generating Piggsy voice...")
audio = client.text_to_speech.convert(
    text=text,
    voice_id=voice_id,
    model_id="eleven_multilingual_v2",
    output_format="mp3_44100_128",
    language_code="en",
    use_pvc_as_ivc=True,
    voice_settings={
        "stability": 0.50,
        "similarity_boost": 0.75,
        "style_exaggeration": 0.0,
        "speaking_rate": 0.95,
    },
)

with open(out_path, "wb") as f:
    for chunk in audio:
        f.write(chunk)

print(f"Saved to {out_path}")
