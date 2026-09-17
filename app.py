import asyncio
import io
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

import edge_tts
import requests
from flask import Flask, jsonify, request, Response, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------------- Configuração ----------------
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
FONTE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ---------------- Lista de vozes ----------------
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

# ---------------- Áudio (TTS) ----------------
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

# ---------------- Vídeo ----------------
TAGS = {
    "tecnologia": "technology", "software": "technology", "digital": "technology", "app": "technology",
    "natureza": "nature", "ambiente": "nature", "animais": "nature", "planta": "nature",
    "saude": "health", "medico": "health", "hospital": "health", "medicina": "health",
    "dinheiro": "finance", "financa": "finance", "economia": "finance", "investimento": "finance",
    "escola": "education", "estudo": "education", "educacao": "education", "professor": "education",
    "viagem": "travel", "viajar": "travel", "cidade": "travel",
    "comida": "food", "alimentacao": "food", "receita": "food", "restaurante": "food",
    "negocio": "business", "empresa": "business", "empreendedor": "business", "trabalho": "business",
    "esporte": "sport", "futebol": "sport", "exercicio": "sport",
    "musica": "music", "arte": "music", "cultura": "music",
}

def detectar_tag(texto):
    t = texto.lower()
    for palavra, tag in TAGS.items():
        if palavra in t:
            return tag
    return "abstract"

def dividir_em_cenas(texto):
    frases = [f.strip() for f in re.split(r'(?<=[.!?…])\s+|\n+', texto) if f.strip()]
    cenas, atual = [], ""
    for f in frases:
        if len(atual) + len(f) + 1 > 180:
            if atual:
                cenas.append(atual.strip())
            atual = f
        else:
            atual = (atual + " " + f).strip()
    if atual:
        cenas.append(atual.strip())
    return cenas

def baixar_url(url, destino, timeout=8):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 1000:
            destino.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False

def criar_gradiente(destino):
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi",
         "-i", "gradients=s=1280x720:c0=0x1a0b2e:c1=0x0f3460",
         "-frames:v", "1", str(destino)],
        capture_output=True,
    )

ESTILOS_IMAGEM = {
    "cinematic": "cinematic film still, dramatic lighting, unified color palette, high detail",
    "realistic": "photorealistic, natural lighting, realistic textures, high detail",
    "illustration": "digital illustration, vibrant colors, clean composition, high detail",
    "anime": "anime style, cel shading, vibrant colors, clean lines, high detail",
    "fantasy": "epic fantasy art, magical atmosphere, rich colors, volumetric light, high detail",
}

def baixar_imagem(cena, pasta, idx, modo, estilo="cinematic"):
    tag = detectar_tag(cena)
    destino = pasta / f"cena_{idx:03d}.jpg"
    sufixo = ESTILOS_IMAGEM.get(estilo, ESTILOS_IMAGEM["cinematic"])
    if modo in ("qualidade", "automatico"):
        prompt = (cena[:120] + ", " + sufixo + ", no text, no watermark, 16:9 widescreen")
        url = (
            "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
            "?width=1280&height=720&nologo=true&seed=" + str(1000 + idx) + "&model=flux"
        )
        if baixar_url(url, destino, timeout=6):
            return "ia"
    if baixar_url(f"https://loremflickr.com/1280/720/{tag}", destino, timeout=4):
        return "loremflickr"
    if baixar_url(f"https://picsum.photos/1280/720?random={1000+idx}", destino, timeout=4):
        return "picsum"
    criar_gradiente(destino)
    return "gradiente"

async def gerar_audio(texto, voz, rate, pitch, volume, destino):
    comunicador = edge_tts.Communicate(texto, voz, rate=rate, pitch=pitch, volume=volume)
    await comunicador.save(str(destino))

def duracao_audio(arquivo):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(arquivo)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except Exception:
        return 10.0

def escapar_drawtext(t):
    t = t.replace('"', " ").replace(":", " ").replace("'", " ").replace("%", " ").replace("\n", " ").replace(",", " ")
    return t.strip()[:80]

def renderizar_cena(img, legenda, idx, total, frames, pasta, outname):
    texto = escapar_drawtext(legenda)
    rotulo = escapar_drawtext(f"Cena {idx} de {total}")
    vf = (
        f"scale=1920:1080,"
        f"zoompan=z='min(1.0+0.00015*on,1.08)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s=1280x720:fps=30,"
        f"drawtext=fontfile={FONTE}:text='VozIA':fontcolor=white@0.9:fontsize=30:x=40:y=40:shadowcolor=black@0.8:shadowx=2:shadowy=2,"
        f"drawtext=fontfile={FONTE}:text='{rotulo}':fontcolor=white@0.7:fontsize=26:x=w-text_w-40:y=40:shadowcolor=black@0.8:shadowx=2:shadowy=2,"
        f"drawtext=fontfile={FONTE}:text='{texto}':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=h-170:box=1:boxcolor=black@0.45:boxborderw=22:shadowcolor=black@0.8:shadowx=2:shadowy=2"
    )
    subprocess.run(
        [FFMPEG, "-y", "-loop", "1", "-i", str(img), "-vf", vf,
         "-t", str(frames / 30.0), "-r", "30", "-preset", "ultrafast",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(pasta / outname)],
        capture_output=True,
    )

@app.route("/api/video", methods=["POST"])
def api_video():
    dados = request.get_json(force=True, silent=True) or {}
    texto = (dados.get("text") or "").strip()
    if not texto:
        return jsonify({"erro": "Texto vazio"}), 400

    voz = dados.get("voice", "pt-BR-FranciscaNeural")
    rate = dados.get("rate", "+0%")
    pitch = dados.get("pitch", "+0Hz")
    volume = dados.get("volume", "+0%")
    modo = dados.get("mode", "rapido")
    estilo = dados.get("style", "cinematic")

    with tempfile.TemporaryDirectory() as tmp:
        pasta = Path(tmp)

        cenas = dividir_em_cenas(texto)
        if len(cenas) > 40:
            return jsonify({"erro": "Muitas cenas (máximo 40). Divida o texto em partes."}), 400

        origens = []
        for i, cena in enumerate(cenas, 1):
            origem = baixar_imagem(cena, pasta, i, modo, estilo)
            origens.append({"cena": cena[:60], "origem": origem})

        audio = pasta / "narracao.mp3"
        asyncio.run(gerar_audio(texto, voz, rate, pitch, volume, audio))

        dur = duracao_audio(audio)
        totais = [max(len(c), 1) for c in cenas]
        soma = sum(totais)
        frames_por_cena = [max(30, int(dur * 30 * (t / soma))) for t in totais]

        for i, cena in enumerate(cenas):
            renderizar_cena(
                pasta / f"cena_{i+1:03d}.jpg",
                cena,
                i + 1,
                len(cenas),
                frames_por_cena[i],
                pasta,
                f"clip_{i:03d}.mp4",
            )

        lista = pasta / "lista.txt"
        lista.write_text("".join(f"file 'clip_{i:03d}.mp4'\n" for i in range(len(cenas))))

        concat = pasta / "concat.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
             "-c", "copy", str(concat)],
            capture_output=True,
        )

        final = pasta / "vozia_video.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-i", str(concat), "-i", str(audio),
             "-c:v", "copy", "-c:a", "aac", "-shortest",
             "-movflags", "+faststart", str(final)],
            capture_output=True,
        )

        if final.stat().st_size == 0:
            return jsonify({"erro": "Falha ao montar o vídeo"}), 500

        return send_file(final, mimetype="video/mp4", as_attachment=True,
                         download_name="vozia_video.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
