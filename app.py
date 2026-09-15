import asyncio
import io
import os

import edge_tts
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

_voices_cache = None

@app.get("/api/voices")
def list_voices():
    global _voices_cache
    if _voices_cache is None:
        raw = asyncio.run(edge_tts.list_voices())
        _voices_cache = sorted(
            (
                {
                    "shortName": v["ShortName"],
                    "name": v["ShortName"].split("-")[2].replace("Neural", ""),
                    "gender": v["Gender"],
                    "locale": v["Locale"],
                }
                for v in raw
            ),
            key=lambda v: (v["locale"], v["name"]),
        )
    return jsonify(_voices_cache)

@app.post("/api/tts")
def synthesize():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()

    if not text:
        return jsonify({"error": "Texto vazio."}), 400

    voice = data.get("voice") or "pt-BR-FranciscaNeural"
    rate = data.get("rate") or "+0%"
    pitch = data.get("pitch") or "+0Hz"
    volume = data.get("volume") or "+0%"

    async def run() -> bytes:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
        buffer = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buffer.write(chunk["data"])
        return buffer.getvalue()

    try:
        audio = asyncio.run(run())
    except Exception as exc:
        return jsonify({"error": f"Falha na sintese: {exc}"}), 502

    if not audio:
        return jsonify({"error": "O motor nao retornou audio. Verifique a voz."}), 502

    return Response(audio, mimetype="audio/mpeg")
