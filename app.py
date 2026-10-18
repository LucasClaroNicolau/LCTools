import os
import time
import threading
import uuid
from flask import Flask, render_template, request, send_file, flash, redirect, url_for, jsonify
import yt_dlp
from rembg import remove
from PIL import Image

app = Flask(__name__)
app.secret_key = "lc-tools-chave-secreta-para-mensagens-flash"

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
DOWNLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
FILE_EXPIRATION_SECONDS = 300

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

PROGRESS_STORE = {}

def clear_download_folder():
    if os.path.exists(DOWNLOAD_FOLDER):
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.remove(file_path)
            except Exception as e:
                print(f"[STARTUP CLEANUP ERROR] {e}")

clear_download_folder()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/remove-bg')
def remove_bg_page():
    return render_template('background_remover.html')

@app.route('/progress/<task_id>')
def get_progress(task_id):
    return jsonify(PROGRESS_STORE.get(task_id, {"progress": 0, "status": "starting"}))

@app.route('/get-file/<task_id>')
def get_file(task_id):
    task = PROGRESS_STORE.get(task_id)
    if task and task.get("status") == "completed" and os.path.exists(task.get("file_path")):
        return send_file(task["file_path"], as_attachment=True)
    return "Arquivo não encontrado ou processamento incompleto.", 404

def process_youtube(task_id, url, format_type):
    unique_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(DOWNLOAD_FOLDER, f'%(title)s_{unique_id}.%(ext)s')

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                percent = int((downloaded / total) * 90)
                PROGRESS_STORE[task_id] = {"progress": percent, "status": "downloading", "msg": "Baixando mídia..."}
        elif d['status'] == 'finished':
            PROGRESS_STORE[task_id] = {"progress": 95, "status": "converting", "msg": "Finalizando arquivo..."}

    cookies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'youtube.com_cookies.txt')

    # Configuração ultra-resiliente para contornar bloqueio de IP de VPS
    ydl_opts = {
        'cookiefile': cookies_path,
        'outtmpl': output_template,
        'progress_hooks': [progress_hook],
        'noplaylist': True,
        # Força o uso do cliente de incorporação (embed), que raramente exige PO Token em servidores
        'extractor_args': {
            'youtube': {
                'player_client': ['embed', 'mweb']
            }
        },
        'geo_bypass': True,
    }

    if format_type == 'mp3':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })
    else:
        ydl_opts.update({'format': 'best'})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if format_type == 'mp3':
                filename = os.path.splitext(filename)[0] + '.mp3'

        PROGRESS_STORE[task_id] = {
            "progress": 100,
            "status": "completed",
            "file_path": filename,
            "filename": os.path.basename(filename)
        }
    except Exception as e:
        PROGRESS_STORE[task_id] = {"progress": 0, "status": "error", "error": str(e)}

@app.route('/start-youtube', methods=['POST'])
def start_youtube():
    url = request.form.get('url')
    format_type = request.form.get('format_type', 'mp4')
    if not url:
        return jsonify({"error": "Insira uma URL válida."}), 400

    task_id = str(uuid.uuid4())
    PROGRESS_STORE[task_id] = {"progress": 5, "status": "starting", "msg": "Iniciando..."}
    threading.Thread(target=process_youtube, args=(task_id, url, format_type), daemon=True).start()
    return jsonify({"task_id": task_id})

# (Restante das rotas de background remover continuam iguais...)

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)