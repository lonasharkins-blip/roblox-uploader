import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

import requests
from flask import Flask, jsonify, render_template, request
from pygltflib import (
    GLTF2,
    BufferView,
    Image,
    Material,
    PbrMetallicRoughness,
    Texture,
    TextureInfo,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuração (vem de variáveis de ambiente — configure isso no Render)
# ---------------------------------------------------------------------------
ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY", "")
ROBLOX_USER_ID = os.environ.get("ROBLOX_USER_ID", "")
APP_SECRET = os.environ.get("APP_SECRET", "")  # senha simples pra proteger o site

ROBLOX_ASSETS_URL = "https://apis.roblox.com/assets/v1/assets"
ROBLOX_OPERATION_URL = "https://apis.roblox.com/assets/v1/operations/{}"

MESH_EXTENSIONS = {".glb", ".gltf", ".obj", ".fbx", ".dae", ".3ds", ".stl", ".ply"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # um pouco acima do limite de 20MB da Roblox p/ dar erro melhor

# Guarda o status dos jobs em memória (ok pra uso pessoal / single instance)
JOBS = {}


def find_main_mesh_file(folder):
    """Procura dentro de uma pasta extraída qual arquivo é o 'modelo principal'."""
    candidates = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in MESH_EXTENSIONS:
                candidates.append(os.path.join(root, f))

    if not candidates:
        return None

    # Preferência: glb/gltf (já empacotado) > obj > fbx > outros
    priority = {".glb": 0, ".gltf": 1, ".obj": 2, ".fbx": 3, ".dae": 4, ".stl": 5, ".ply": 6, ".3ds": 7}
    candidates.sort(key=lambda p: priority.get(os.path.splitext(p)[1].lower(), 99))
    return candidates[0]


def attach_fallback_texture(glb_path, texture_path, work_dir):
    """Abre um .glb e força a imagem em `texture_path` como a textura base
    de qualquer material que esteja sem nenhuma textura aplicada (e cria um
    material novo pras meshes que nem isso têm). Não toca em materiais que
    já têm textura própria. Depende do mesh já ter UV mapping — a maioria
    tem, mesmo sem a textura linkada."""
    gltf = GLTF2().load_binary(glb_path)

    with open(texture_path, "rb") as f:
        img_bytes = f.read()

    ext = os.path.splitext(texture_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"

    blob = gltf.binary_blob() or b""
    pad = (-len(blob)) % 4
    blob = blob + b"\x00" * pad

    offset = len(blob)
    new_blob = blob + img_bytes
    new_blob = new_blob + b"\x00" * ((-len(new_blob)) % 4)

    buffer_view_index = len(gltf.bufferViews)
    gltf.bufferViews.append(BufferView(buffer=0, byteOffset=offset, byteLength=len(img_bytes)))

    image_index = len(gltf.images)
    gltf.images.append(Image(bufferView=buffer_view_index, mimeType=mime))

    texture_index = len(gltf.textures)
    gltf.textures.append(Texture(source=image_index))

    if gltf.buffers:
        gltf.buffers[0].byteLength = len(new_blob)

    # Garante que existe pelo menos um material com a textura
    fallback_material_index = None
    for i, material in enumerate(gltf.materials):
        if material.pbrMetallicRoughness is None:
            material.pbrMetallicRoughness = PbrMetallicRoughness()
        if material.pbrMetallicRoughness.baseColorTexture is None:
            material.pbrMetallicRoughness.baseColorTexture = TextureInfo(index=texture_index)
            if fallback_material_index is None:
                fallback_material_index = i

    if not gltf.materials:
        gltf.materials.append(
            Material(pbrMetallicRoughness=PbrMetallicRoughness(baseColorTexture=TextureInfo(index=texture_index)))
        )
        fallback_material_index = 0

    if fallback_material_index is None:
        fallback_material_index = 0

    # Qualquer primitive sem material nenhum recebe o material com a textura nova
    for mesh in gltf.meshes:
        for primitive in mesh.primitives:
            if primitive.material is None:
                primitive.material = fallback_material_index

    gltf.set_binary_blob(new_blob)

    output_path = os.path.join(work_dir, "with_texture.glb")
    gltf.save_binary(output_path)
    return output_path


def convert_to_glb(mesh_path, work_dir):
    """Usa o assimp pra converter qualquer formato suportado em um único .glb
    com as texturas embutidas. Se já for .glb, não faz nada."""
    ext = os.path.splitext(mesh_path)[1].lower()
    if ext == ".glb":
        return mesh_path

    output_path = os.path.join(work_dir, "converted.glb")
    result = subprocess.run(
        ["assimp", "export", mesh_path, output_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(
            "Não consegui converter esse arquivo pra .glb. "
            f"Detalhe técnico: {result.stderr.strip()[-500:]}"
        )
    return output_path


def upload_to_roblox(glb_path, display_name, description):
    """Sobe o .glb pra Roblox via Open Cloud Assets API e espera a moderação."""
    request_payload = {
        "assetType": "Model",
        "displayName": display_name[:50] if display_name else "Modelo sem nome",
        "description": (description or "")[:1000],
        "creationContext": {"creator": {"userId": ROBLOX_USER_ID}},
    }

    with open(glb_path, "rb") as f:
        files = {
            "request": (None, json.dumps(request_payload), "application/json"),
            "fileContent": (os.path.basename(glb_path), f, "model/gltf-binary"),
        }
        resp = requests.post(
            ROBLOX_ASSETS_URL,
            headers={"x-api-key": ROBLOX_API_KEY},
            files=files,
            timeout=60,
        )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Roblox recusou o envio ({resp.status_code}): {resp.text[:500]}")

    operation_path = resp.json().get("path", "")
    operation_id = operation_path.split("/")[-1]
    if not operation_id:
        raise RuntimeError(f"Resposta inesperada da Roblox: {resp.text[:500]}")

    # Faz polling até a moderação terminar (timeout total ~2 minutos)
    for _ in range(40):
        time.sleep(3)
        op_resp = requests.get(
            ROBLOX_OPERATION_URL.format(operation_id),
            headers={"x-api-key": ROBLOX_API_KEY},
            timeout=30,
        )
        op_data = op_resp.json()
        if op_data.get("done"):
            response = op_data.get("response")
            if response and response.get("assetId"):
                return response["assetId"]
            raise RuntimeError(f"A Roblox rejeitou o asset (provavelmente na moderação): {op_data}")

    raise RuntimeError("Deu timeout esperando a moderação da Roblox responder. Tente checar seu Inventário em alguns minutos.")


def process_job(job_id, saved_path, display_name, description, texture_path=None):
    """Roda em background: extrai/converte/envia, e guarda o resultado em JOBS."""
    try:
        with tempfile.TemporaryDirectory() as work_dir:
            mesh_path = saved_path

            if saved_path.lower().endswith(".zip"):
                extract_dir = os.path.join(work_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(saved_path, "r") as z:
                    z.extractall(extract_dir)
                mesh_path = find_main_mesh_file(extract_dir)
                if not mesh_path:
                    raise RuntimeError("Não encontrei nenhum arquivo de modelo 3D dentro do .zip.")

            ext = os.path.splitext(mesh_path)[1].lower()
            if ext == ".blend":
                raise RuntimeError(
                    "Arquivos .blend não são suportados aqui (precisam do Blender pra abrir). "
                    "Procure a versão exportada em .glb, .obj ou .fbx do modelo."
                )
            if ext not in MESH_EXTENSIONS:
                raise RuntimeError(f"Formato '{ext}' não é um modelo 3D suportado.")

            glb_path = convert_to_glb(mesh_path, work_dir)

            warning = None
            if texture_path:
                try:
                    glb_path = attach_fallback_texture(glb_path, texture_path, work_dir)
                except Exception as exc:  # noqa: BLE001
                    # Se a injeção da textura falhar, não aborta o upload —
                    # sobe sem a textura e avisa o motivo.
                    warning = f"Não consegui aplicar a textura manual ({exc}); o modelo subiu sem ela."

            asset_id = upload_to_roblox(glb_path, display_name, description)

            result = {
                "status": "done",
                "assetId": asset_id,
                "inventoryUrl": "https://www.roblox.com/users/inventory/#!/3d-model",
            }
            if warning:
                result["warning"] = warning
            JOBS[job_id] = result
    except Exception as exc:  # noqa: BLE001
        JOBS[job_id] = {"status": "error", "error": str(exc)}
    finally:
        try:
            os.remove(saved_path)
        except OSError:
            pass
        if texture_path:
            try:
                os.remove(texture_path)
            except OSError:
                pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if not ROBLOX_API_KEY or not ROBLOX_USER_ID or not APP_SECRET:
        return jsonify({"error": "O servidor não está configurado (faltam variáveis de ambiente). Veja o README."}), 500

    secret = request.form.get("secret", "")
    if secret != APP_SECRET:
        return jsonify({"error": "Senha incorreta."}), 401

    if "file" not in request.files or request.files["file"].filename == "":
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    uploaded = request.files["file"]
    display_name = request.form.get("displayName", "").strip() or os.path.splitext(uploaded.filename)[0]
    description = request.form.get("description", "").strip()

    upload_dir = tempfile.mkdtemp(prefix="upload_")
    saved_path = os.path.join(upload_dir, uploaded.filename)
    uploaded.save(saved_path)

    if os.path.getsize(saved_path) > MAX_UPLOAD_BYTES:
        shutil.rmtree(upload_dir, ignore_errors=True)
        return jsonify({"error": "Arquivo maior que o limite de 20MB da Roblox."}), 400

    texture_path = None
    texture_file = request.files.get("texture")
    if texture_file and texture_file.filename:
        texture_ext = os.path.splitext(texture_file.filename)[1].lower()
        if texture_ext not in {".png", ".jpg", ".jpeg"}:
            shutil.rmtree(upload_dir, ignore_errors=True)
            return jsonify({"error": "A textura manual precisa ser .png, .jpg ou .jpeg."}), 400
        texture_path = os.path.join(upload_dir, "manual_texture" + texture_ext)
        texture_file.save(texture_path)

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "processing"}

    thread = threading.Thread(
        target=process_job,
        args=(job_id, saved_path, display_name, description, texture_path),
        daemon=True,
    )
    thread.start()

    return jsonify({"jobId": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado."}), 404
    return jsonify(job)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
