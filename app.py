import os
import platform
import subprocess
import sys
import re
import json
import shutil
import time
import signal
import threading
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Global variables to track running processes
current_push_process = None
current_create_process = None

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

def get_ollama_command():
    """Get the correct ollama command path for the current system"""
    system = platform.system()
    if system == "Windows":
        # Check if ollama.exe exists in the default installation location
        ollama_path = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Ollama", "ollama.exe")
        if os.path.exists(ollama_path):
            return ollama_path
        # Fallback to PATH
        return "ollama.exe"
    return "ollama"

def check_ollama():
    system = platform.system()
    if system == "Windows":
        # Check if ollama.exe exists in the default installation location
        ollama_path = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Ollama", "ollama.exe")
        return os.path.exists(ollama_path) or shutil.which("ollama.exe") is not None
    return shutil.which("ollama") is not None

def get_uploaded_models():
    upload_folder = Path('uploads')
    if not upload_folder.exists():
        upload_folder.mkdir(exist_ok=True)
    return [f.name for f in upload_folder.glob('*.gguf')]

def get_installed_models():
    try:
        ollama_cmd = get_ollama_command()
        result = subprocess.run([ollama_cmd, 'list'], capture_output=True, text=True)
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.split('\n') if line.strip()]
        return []
    except Exception as e:
        print(f"Error getting installed models: {e}")
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

def send_event(type_='progress', message='', progress=None):
    data = {'type': type_, 'message': message}
    if progress is not None:
        data['progress'] = progress
    # Ensure proper encoding for SSE
    try:
        json_data = json.dumps(data, ensure_ascii=False)
        return f"data: {json_data}\n\n"
    except (TypeError, ValueError) as e:
        # Fallback for encoding issues
        data['message'] = str(message).encode('utf-8', errors='replace').decode('utf-8')
        json_data = json.dumps(data, ensure_ascii=False)
        return f"data: {json_data}\n\n"

def parse_ollama_output(line):
    """Parse ollama's output for progress and status"""
    line = line.lower()
    if 'pulling manifest' in line:
        return ('progress', 'Downloading model manifest...', 10)
    elif 'pulling layer' in line:
        match = re.search(r'(\d+)/(\d+)', line)
        if match:
            current, total = map(int, match.groups())
            progress = 10 + int((current / total) * 40)
            return ('progress', f'Downloading layers ({current}/{total})...', progress)
    elif 'verifying sha256 digest' in line:
        return ('progress', 'Verifying checksums...', 60)
    elif 'writing manifest' in line:
        return ('progress', 'Writing manifest...', 70)
    elif 'creating model' in line:
        return ('progress', 'Creating model...', 80)
    elif 'computing metadata' in line:
        return ('progress', 'Computing metadata...', 85)
    elif 'pushing manifest' in line:
        return ('progress', 'Pushing manifest...', 90)
    elif 'pushing layer' in line:
        return ('progress', 'Pushing layer...', 95)
    elif 'success' in line or 'successfully' in line:
        return ('success', 'Model processed successfully!', 100)
    elif 'error' in line:
        return ('error', line.strip(), None)
    return ('progress', 'Processing...', None)  # Default return for unmatched lines

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

@app.route('/stop_push', methods=['POST'])
def stop_push():
    global current_push_process, current_create_process
    
    stopped = False
    
    if current_create_process and current_create_process.poll() is None:
        try:
            current_create_process.terminate()
            current_create_process = None
            stopped = True
        except Exception as e:
            print(f"Error stopping create process: {e}")
            
    if current_push_process and current_push_process.poll() is None:
        try:
            current_push_process.terminate()
            current_push_process = None
            stopped = True
        except Exception as e:
            print(f"Error stopping push process: {e}")
            
    if stopped:
        return jsonify({'status': 'success', 'message': 'Operation stopped'})
    return jsonify({'status': 'info', 'message': 'No running operation to stop'})

