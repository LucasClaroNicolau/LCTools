import os
import time
import threading
import uuid
import shutil
from urllib.parse import urlparse

from flask import Flask, render_template, request, send_file, jsonify
from rembg import remove
from PIL import Image

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "lc-tools-chave-secreta-para-mensagens-flash")

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
DOWNLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
FILE_EXPIRATION_SECONDS = 300
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

PROGRESS_STORE = {}
PROGRESS_LOCK = threading.RLock()


def clear_download_folder():
    """Limpa arquivos antigos da pasta temporária na inicialização."""
    if os.path.exists(DOWNLOAD_FOLDER):
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.remove(file_path)
            except OSError as exc:
                app.logger.warning("Falha ao limpar %s: %s", file_path, exc)


# Não apagamos toda a pasta ao iniciar: isso poderia remover arquivos de tarefas ativas
# quando o servidor reinicia ou inicia mais de um worker. A limpeza por expiração cuida disso.


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/remove-bg')
def remove_bg_page():
    return render_template('background_remover.html')


@app.route('/progress/<task_id>')
def get_progress(task_id):
    with PROGRESS_LOCK:
        task = dict(PROGRESS_STORE.get(task_id, {"progress": 0, "status": "starting"}))
    # Não expõe caminhos internos do servidor ao navegador.
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
    return "Arquivo não encontrado ou processamento incompleto. Tente gerar o arquivo novamente.", 404


def validate_youtube_url(url):
    """Aceita somente links HTTP(S) dos domínios oficiais do YouTube."""
    try:
        parsed = urlparse((url or '').strip())
        host = (parsed.hostname or '').lower().rstrip('.')
        allowed_hosts = {
            'youtube.com', 'www.youtube.com', 'm.youtube.com',
            'music.youtube.com', 'youtu.be', 'www.youtube-nocookie.com',
            'youtube-nocookie.com'
        }
        return (
            parsed.scheme in ('http', 'https')
            and host in allowed_hosts
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 80, 443)
        )
    except (TypeError, ValueError):
        return False


def _set_task(task_id, **values):
    """Atualiza o estado de uma tarefa de forma consistente entre threads."""
    with PROGRESS_LOCK:
        current = dict(PROGRESS_STORE.get(task_id, {}))
        current.update(values)
        PROGRESS_STORE[task_id] = current


def _friendly_youtube_error(exc):
    """Converte erros comuns em mensagens úteis sem esconder o erro no log."""
    message = str(exc or '').strip()
    lowered = message.lower()
    if 'sign in to confirm' in lowered or 'not a bot' in lowered or 'confirm you’re not a bot' in lowered:
        return (
            'O YouTube exigiu uma verificação de acesso e o download não pôde continuar. '
            'Atualize o yt-dlp e, se você tiver autorização para acessar esse conteúdo, '
            'configure um arquivo de cookies válido. Consulte o log do servidor para detalhes.'
        )
    if 'private video' in lowered or 'members-only' in lowered or 'members only' in lowered:
        return 'Este vídeo é privado ou restrito e não está acessível com a sessão atual.'
    if 'video unavailable' in lowered or 'is not available' in lowered:
        return 'O vídeo não está disponível para acesso. Verifique o link e as restrições do conteúdo.'
    if 'ffmpeg' in lowered and ('not found' in lowered or 'not installed' in lowered or 'unable to' in lowered):
        return 'O FFmpeg não foi encontrado ou não conseguiu processar o arquivo. Verifique a instalação.'
    if 'timed out' in lowered or 'timeout' in lowered or 'connection' in lowered:
        return 'Falha de conexão com o YouTube. Verifique a internet do servidor e tente novamente.'
    return 'Não foi possível baixar este conteúdo. Consulte o log do servidor para ver o motivo.'


