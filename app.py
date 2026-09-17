import asyncio
import io
import os
import re
import subprocess
import tempfile
import threading
import urllib.parse
import uuid
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

# ---------------- Fila de vídeos (processamento em segundo plano) ----------------
JOBS_DIR = Path("/tmp/vozia_jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)
JOBS = {}

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


def extrair_assunto(cena, tag):
    cena_limpa = cena.replace('"', "").replace("'", "").strip()
    palavras = cena_limpa.split()
    if len(palavras) <= 6:
        return cena_limpa
    nomes_proprios = [p for p in palavras if p[0].isupper() and len(p) > 2]
    if nomes_proprios:
        return " ".join(nomes_proprios[:3])
    return " ".join(palavras[:8])


def baixar_imagem(cena, pasta, idx, modo, estilo="cinematic"):
    tag = detectar_tag(cena)
    destino = pasta / f"cena_{idx:03d}.jpg"
    sufixo = ESTILOS_IMAGEM.get(estilo, ESTILOS_IMAGEM["cinematic"])
    assunto = extrair_assunto(cena, tag)
    if modo in ("qualidade", "automatico"):
        prompt = (
            f"A cinematic scene of {assunto}, related to {tag}, "
            f"{sufixo}, no text, no watermark, 16:9 widescreen"
        )
        url = (
            "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
            "?width=1280&height=720&nologo=true&seed=" + str(1000 + idx) + "&model=flux"
        )
        if baixar_url(url, destino, timeout=8):
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
    t = t.replace('"', " ").replace(":", " ").replace("'", " ").replace("%", " ")
    return t.strip()[:120]


def quebrar_linhas(texto, max_chars=28, max_linhas=3):
    palavras = texto.split()
    linhas, atual = [], []
    for p in palavras:
        if sum(len(w) for w in atual) + len(atual) + len(p) <= max_chars:
            atual.append(p)
        else:
            if atual:
                linhas.append(" ".join(atual))
            atual = [p]
            if len(linhas) >= max_linhas:
                break
    if atual and len(linhas) < max_linhas:
        linhas.append(" ".join(atual))
    return "\n".join(linhas[:max_linhas])


def renderizar_cena(img, legenda, idx, total, frames, pasta, outname,
                    legenda_mostrar=True, legenda_posicao="inferior",
                    legenda_tamanho=28):
    rotulo = escapar_drawtext(f"Cena {idx} de {total}")

    # Filtros fixos (marca d'água e contador de cena)
    filtros = [
        "scale=1920:1080",
        ("zoompan=z='min(1.0+0.00015*on,1.08)':x='iw/2-(iw/zoom/2)':"
         "y='ih/2-(ih/zoom/2)':d={}:s=1280x720:fps=30".format(frames)),
        ("drawtext=fontfile={}:text='VozIA':fontcolor=white@0.9:fontsize=30:"
         "x=40:y=40:shadowcolor=black@0.8:shadowx=2:shadowy=2".format(FONTE)),
        ("drawtext=fontfile={}:text='{}':fontcolor=white@0.7:fontsize=26:"
         "x=w-text_w-40:y=40:shadowcolor=black@0.8:shadowx=2:shadowy=2".format(FONTE, rotulo)),
    ]

    # Filtro da legenda (opcional e reenquadrável)
    if legenda_mostrar:
        texto_legenda = escapar_drawtext(legenda)
        texto_legenda = quebrar_linhas(texto_legenda, max_chars=28, max_linhas=3)
        legenda_file = pasta / f"legenda_{idx:03d}.txt"
        legenda_file.write_text(texto_legenda, encoding="utf-8")

        if legenda_posicao == "centro":
            pos_y = "(h-text_h)/2"
        elif legenda_posicao == "superior":
            pos_y = "90"
        else:
            pos_y = "h-text_h-40"

        boxborder = max(8, int(legenda_tamanho * 0.35))
        filtros.append(
            "drawtext=fontfile={}:textfile={}:fontcolor=white:fontsize={}:"
            "x=(w-text_w)/2:y={}:line_spacing=6:box=1:boxcolor=black@0.45:"
            "boxborderw={}:shadowcolor=black@0.8:shadowx=2:shadowy=2".format(
                FONTE, legenda_file, legenda_tamanho, pos_y, boxborder
            )
        )

    vf = ",".join(filtros)
    subprocess.run(
        [FFMPEG, "-y", "-loop", "1", "-i", str(img), "-vf", vf,
         "-t", str(frames / 30.0), "-r", "30", "-preset", "ultrafast",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(pasta / outname)],
        capture_output=True,
    )


def gerar_video_job(job_id, dados):
    final = JOBS_DIR / f"{job_id}.mp4"
    try:
        texto = (dados.get("text") or "").strip()
        if not texto:
            raise ValueError("Texto vazio")

        voz = dados.get("voice", "pt-BR-FranciscaNeural")
        rate = dados.get("rate", "+0%")
        pitch = dados.get("pitch", "+0Hz")
        volume = dados.get("volume", "+0%")
        modo = dados.get("mode", "rapido")
        estilo = dados.get("style", "cinematic")

        # Configurações de legenda
        legenda_mostrar = dados.get("legenda_mostrar", True)
        if not isinstance(legenda_mostrar, bool):
            legenda_mostrar = str(legenda_mostrar).lower() in ("1", "true", "sim", "yes", "on")
        legenda_posicao = str(dados.get("legenda_posicao", "inferior")).lower()
        if legenda_posicao not in ("inferior", "centro", "superior"):
            legenda_posicao = "inferior"
        try:
            legenda_tamanho = int(dados.get("legenda_tamanho", 28))
        except (TypeError, ValueError):
            legenda_tamanho = 28
        legenda_tamanho = max(20, min(40, legenda_tamanho))
        legendas = dados.get("legendas")
        if not isinstance(legendas, list):
            legendas = None

        with tempfile.TemporaryDirectory() as tmp:
            pasta = Path(tmp)

            cenas = dividir_em_cenas(texto)
            if len(cenas) > 40:
                raise ValueError("Muitas cenas (máximo 40). Divida o texto em partes.")

            # Valida o array de legendas customizadas contra o número de cenas
            legendas_usar = legendas if (legendas and len(legendas) == len(cenas)) else None

            for i, cena in enumerate(cenas, 1):
                baixar_imagem(cena, pasta, i, modo, estilo)

            audio = pasta / "narracao.mp3"
            asyncio.run(gerar_audio(texto, voz, rate, pitch, volume, audio))

            dur = duracao_audio(audio)
            totais = [max(len(c), 1) for c in cenas]
            soma = sum(totais)
            frames_por_cena = [max(30, int(dur * 30 * (t / soma))) for t in totais]

            for i, cena in enumerate(cenas):
                legenda_cena = cena
                if legendas_usar:
                    legenda_cena = (legendas_usar[i - 1] or "").strip() or cena
                renderizar_cena(
                    pasta / f"cena_{i+1:03d}.jpg",
                    legenda_cena,
                    i + 1,
                    len(cenas),
                    frames_por_cena[i],
                    pasta,
                    f"clip_{i:03d}.mp4",
                    legenda_mostrar=legenda_mostrar,
                    legenda_posicao=legenda_posicao,
                    legenda_tamanho=legenda_tamanho,
                )

            lista = pasta / "lista.txt"
            lista.write_text("".join(f"file 'clip_{i:03d}.mp4'\n" for i in range(len(cenas))))

            concat = pasta / "concat.mp4"
            subprocess.run(
                [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
                 "-c", "copy", str(concat)],
                capture_output=True,
            )

            subprocess.run(
                [FFMPEG, "-y", "-i", str(concat), "-i", str(audio),
                 "-c:v", "copy", "-c:a", "aac", "-shortest",
                 "-movflags", "+faststart", str(final)],
                capture_output=True,
            )

        if not final.exists() or final.stat().st_size == 0:
            raise RuntimeError("Falha ao montar o vídeo")

        JOBS[job_id] = {"status": "done", "file": str(final)}
    except Exception as exc:
        JOBS[job_id] = {"status": "error", "error": str(exc)}


@app.route("/api/video", methods=["POST"])
def api_video():
    dados = request.get_json(force=True, silent=True) or {}
    texto = (dados.get("text") or "").strip()
    if not texto:
        return jsonify({"erro": "Texto vazio"}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "processing"}
    threading.Thread(target=gerar_video_job, args=(job_id, dados), daemon=True).start()
    return jsonify({"jobId": job_id}), 202


@app.get("/api/video/status/<job_id>")
def video_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"erro": "Trabalho não encontrado"}), 404
    if job["status"] == "processing":
        return jsonify({"status": "processing"})
    if job["status"] == "done":
        return jsonify({"status": "done", "downloadUrl": f"/api/video/download/{job_id}"})
    return jsonify({"status": "error", "erro": job.get("error", "Erro desconhecido")}), 500


@app.get("/api/video/download/<job_id>")
def video_download(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"erro": "Vídeo não disponível"}), 404
    return send_file(job["file"], mimetype="video/mp4", as_attachment=True,
                     download_name="vozia_video.mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=False)