@app.route('/push_model', methods=['POST'])
def push_model():
    global current_push_process, current_create_process
    
    try:
        print(f"Push model request received")
        data = request.get_json()
        if not data:
            print("Error: No JSON data received")
            return jsonify({'status': 'error', 'message': 'No JSON data received'})

        print(f"Received data: {data}")
        repository = data.get('repository')
        base_model = data.get('base_model')
        system_prompt = data.get('system_prompt', '')
        use_uploaded = data.get('use_uploaded', False)
        uploaded_model = data.get('uploaded_model')
        version = data.get('version', 'latest')
        license_text = data.get('license', '')

        print(f"Repository: {repository}, Use uploaded: {use_uploaded}, Uploaded model: {uploaded_model}")

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
            global current_push_process, current_create_process
            try:
                # Provide initial feedback based on model type
                if use_uploaded and uploaded_model:
                    yield send_event('progress', f'Creating model from uploaded file: {uploaded_model}. This may take several minutes for large models...', 5)
                else:
                    yield send_event('progress', f'Creating model from base: {base_model}. This may take several minutes for large models...', 5)
                
                last_heartbeat = time.time()
                
                # Create model
                ollama_cmd = get_ollama_command()
                current_create_process = subprocess.Popen(
                    [ollama_cmd, 'create', '-f', str(modelfile_path), repository_with_version],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    encoding='utf-8',
                    errors='replace'
                )

                last_progress = 5
                while True:
                    # Send heartbeat every 15 seconds to keep connection alive
                    if time.time() - last_heartbeat > 15:
                        yield send_event('heartbeat', 'keepalive', last_progress)
                        last_heartbeat = time.time()
                    
                    # Check if process still exists
                    if current_create_process is None or current_create_process.poll() is not None:
                        break
                    
                    try:
                        output = current_create_process.stdout.readline() if current_create_process.stdout else ''
                        error = current_create_process.stderr.readline() if current_create_process.stderr else ''
                    except:
                        break

                    if current_create_process.poll() is not None and not output and not error:
                        break

                    if error:
                        # Log the actual error output for debugging
                        print(f"Create stderr: {error.strip()}")
                        # Filter out non-error messages that might appear in stderr
                        if "transferring model data" in error.lower():
                            # This indicates upload progress for large models
                            yield send_event('progress', 'Uploading model data to registry... This may take several minutes for large models.', last_progress + 5)
                            continue
                        elif "using existing layer" in error.lower():
                            # This is normal progress for base models
                            yield send_event('progress', 'Using existing model layers...', last_progress + 2)
                            continue
                        elif "writing manifest" in error.lower():
                            yield send_event('progress', 'Writing model manifest...', 80)
                            continue
                        elif "success" in error.lower():
                            yield send_event('progress', 'Model creation completed successfully!', 85)
                            continue
                        yield send_event('error', error.strip())
                        continue
                        print(f"Create stderr: {error.strip()}")
                        # Filter out non-error messages that might appear in stderr
                        if "transferring model data" in error.lower():
                            continue
                        yield send_event('error', error.strip())
                        continue

                    if output:
                        # Log the actual output for debugging
                        print(f"Create stdout: {output.strip()}")
                        result = parse_ollama_output(output.strip())
                        if result:
                            type_, message, progress = result
                            if progress and progress > last_progress:
                                last_progress = progress
                                yield send_event(type_, message, progress)

                # Check if process exists and get return code safely
                return_code = current_create_process.returncode if current_create_process else -1
                if return_code != 0:
                    yield send_event('error', f'Model creation failed with return code {return_code}')
                    return
                current_create_process = None

                # Push model with enhanced progress feedback
                yield send_event('progress', 'Starting model push to registry...', 85)
                yield send_event('progress', 'Please be patient - uploading large models can take several minutes...', 86)
                
                current_push_process = subprocess.Popen(
                    [ollama_cmd, 'push', repository_with_version],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    encoding='utf-8',
                    errors='replace'
                )

                push_started = time.time()
                timeout = 600  # Increased timeout to 10 minutes
                
                while True:
                    # Send heartbeat every 15 seconds to keep connection alive
                    if time.time() - last_heartbeat > 15:
                        elapsed = int(time.time() - push_started)
                        yield send_event('heartbeat', f'Upload in progress... ({elapsed}s elapsed)', 87)
                        last_heartbeat = time.time()
                    
                    # Check if process still exists
                    if current_push_process is None or current_push_process.poll() is not None:
                        break
                    
                    try:
                        output = current_push_process.stdout.readline() if current_push_process.stdout else ''
                        error = current_push_process.stderr.readline() if current_push_process.stderr else ''
                    except:
                        break

                    # Check for timeout
                    if time.time() - push_started > timeout:
                        if current_push_process and current_push_process.poll() is None:
                            current_push_process.terminate()
                        yield send_event('error', 'Push operation timed out after 10 minutes')
                        return

                    if current_push_process.poll() is not None and not output and not error:
                        break

                    if error:
                        # Log the actual error output for debugging
                        print(f"Push stderr: {error.strip()}")
                        # Filter out progress messages
                        error_msg = error.strip()
                        if "pushing" in error_msg.lower() and "layer" in error_msg.lower():
                            yield send_event('progress', f'Uploading layer: {error_msg}', 88)
                            continue
                        elif "writing manifest" in error_msg.lower():
                            yield send_event('progress', 'Writing manifest to registry...', 94)
                            continue
                        yield send_event('error', error_msg)
                        continue

                    if output:
                        # Log the actual output for debugging
                        print(f"Push stdout: {output.strip()}")
                        result = parse_ollama_output(output.strip())
                        if result:
                            type_, message, progress = result
                            if progress and progress > last_progress:
                                last_progress = progress
                                yield send_event(type_, message, progress)
                
                # Check if process exists and get return code safely
                return_code = current_push_process.returncode if current_push_process else -1
                current_push_process = None

                if return_code == 0:
                    # Enhanced verification with pull test
                    yield send_event('progress', 'Verifying model in registry...', 95)
                    try:
                        ollama_cmd = get_ollama_command()
                        model_name = f"{repository}:{version}"
                        
                        # First check if model appears in local list
                        verify_result = subprocess.run(
                            [ollama_cmd, 'list'],
                            capture_output=True,
                            text=True,
                            timeout=30,
                            encoding='utf-8',
                            errors='replace'
                        )
                        
                        if verify_result.returncode == 0 and model_name in verify_result.stdout:
                            yield send_event('progress', 'Model found in local registry, performing pull test...', 96)
                            
                            # Delete local model to ensure pull test is valid
                            yield send_event('progress', 'Removing local model for pull verification...', 97)
                            delete_result = subprocess.run(
                                [ollama_cmd, 'rm', model_name],
                                capture_output=True,
                                text=True,
                                timeout=30,
                                encoding='utf-8',
                                errors='replace'
                            )
                            
                            # Now attempt to pull the model to verify it's in the remote registry
                            yield send_event('progress', 'Pulling model from registry to verify availability...', 98)
                            
                            pull_process = subprocess.Popen(
                                [ollama_cmd, 'pull', model_name],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                bufsize=1,
                                universal_newlines=True,
                                encoding='utf-8',
                                errors='replace'
                            )
                            
                            pull_started = time.time()
                            pull_timeout = 600 # 10 minutes for verification pull
                            
                            while True:
                                # Send heartbeat every 15 seconds
                                if time.time() - last_heartbeat > 15:
                                    elapsed = int(time.time() - pull_started)
                                    yield send_event('heartbeat', f'Verification in progress... ({elapsed}s elapsed)', 98)
                                    last_heartbeat = time.time()
                                
                                if pull_process.poll() is not None:
                                    break
                                    
                                # Check for timeout
                                if time.time() - pull_started > pull_timeout:
                                    pull_process.terminate()
                                    yield send_event('error', 'Verification pull timed out')
                                    return
                                    
                                time.sleep(0.5)
                            
                            if pull_process.returncode == 0:
                                # Clean up the pulled model (optional)
                                yield send_event('progress', 'Cleaning up verification model...', 99)
                                subprocess.run(
                                    [ollama_cmd, 'rm', model_name],
                                    capture_output=True,
                                    text=True,
                                    timeout=30,
                                    encoding='utf-8',
                                    errors='replace'
                                )
                                
                                save_model_metadata(repository, version, license_text, system_prompt)
                                yield send_event('success', 'Model pushed and verified successfully! The model is available in the registry.', 100)
                            else:
                                stderr_output = pull_process.stderr.read() if pull_process.stderr else "Unknown error"
                                yield send_event('error', f'Pull verification failed. The model may not be available in the remote registry. Error: {stderr_output}')
                        else:
                            yield send_event('error', f'Model {model_name} not found in local registry after push.')
                    except subprocess.TimeoutExpired:
                        yield send_event('error', 'Verification timeout - model may not be available yet or is too large')
                    except Exception as verify_error:
                        yield send_event('error', f'Verification error: {str(verify_error)}')
                else:
                    yield send_event('error', f'Model push failed with return code {return_code}')

            except Exception as e:
                print(f"Error in generate_progress: {str(e)}")
                yield send_event('error', f'Process error: {str(e)}')
            finally:
                current_create_process = None
                current_push_process = None

        try:
            return Response(generate_progress(), mimetype='text/event-stream')
        except Exception as e:
            print(f"Error creating Response: {str(e)}")
            return jsonify({'status': 'error', 'message': f'Server error: {str(e)}'})

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
        try:
            upload_path.unlink()
            return jsonify({'status': 'success', 'message': 'Model deleted successfully'})
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'Error deleting model: {str(e)}'})
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
    
    # Auto-select localhost for testing
    host = 'localhost'
    port = 5000
    print(f"Starting server on {host}:{port}")
    print("You can change this by modifying the host and port variables in app.py")
    
    print(f"\nStarting Ollama Pusher server...")
    print(f"Server URL: http://{host}:{port}")
    print("Press Ctrl+C to stop the server")
    print("---------------------------")
    
    app.run(host=host, port=port)
