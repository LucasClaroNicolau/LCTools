import os
import time
import threading
import uuid
import requests
from flask import Flask, render_template, request, send_file, jsonify
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


def process_youtube_via_api(task_id, url, format_type):
    try:
        PROGRESS_STORE[task_id] = {"progress": 30, "status": "processing", "msg": "A contactar API de extração..."}

        api_instances = [
            'https://api.cobalt.tools/api/json',
            'https://cobalt-api.kwi.mobi/api/json'
        ]

        download_url = None
        for instance in api_instances:
            try:
                response = requests.post(instance, json={
                    "url": url,
                    "downloadMode": "audio" if format_type == 'mp3' else "auto",
                    "audioFormat": "mp3"
                }, headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0'
                }, timeout=10)

                data = response.json()
                if data.get('url'):
                    download_url = data['url']
                    break
            except Exception:
                continue

        if not download_url:
            raise Exception("Não foi possível extrair o link de download pelas APIs públicas.")

        PROGRESS_STORE[task_id] = {
            "progress": 100,
            "status": "completed",
            "file_path": download_url,  # Redireciona o link direto obtido
            "filename": "youtube_media"
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
    PROGRESS_STORE[task_id] = {"progress": 10, "status": "starting", "msg": "Iniciando..."}
    threading.Thread(target=process_youtube_via_api, args=(task_id, url, format_type), daemon=True).start()
    return jsonify({"task_id": task_id})


# --- Lógica de Remoção de Fundo ---
def process_bg_removal(task_id, input_image_path, original_filename):
    try:
        PROGRESS_STORE[task_id] = {"progress": 25, "status": "processing", "msg": "Carregando imagem..."}
        input_image = Image.open(input_image_path)
        PROGRESS_STORE[task_id] = {"progress": 55, "status": "processing", "msg": "Removendo o fundo com IA..."}
        output_image = remove(input_image)
        output_path = os.path.join(DOWNLOAD_FOLDER, f"no_bg_{uuid.uuid4().hex[:8]}.png")
        output_image.save(output_path, format="PNG")
        if os.path.exists(input_image_path):
            os.remove(input_image_path)
        PROGRESS_STORE[task_id] = {"progress": 100, "status": "completed", "file_path": output_path,
                                   "filename": f"sem_fundo_{original_filename}"}
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
    PROGRESS_STORE[task_id] = {"progress": 10, "status": "starting", "msg": "Iniciando..."}
    threading.Thread(target=process_bg_removal, args=(task_id, temp_input_path, file.filename), daemon=True).start()
    return jsonify({"task_id": task_id})


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)