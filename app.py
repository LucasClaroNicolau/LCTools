import os
import time
import threading
import uuid
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
import yt_dlp
from rembg import remove
from PIL import Image

app = Flask(__name__)
app.secret_key = "chave-secreta-para-mensagens-flash"

# Configuração de pastas e tempo de expiração
DOWNLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
FILE_EXPIRATION_SECONDS = 300  # 5 minutos

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)


# --- Gerenciamento de Limpeza Automática ---

def clear_download_folder():
    """Remove todos os arquivos da pasta de temporários ao iniciar o servidor."""
    if os.path.exists(DOWNLOAD_FOLDER):
        for filename in os.listdir(DOWNLOAD_FOLDER):
            file_path = os.path.join(DOWNLOAD_FOLDER, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.remove(file_path)
                    print(f"[STARTUP CLEANUP] Arquivo antigo removido: {filename}")
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
                    # Verifica a data de criação/modificação do arquivo
                    file_age = now - os.path.getmtime(file_path)
                    if file_age > FILE_EXPIRATION_SECONDS:
                        os.remove(file_path)
                        print(f"[CLEANUP] Arquivo removido por expiração: {filename}")
        except Exception as e:
            print(f"[CLEANUP ERROR] {e}")

        time.sleep(60)  # Executa a verificação a cada 1 minuto


# 1. Executa a limpeza completa no momento da inicialização do app
clear_download_folder()

# 2. Inicia a thread de limpeza contínua em background
cleaner_thread = threading.Thread(target=auto_clean_files, daemon=True)
cleaner_thread.start()

# --- Rotas Principais ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/youtube', methods=['POST'])
def youtube_download():
    url = request.form.get('url')
    format_type = request.form.get('format_type')  # 'mp3' ou 'mp4'

    if not url:
        flash("Por favor, insira uma URL do YouTube.", "danger")
        return redirect(url_for('index'))

    unique_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(DOWNLOAD_FOLDER, f'%(title)s_{unique_id}.%(ext)s')

    ydl_opts = {}

    if format_type == 'mp3':
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
    else:  # mp4
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': output_template,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            if format_type == 'mp3':
                # O ffmpeg altera a extensão para .mp3 após o pós-processamento
                filename = os.path.splitext(filename)[0] + '.mp3'

        download_name = os.path.basename(filename)
        return send_file(filename, as_attachment=True)

    except Exception as e:
        flash(f"Erro ao processar vídeo do YouTube: {str(e)}", "danger")
        return redirect(url_for('index'))


@app.route('/remove-bg', methods=['GET', 'POST'])
def remove_background():
    if request.method == 'POST':
        file = request.files.get('image')
        if not file or file.filename == '':
            flash("Nenhuma imagem selecionada.", "warning")
            return redirect(url_for('remove_background'))

        try:
            input_image = Image.open(file.stream)
            output_image = remove(input_image)

            unique_filename = f"no_bg_{uuid.uuid4().hex[:8]}.png"
            output_path = os.path.join(DOWNLOAD_FOLDER, unique_filename)
            output_image.save(output_path, format="PNG")

            return send_file(output_path, as_attachment=True, download_name=f"sem_fundo_{file.filename}.png")

        except Exception as e:
            flash(f"Erro ao processar a imagem: {str(e)}", "danger")
            return redirect(url_for('remove_background'))

    return render_template('background_remover.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)