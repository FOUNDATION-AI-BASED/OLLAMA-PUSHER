import os
import platform
import subprocess
import sys
from flask import Flask, render_template, jsonify, request, Response
from pathlib import Path
import shutil
import json
from werkzeug.utils import secure_filename

app = Flask(__name__)

def get_ollama_public_key():
    system = platform.system()
    key_path = None
    
    if system == "Darwin":  # macOS
        key_path = os.path.expanduser("~/.ollama/id_ed25519.pub")
    elif system == "Linux":
        key_path = "/usr/share/ollama/.ollama/id_ed25519.pub"
    elif system == "Windows":
        key_path = os.path.join(os.path.expanduser("~"), ".ollama", "id_ed25519.pub")
    
    if key_path and os.path.exists(key_path):
        with open(key_path, 'r') as f:
            return f.read().strip()
    return None

def check_ollama():
    system = platform.system()
    if system == "Windows":
        return shutil.which("ollama.exe") is not None
    return shutil.which("ollama") is not None

def get_uploaded_models():
    upload_folder = Path('uploads')
    if not upload_folder.exists():
        upload_folder.mkdir(exist_ok=True)
    return [f.name for f in upload_folder.glob('*.gguf')]

def get_installed_models():
    try:
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True)
        return [line for line in result.stdout.split('\n') if line.strip()]
    except:
        return []

def save_model_metadata(repository, version, license_text, system_prompt=None):
    metadata_dir = Path('metadata')
    metadata_dir.mkdir(exist_ok=True)
    
    metadata_file = metadata_dir / f"{repository.replace('/', '_')}.json"
    metadata = {
        'version': version,
        'license': license_text,
        'system_prompt': system_prompt
    }
    
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f)

@app.route('/')
def index():
    public_key = get_ollama_public_key()
    return render_template('index.html',
                         uploaded_models=get_uploaded_models(),
                         installed_models=get_installed_models(),
                         public_key=public_key)

@app.route('/upload_model', methods=['POST'])
def upload_model():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'})
    
    if not file.filename.endswith('.gguf'):
        return jsonify({'error': 'Only .gguf files are supported'})
    
    upload_folder = Path('uploads')
    upload_folder.mkdir(exist_ok=True)
    file_path = upload_folder / secure_filename(file.filename)
    
    try:
        file.save(file_path)
        return jsonify({'status': 'success', 'filename': file.filename})
    except Exception as e:
        if file_path.exists():
            file_path.unlink()
        return jsonify({'error': str(e)})

@app.route('/push_model', methods=['POST'])
def push_model():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': 'No JSON data received'})

        repository = data.get('repository')
        base_model = data.get('base_model')
        system_prompt = data.get('system_prompt', '')
        use_uploaded = data.get('use_uploaded', False)
        uploaded_model = data.get('uploaded_model')
        version = data.get('version', 'latest')
        license_text = data.get('license', '')

        modelfile_content = []
        
        if use_uploaded and uploaded_model:
            upload_path = Path('uploads') / uploaded_model
            if not upload_path.exists():
                return jsonify({'status': 'error', 'message': 'Uploaded model file not found'})
            modelfile_content.append(f"FROM {str(upload_path.absolute())}")
        else:
            if not base_model:
                return jsonify({'status': 'error', 'message': 'Base model is required'})
            modelfile_content.append(f"FROM {base_model}")

        if system_prompt:
            modelfile_content.append(f'SYSTEM """{system_prompt}"""')
        
        if license_text:
            modelfile_content.append(f'LICENSE """{license_text}"""')

        modelfile_path = Path('Modelfile')
        modelfile_path.write_text('\n'.join(modelfile_content))

        repository_with_version = f"{repository}:{version}"

        def generate_progress():
            create_process = subprocess.Popen(
                ['ollama', 'create', '-f', str(modelfile_path), repository_with_version],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            while True:
                output = create_process.stdout.readline()
                error = create_process.stderr.readline()

                if output == '' and error == '' and create_process.poll() is not None:
                    break

                if output:
                    yield f"data: {json.dumps({'progress': output.strip()})}\n\n"
                if error:
                    yield f"data: {json.dumps({'progress': f'Error: {error.strip()}'})}\n\n"

            if create_process.returncode != 0:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Model creation failed'})}\n\n"
                return

            push_process = subprocess.Popen(
                ['ollama', 'push', repository_with_version],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            while True:
                output = push_process.stdout.readline()
                error = push_process.stderr.readline()

                if output == '' and error == '' and push_process.poll() is not None:
                    break

                if output:
                    yield f"data: {json.dumps({'progress': output.strip()})}\n\n"
                if error:
                    yield f"data: {json.dumps({'progress': f'Error: {error.strip()}'})}\n\n"

            if push_process.returncode == 0:
                save_model_metadata(repository, version, license_text, system_prompt)
                yield f"data: {json.dumps({'status': 'success', 'message': 'Model pushed successfully'})}\n\n"
            else:
                yield f"data: {json.dumps({'status': 'error', 'message': 'Model push failed'})}\n\n"

        return Response(generate_progress(), mimetype='text/event-stream')

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/refresh_models', methods=['GET'])
def refresh_models():
    return jsonify({
        'uploaded_models': get_uploaded_models(),
        'installed_models': get_installed_models()
    })

@app.route('/delete_model', methods=['POST'])
def delete_model():
    data = request.json
    model_name = data.get('model_name')
    if not model_name:
        return jsonify({'status': 'error', 'message': 'No model name provided'})
    
    upload_path = Path('uploads') / model_name
    if upload_path.exists():
        upload_path.unlink()
        return jsonify({'status': 'success', 'message': 'Model deleted successfully'})
    else:
        return jsonify({'status': 'error', 'message': 'Model not found'})

def shutdown_server():
    os._exit(0)

@app.route('/shutdown', methods=['POST'])
def shutdown():
    shutdown_server()
    return jsonify({'status': 'success', 'message': 'Server shutting down...'})

if __name__ == '__main__':
    if not check_ollama():
        print("Error: Ollama must be installed to use this application.")
        sys.exit(1)
    
    print("\nOllama Pusher Web Interface")
    print("---------------------------")
    print("1. localhost (127.0.0.1)")
    print("2. All interfaces (0.0.0.0)")
    
    while True:
        try:
            choice = input("\nSelect host option (1 or 2): ").strip()
            if choice in ['1', '2']:
                host = 'localhost' if choice == '1' else '0.0.0.0'
                break
            print("Please enter 1 or 2")
        except ValueError:
            print("Please enter 1 or 2")
    
    while True:
        try:
            port = int(input("\nEnter port number (1024-65535): "))
            if 1024 <= port <= 65535:
                break
            print("Please enter a valid port number between 1024 and 65535")
        except ValueError:
            print("Please enter a valid port number")
    
    print(f"\nStarting Ollama Pusher server...")
    print(f"Server URL: http://{host}:{port}")
    print("Press Ctrl+C to stop the server")
    print("---------------------------")
    
    app.run(host=host, port=port)
