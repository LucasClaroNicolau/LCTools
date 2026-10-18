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

# Limite máximo de upload (50 MB)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

DOWNLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
FILE_EXPIRATION_SECONDS = 300  # 5 minutos

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Dicionário em memória para armazenar o progresso dos downloads
PROGRESS_STORE = {}


# --- Gerenciamento de Limpeza Automática ---

def clear_download_folder():
    """Remove todos os arquivos antigos da pasta de temporários ao iniciar o servidor."""
    if os.path.exists(DOWNLOAD_FOLDER):
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.remove(file_path)
            except Exception as e:
                print(f"[STARTUP CLEANUP ERROR] Erro ao deletar {filename}: {e}")


def auto_clean_files():
    """Thread em segundo plano para excluir arquivos com mais de 5 minutos."""
    while True:
        now = time.time()
        try:
            for filename in os.listdir(DOWNLOAD_FOLDER):
                file_path = os.path.join(DOWNLOAD_FOLDER, filename)
                if os.path.isfile(file_path):
                    if (now - os.path.getmtime(file_path)) > FILE_EXPIRATION_SECONDS:
                        os.remove(file_path)
                        print(f"[CLEANUP] Arquivo removido por expiração: {filename}")
        except Exception as e:
            print(f"[CLEANUP ERROR] {e}")
        time.sleep(60)


# Executa limpeza inicial e dispara a thread de background
clear_download_folder()
cleaner_thread = threading.Thread(target=auto_clean_files, daemon=True)
cleaner_thread.start()


# --- Rotas Principais e API de Progresso ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/remove-bg')
def remove_bg_page():
    return render_template('background_remover.html')


@app.route('/progress/<task_id>')
def get_progress(task_id):
    """Retorna o percentual e status em tempo real via JSON."""
    data = PROGRESS_STORE.get(task_id, {"progress": 0, "status": "starting"})
    return jsonify(data)


@app.route('/get-file/<task_id>')
def get_file(task_id):
    """Baixa o arquivo gerado assim que o progresso chega a 100%."""
    task = PROGRESS_STORE.get(task_id)
    if task and task.get("status") == "completed" and os.path.exists(task.get("file_path")):
        return send_file(task["file_path"], as_attachment=True)
    return "Arquivo não encontrado ou processamento incompleto.", 404


# --- Lógica do YouTube ---

def process_youtube(task_id, url, format_type):
    unique_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(DOWNLOAD_FOLDER, f'%(title)s_{unique_id}.%(ext)s')

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                percent = int((downloaded / total) * 90)  # Reserva 10% para o pós-processamento
                PROGRESS_STORE[task_id] = {
                    "progress": percent,
                    "status": "downloading",
                    "msg": "Baixando mídia do YouTube..."
                }
        elif d['status'] == 'finished':
            PROGRESS_STORE[task_id] = {
                "progress": 95,
                "status": "converting",
                "msg": "Convertendo / Finalizando arquivo..."
            }

    # Configuração forçando apenas o cliente Web e usando os cookies/headers de navegador
    ydl_opts = {
        'cookiefile': 'youtube.com_cookies.txt',
        'outtmpl': output_template,
        'progress_hooks': [progress_hook],
        'noplaylist': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['web']  # Trava estritamente no web player para evitar erros 400 de mobile
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        }
    }

    if format_type == 'mp3':
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        ydl_opts.update({
            'format': 'bestvideo+bestaudio/best',  # Máxima qualidade combinada (requer ffmpeg)
        })

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
        return jsonify({"error": "Insira uma URL do YouTube válida."}), 400

    task_id = str(uuid.uuid4())
    PROGRESS_STORE[task_id] = {"progress": 5, "status": "starting", "msg": "Iniciando conexão..."}

    thread = threading.Thread(target=process_youtube, args=(task_id, url, format_type))
    thread.start()

    return jsonify({"task_id": task_id})


# --- Lógica de Remoção de Fundo ---

def process_bg_removal(task_id, input_image_path, original_filename):
    try:
        PROGRESS_STORE[task_id] = {"progress": 25, "status": "processing", "msg": "Carregando imagem..."}
        time.sleep(0.3)

        input_image = Image.open(input_image_path)
        PROGRESS_STORE[task_id] = {"progress": 55, "status": "processing", "msg": "Removendo o fundo com IA..."}

        output_image = remove(input_image)
        PROGRESS_STORE[task_id] = {"progress": 85, "status": "processing", "msg": "Gerando arquivo em PNG transparente..."}

        unique_filename = f"no_bg_{uuid.uuid4().hex[:8]}.png"
        output_path = os.path.join(DOWNLOAD_FOLDER, unique_filename)
        output_image.save(output_path, format="PNG")

        if os.path.exists(input_image_path):
            os.remove(input_image_path)

        PROGRESS_STORE[task_id] = {
            "progress": 100,
            "status": "completed",
            "file_path": output_path,
            "filename": f"sem_fundo_{original_filename}"
        }
    except Exception as e:
        PROGRESS_STORE[task_id] = {"progress": 0, "status": "error", "error": str(e)}


@app.route('/start-remove-bg', methods=['POST'])
def start_remove_bg():
    file = request.files.get('image')
    if not file or file.filename == '':
        return jsonify({"error": "Nenhuma imagem selecionada."}), 400

    temp_input_path = os.path.join(DOWNLOAD_FOLDER, f"temp_{uuid.uuid4().hex}_{file.filename}")
    file.save(temp_input_path)

    task_id = str(uuid.uuid4())
    PROGRESS_STORE[task_id] = {"progress": 10, "status": "starting", "msg": "Iniciando a inteligência artificial..."}

    thread = threading.Thread(target=process_bg_removal, args=(task_id, temp_input_path, file.filename))
    thread.start()

    return jsonify({"task_id": task_id})


@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "O arquivo é muito grande! O limite máximo é 50 MB."}), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)