def process_youtube_download(task_id, url, format_type):
    """Baixa mídia autorizada para disco local e publica o arquivo final para /get-file."""
    task_dir = os.path.join(DOWNLOAD_FOLDER, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                'A biblioteca yt-dlp não está instalada. Execute: python -m pip install -U yt-dlp'
            ) from exc

        if format_type == 'mp3' and not shutil.which('ffmpeg'):
            raise RuntimeError(
                'O FFmpeg não está instalado ou não está no PATH. Ele é necessário para converter áudio para MP3.'
            )

        output_template = os.path.join(task_dir, 'media.%(ext)s')

        def progress_hook(info):
            status = info.get('status')
            if status == 'downloading':
                downloaded = info.get('downloaded_bytes') or 0
                total = info.get('total_bytes') or info.get('total_bytes_estimate') or 0
                if total:
                    progress = min(90, max(10, int(10 + 80 * downloaded / total)))
                else:
                    progress = 20
                _set_task(task_id, progress=progress, status='processing', msg='Baixando mídia do YouTube...')
            elif status == 'finished':
                _set_task(task_id, progress=92, status='processing', msg='Download recebido. Preparando o arquivo...')

        options = {
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': False,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            'progress_hooks': [progress_hook],
            'restrictfilenames': True,
            'overwrites': True,
            'continuedl': True,
        }

        # Cookies são opcionais e nunca são lidos automaticamente do navegador.
        # Configure YTDLP_COOKIES_FILE com um arquivo Netscape obtido legitimamente, se necessário.
        cookies_file = os.environ.get('YTDLP_COOKIES_FILE', '').strip()
        if cookies_file:
            if not os.path.isfile(cookies_file):
                raise RuntimeError('YTDLP_COOKIES_FILE aponta para um arquivo que não existe.')
            options['cookiefile'] = cookies_file

        if format_type == 'mp3':
            options.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            options.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
                'merge_output_format': 'mp4',
            })

        _set_task(task_id, progress=5, status='processing', msg='Conectando ao YouTube...')

        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            title = info.get('title') or 'youtube_media'

        candidates = []
        for filename in os.listdir(task_dir):
            full_path = os.path.join(task_dir, filename)
            if os.path.isfile(full_path) and not filename.endswith(('.part', '.ytdl', '.json')):
                candidates.append(full_path)
        if not candidates:
            raise RuntimeError('O YouTube não gerou um arquivo final para download.')

        # Preferir a extensão esperada e só usar a data como desempate.
        expected_ext = '.mp3' if format_type == 'mp3' else '.mp4'
        matching = [path for path in candidates if os.path.splitext(path)[1].lower() == expected_ext]
        final_path = max(matching or candidates, key=os.path.getmtime)
        final_ext = os.path.splitext(final_path)[1].lower()
        if final_ext != expected_ext:
            raise RuntimeError(
                f'O arquivo gerado não ficou no formato solicitado ({expected_ext}). '
                'Verifique o FFmpeg e os formatos disponíveis para este vídeo.'
            )

        safe_title = ''.join(ch if ch.isalnum() or ch in (' ', '-', '_', '.') else '_' for ch in title)
        safe_title = ' '.join(safe_title.split()).strip(' ._')[:150] or 'youtube_media'
        _set_task(
            task_id,
            progress=100,
            status='completed',
            msg='Arquivo pronto para download.',
            file_path=final_path,
            filename=safe_title + final_ext,
            completed_at=time.time(),
        )

    except Exception as exc:
        app.logger.exception('Falha no download do YouTube (tarefa %s)', task_id)
        try:
            shutil.rmtree(task_dir, ignore_errors=True)
        except OSError:
            pass
        _set_task(task_id, progress=0, status='error', error=_friendly_youtube_error(exc), completed_at=time.time())


@app.route('/start-youtube', methods=['POST'])
def start_youtube():
    url = (request.form.get('url') or '').strip()
    format_type = (request.form.get('format_type') or 'mp4').lower()

    if not validate_youtube_url(url):
        return jsonify({"error": "Insira um link válido do YouTube (youtube.com ou youtu.be)."}), 400
    if format_type not in ('mp4', 'mp3'):
        return jsonify({"error": "Formato inválido. Escolha MP4 ou MP3."}), 400

    task_id = str(uuid.uuid4())
    _set_task(task_id, progress=1, status='starting', msg='Iniciando...')
    threading.Thread(
        target=process_youtube_download,
        args=(task_id, url, format_type),
        daemon=True
    ).start()
    return jsonify({"task_id": task_id})


# --- Lógica de Remoção de Fundo (mantida) ---
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
        PROGRESS_STORE[task_id] = {
            "progress": 100,
            "status": "completed",
            "file_path": output_path,
            "filename": f"sem_fundo_{os.path.basename(original_filename)}",
            "completed_at": time.time(),
        }
    except Exception as exc:
        app.logger.exception("Falha ao remover fundo (tarefa %s)", task_id)
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
    PROGRESS_STORE[task_id] = {"progress": 10, "status": "starting", "msg": "Iniciando..."}
    threading.Thread(target=process_bg_removal, args=(task_id, temp_input_path, safe_name), daemon=True).start()
    return jsonify({"task_id": task_id})


def cleanup_expired_files():
    """Remove arquivos concluídos após o prazo configurado sem tocar em tarefas em andamento."""
    while True:
        time.sleep(30)
        now = time.time()
        with PROGRESS_LOCK:
            expired = [
                (task_id, dict(task))
                for task_id, task in PROGRESS_STORE.items()
                if task.get('completed_at') and now - task['completed_at'] > FILE_EXPIRATION_SECONDS
            ]
            for task_id, _task in expired:
                PROGRESS_STORE.pop(task_id, None)
        for task_id, task in expired:
            try:
                # YouTube usa uma pasta própria por tarefa; a remoção de fundo salva um arquivo individual.
                youtube_dir = os.path.join(DOWNLOAD_FOLDER, task_id)
                if os.path.isdir(youtube_dir):
                    shutil.rmtree(youtube_dir, ignore_errors=True)
                else:
                    file_path = task.get('file_path')
                    if file_path and os.path.isfile(file_path):
                        os.remove(file_path)
            except OSError as exc:
                app.logger.warning('Falha ao excluir arquivo expirado da tarefa %s: %s', task_id, exc)


threading.Thread(target=cleanup_expired_files, daemon=True).start()


if __name__ == '__main__':
    # Em produção, execute com Gunicorn/uWSGI e debug desativado.
    app.run(host='0.0.0.0', debug=False, port=5000)
