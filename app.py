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
DEFAULT_TARGET_SIZE = 6.0  # studs, lado maior do model — tamanho-base "tipo humano"

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


def convert_to_glb(mesh_path, work_dir):
    """Usa o assimp pra (re)exportar o modelo como um único .glb autocontido.

    Mesmo arquivos que já SÃO .glb passam por aqui de propósito: isso ajuda a
    normalizar formatos de textura/compressão que a fonte original usa sem
    avisar (ex: compressão Draco na malha, texturas Basis Universal/KTX2) e
    que a Roblox não entende. Se a reconversão falhar, cai de volta pro
    arquivo original sem alterações, em vez de travar o upload.
    """
    output_path = os.path.join(work_dir, "converted.glb")
    result = subprocess.run(
        ["assimp", "export", mesh_path, output_path],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode == 0 and os.path.exists(output_path):
        return output_path, None

    ext = os.path.splitext(mesh_path)[1].lower()
    if ext == ".glb":
        return mesh_path, (
            "Não consegui reprocessar esse .glb pra normalizar formato/compressão "
            "(pode usar algo como compressão Draco). Subiu o arquivo original, sem ajustes."
        )

    raise RuntimeError(
        "Não consegui converter esse arquivo pra .glb. "
        f"Detalhe técnico: {result.stderr.strip()[-500:]}"
    )


def diagnose_missing_texture(glb_path, manual_texture_provided):
    """Inspeciona o .glb final e, se não tiver nenhuma imagem embutida, tenta
    apontar o motivo mais provável — só pra informar, não tenta mais nada."""
    if manual_texture_provided:
        return None
    try:
        gltf = GLTF2().load_binary(glb_path)
    except Exception:
        return None

    if gltf.images:
        return None

    used = set(gltf.extensionsUsed or [])
    if "KHR_draco_mesh_compression" in used:
        return (
            "Esse modelo usa compressão Draco e pode ter perdido textura/detalhe na conversão — "
            "se existir, procure a versão 'sem compressão' (uncompressed) na fonte original."
        )
    if "KHR_texture_basisu" in used:
        return (
            "Esse modelo usa textura comprimida em Basis Universal/KTX2, que não converte bem por aqui — "
            "procure a versão com textura em PNG/JPG normal ou anexe a imagem manualmente."
        )
    if "KHR_materials_pbrSpecularGlossiness" in used or "KHR_materials_unlit" in used:
        return "Esse modelo define a cor de um jeito não totalmente compatível com esse fluxo — anexar a textura manualmente garante o resultado."
    return "Não encontrei nenhuma imagem dentro desse modelo — pode ser que a textura nunca tenha vindo junto na fonte original."


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

    for mesh in gltf.meshes:
        for primitive in mesh.primitives:
            if primitive.material is None:
                primitive.material = fallback_material_index

    gltf.set_binary_blob(new_blob)

    output_path = os.path.join(work_dir, "with_texture.glb")
    gltf.save_binary(output_path)
    return output_path


def _compute_bounding_box(gltf):
    """Bounding box aproximado a partir dos accessors POSITION de todas as
    meshes. Não considera transforms de nós aninhados — funciona bem pra
    props/personagens simples (a maioria dos modelos baixados prontos),
    mas pode não ser perfeito em rigs muito complexos com vários ossos."""
    mins, maxs = [], []
    for mesh in gltf.meshes:
        for prim in mesh.primitives:
            pos_idx = getattr(prim.attributes, "POSITION", None)
            if pos_idx is None:
                continue
            accessor = gltf.accessors[pos_idx]
            if accessor.min and accessor.max:
                mins.append(accessor.min)
                maxs.append(accessor.max)
    if not mins:
        return None
    overall_min = [min(v[i] for v in mins) for i in range(3)]
    overall_max = [max(v[i] for v in maxs) for i in range(3)]
    return overall_min, overall_max


def normalize_scale(glb_path, target_size, work_dir):
    """Escala o modelo todo (via o transform dos nós-raiz da cena, sem tocar
    na geometria) pra que o lado maior do bounding box vire `target_size`
    studs. Isso resolve a inconsistência de tamanho entre fontes diferentes
    (uma exporta em metros, outra em centímetros, outra em unidades
    arbitrárias) sem precisar editar vértice por vértice."""
    gltf = GLTF2().load_binary(glb_path)

    bbox = _compute_bounding_box(gltf)
    if not bbox or not gltf.scenes:
        return None, None

    bmin, bmax = bbox
    largest = max(bmax[i] - bmin[i] for i in range(3))
    if largest <= 0:
        return None, None

    factor = target_size / largest

    scene_idx = gltf.scene if gltf.scene is not None else 0
    root_indices = gltf.scenes[scene_idx].nodes or []
    if not root_indices:
        return None, None

    for idx in root_indices:
        node = gltf.nodes[idx]
        if node.matrix:
            # Pré-multiplica por uma escala uniforme: todo o bloco 3x4
            # (rotação/escala + translação) escala junto, w (índice 15) fica em 1.
            node.matrix = [v * factor if i != 15 else v for i, v in enumerate(node.matrix)]
        else:
            base_scale = node.scale if node.scale else [1.0, 1.0, 1.0]
            node.scale = [base_scale[i] * factor for i in range(3)]

    output_path = os.path.join(work_dir, "scaled.glb")
    gltf.save_binary(output_path)
    return output_path, factor


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


def process_job(job_id, saved_path, display_name, description, texture_path, target_size):
    """Roda em background: extrai/converte/normaliza/envia, guarda o resultado em JOBS."""
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

            warnings = []

            glb_path, convert_warning = convert_to_glb(mesh_path, work_dir)
            if convert_warning:
                warnings.append(convert_warning)

            if texture_path:
                try:
                    glb_path = attach_fallback_texture(glb_path, texture_path, work_dir)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui aplicar a textura manual ({exc}); o modelo subiu sem ela.")

            diag = diagnose_missing_texture(glb_path, manual_texture_provided=bool(texture_path))
            if diag:
                warnings.append(diag)

            if target_size:
                try:
                    scaled_path, _factor = normalize_scale(glb_path, target_size, work_dir)
                    if scaled_path:
                        glb_path = scaled_path
                    else:
                        warnings.append("Não consegui medir o tamanho desse modelo pra padronizar; subiu no tamanho original.")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui padronizar o tamanho automaticamente ({exc}); subiu no tamanho original.")

            asset_id = upload_to_roblox(glb_path, display_name, description)

            result = {
                "status": "done",
                "assetId": asset_id,
                "inventoryUrl": "https://www.roblox.com/users/inventory/#!/3d-model",
            }
            if warnings:
                result["warnings"] = warnings
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

    try:
        target_size = float(request.form.get("targetSize", DEFAULT_TARGET_SIZE))
    except (TypeError, ValueError):
        target_size = DEFAULT_TARGET_SIZE
    target_size = max(0.1, min(target_size, 500))

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
        args=(job_id, saved_path, display_name, description, texture_path, target_size),
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
