import os
import time
import threading
import uuid
from flask import Flask, render_template, request, send_file, jsonify
from rembg import remove
from PIL import Image

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "lc-tools-chave-secreta-para-mensagens-flash")

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Limite de 50MB
DOWNLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
FILE_EXPIRATION_SECONDS = 300
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

PROGRESS_STORE = {}
PROGRESS_LOCK = threading.RLock()


@app.route('/')
def index():
    return render_template('background_remover.html')


@app.route('/progress/<task_id>')
def get_progress(task_id):
    with PROGRESS_LOCK:
        task = dict(PROGRESS_STORE.get(task_id, {"progress": 0, "status": "starting"}))
    task.pop('file_path', None)
    return jsonify(task)


@app.route('/get-file/<task_id>')
def get_file(task_id):
    with PROGRESS_LOCK:
        task = dict(PROGRESS_STORE.get(task_id, {}))
    file_path = task.get('file_path')
    if task.get('status') == 'completed' and file_path and os.path.isfile(file_path):
        return send_file(
            file_path,
            as_attachment=True,
            download_name=task.get('filename') or os.path.basename(file_path),
            conditional=True,
        )
    return "Arquivo não encontrado ou processamento incompleto.", 404


def process_bg_removal(task_id, input_image_path, original_filename):
    try:
        with PROGRESS_LOCK:
            PROGRESS_STORE[task_id] = {"progress": 25, "status": "processing", "msg": "Carregando imagem..."}

        input_image = Image.open(input_image_path)

        with PROGRESS_LOCK:
            PROGRESS_STORE[task_id] = {"progress": 55, "status": "processing", "msg": "Removendo o fundo com IA..."}

        output_image = remove(input_image)
        output_path = os.path.join(DOWNLOAD_FOLDER, f"no_bg_{uuid.uuid4().hex[:8]}.png")
        output_image.save(output_path, format="PNG")

        if os.path.exists(input_image_path):
            os.remove(input_image_path)

        with PROGRESS_LOCK:
            PROGRESS_STORE[task_id] = {
                "progress": 100,
                "status": "completed",
                "file_path": output_path,
                "filename": f"sem_fundo_{os.path.basename(original_filename)}",
                "completed_at": time.time(),
            }
    except Exception as exc:
        app.logger.exception("Falha ao remover fundo (tarefa %s)", task_id)
        with PROGRESS_LOCK:
            PROGRESS_STORE[task_id] = {"progress": 0, "status": "error", "error": str(exc)}


@app.route('/start-remove-bg', methods=['POST'])
def start_remove_bg():
    file = request.files.get('image')
    if not file or file.filename == '':
        return jsonify({"error": "Nenhuma imagem selecionada."}), 400

    safe_name = os.path.basename(file.filename)
    temp_input_path = os.path.join(DOWNLOAD_FOLDER, f"temp_{uuid.uuid4().hex}_{safe_name}")
    file.save(temp_input_path)

    task_id = str(uuid.uuid4())
    with PROGRESS_LOCK:
        PROGRESS_STORE[task_id] = {"progress": 10, "status": "starting", "msg": "Iniciando..."}

    threading.Thread(target=process_bg_removal, args=(task_id, temp_input_path, safe_name), daemon=True).start()
    return jsonify({"task_id": task_id})


def cleanup_expired_files():
    """Remove arquivos salvos após o tempo configurado."""
    while True:
        time.sleep(30)
        now = time.time()
        with PROGRESS_LOCK:
            expired = [
                (task_id, dict(task))
                for task_id, task in PROGRESS_STORE.items()
                if task.get('completed_at') and now - task['completed_at'] > FILE_EXPIRATION_SECONDS
            ]
            for task_id, _ in expired:
                PROGRESS_STORE.pop(task_id, None)

        for _, task in expired:
            try:
                file_path = task.get('file_path')
                if file_path and os.path.isfile(file_path):
                    os.remove(file_path)
            except OSError as exc:
                app.logger.warning('Falha ao excluir arquivo expirado: %s', exc)


threading.Thread(target=cleanup_expired_files, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000)