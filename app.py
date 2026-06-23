import copy
import io
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile

import requests
from flask import Flask, jsonify, render_template, request
from PIL import Image as PILImage
from pygltflib import (
    GLTF2,
    Buffer,
    BufferView,
    Image,
    Material,
    Mesh,
    PbrMetallicRoughness,
    Scene,
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
ATLAS_TILE_SIZE = 512  # tamanho de cada textura individual dentro do atlas combinado

# Guarda o status dos jobs em memória (ok pra uso pessoal / single instance)
JOBS = {}


def bake_texture_atlas(glb_path, work_dir, exclude_names=None, exclude_indices=None):
    """[EXPERIMENTAL] Combina as texturas de COR de partes diferentes do
    mesmo modelo (ex: lâmina + bainha) num único atlas, redesenhando o UV de
    cada peça pra apontar pra sua região dentro do atlas -- assim o
    resultado final usa só 1 material/textura no total, em vez de uma por
    peça.

    `exclude_names` (nomes de material) e `exclude_indices` (posição,
    usada quando o material não tem nome) deixam peças específicas DE FORA
    do atlas de propósito -- útil pra peças que devem continuar
    independentes (ex: uma serra que precisa girar separada da arma).

    Só entram no atlas as texturas usadas como `baseColorTexture` (a cor
    visível) de cada material -- mapas de normal/relevo, metálico-
    -rugosidade, oclusão e emissivo ficam de fora de propósito, já que a
    Roblox MeshPart só usa uma textura de cor mesmo, e incluir essas outras
    no atlas só desperdiçava espaço e podia aparecer como manchas
    arroxeadas/azuladas (a cor típica de um mapa de normal) caso algo saísse
    do lugar.

    Isso existe porque a Roblox só aceita 1 textura base por MeshPart -- não
    tem suporte nativo a 'várias cores num mesh só' fora desse truque.
    Funciona melhor em modelos simples; qualidade pode cair (textura
    'espremida' pra caber no atlas) e não tenho como testar contra a Roblox
    real antes de entregar, por isso é uma opção separada e desligada por
    padrão."""
    exclude_names = exclude_names or set()
    exclude_indices = exclude_indices or set()

    gltf = GLTF2().load_binary(glb_path)

    if not gltf.images:
        return glb_path, None

    # Mapeia cada material só pela sua textura de COR (baseColorTexture) --
    # ignora normalTexture/metallicRoughnessTexture/occlusionTexture/emissiveTexture.
    # Pula de propósito qualquer material marcado pra ficar separado.
    material_to_image = {}
    for m_idx, mat in enumerate(gltf.materials):
        if mat.name and mat.name in exclude_names:
            continue
        if not mat.name and m_idx in exclude_indices:
            continue
        if mat.pbrMetallicRoughness and mat.pbrMetallicRoughness.baseColorTexture is not None:
            tex_idx = mat.pbrMetallicRoughness.baseColorTexture.index
            if tex_idx is not None and gltf.textures[tex_idx].source is not None:
                material_to_image[m_idx] = gltf.textures[tex_idx].source

    needed_image_indices = sorted(set(material_to_image.values()))
    if len(needed_image_indices) < 2:
        return glb_path, None  # já é 1 textura de cor só (ou nenhuma) -- nada a combinar

    blob = gltf.binary_blob() or b""

    pil_images = {}
    for i in needed_image_indices:
        img = gltf.images[i]
        if img.bufferView is None:
            continue
        bv = gltf.bufferViews[img.bufferView]
        start = bv.byteOffset or 0
        data = blob[start:start + bv.byteLength]
        try:
            pil_images[i] = PILImage.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            continue

    if len(pil_images) < 2:
        return glb_path, "Não encontrei 2+ texturas de cor válidas pra combinar num atlas; modelo subiu sem mudar."

    image_indices = sorted(pil_images.keys())
    n = len(image_indices)
    atlas = PILImage.new("RGBA", (ATLAS_TILE_SIZE * n, ATLAS_TILE_SIZE), (0, 0, 0, 0))
    region_for_image = {}
    for slot, img_idx in enumerate(image_indices):
        tile = pil_images[img_idx].resize((ATLAS_TILE_SIZE, ATLAS_TILE_SIZE))
        atlas.paste(tile, (slot * ATLAS_TILE_SIZE, 0))
        region_for_image[img_idx] = (slot / n, 0.0, 1.0 / n, 1.0)

    atlas_io = io.BytesIO()
    atlas.save(atlas_io, format="PNG")
    atlas_bytes = atlas_io.getvalue()

    # Mantém só os materiais cuja textura de cor realmente entrou no atlas
    material_to_image = {m: img for m, img in material_to_image.items() if img in region_for_image}

    if not material_to_image:
        return glb_path, "Não consegui ligar nenhum material a uma textura de cor pra montar o atlas; modelo subiu sem mudar."

    pad = (-len(blob)) % 4
    new_blob = blob + b"\x00" * pad
    atlas_offset = len(new_blob)
    new_blob += atlas_bytes

    atlas_bv_index = len(gltf.bufferViews)
    gltf.bufferViews.append(BufferView(buffer=0, byteOffset=atlas_offset, byteLength=len(atlas_bytes)))

    atlas_image_index = len(gltf.images)
    gltf.images.append(Image(bufferView=atlas_bv_index, mimeType="image/png"))

    atlas_texture_index = len(gltf.textures)
    gltf.textures.append(Texture(source=atlas_image_index))

    atlas_material_index = len(gltf.materials)
    gltf.materials.append(
        Material(pbrMetallicRoughness=PbrMetallicRoughness(baseColorTexture=TextureInfo(index=atlas_texture_index)))
    )

    remapped_any = False
    for mesh in gltf.meshes:
        for prim in mesh.primitives:
            if prim.material is None or prim.material not in material_to_image:
                continue
            img_idx = material_to_image[prim.material]
            offset_u, offset_v, scale_u, scale_v = region_for_image[img_idx]

            uv_accessor_idx = getattr(prim.attributes, "TEXCOORD_0", None)
            if uv_accessor_idx is None:
                continue

            old_accessor = gltf.accessors[uv_accessor_idx]
            old_bv = gltf.bufferViews[old_accessor.bufferView]
            start = (old_bv.byteOffset or 0) + (old_accessor.byteOffset or 0)
            count = old_accessor.count
            stride = old_bv.byteStride or 8  # 2 floats (8 bytes) por UV se não-intercalado

            new_uv_bytes = bytearray()
            for i in range(count):
                base = start + i * stride
                u, v = struct.unpack_from("<ff", blob, base)
                new_u = u * scale_u + offset_u
                new_v = v * scale_v + offset_v
                new_uv_bytes += struct.pack("<ff", new_u, new_v)

            pad2 = (-len(new_blob)) % 4
            new_blob += b"\x00" * pad2
            new_bv_offset = len(new_blob)
            new_blob += bytes(new_uv_bytes)

            new_bv_index = len(gltf.bufferViews)
            gltf.bufferViews.append(BufferView(buffer=0, byteOffset=new_bv_offset, byteLength=len(new_uv_bytes)))

            new_accessor_index = len(gltf.accessors)
            new_accessor = copy.deepcopy(old_accessor)
            new_accessor.bufferView = new_bv_index
            new_accessor.byteOffset = 0
            gltf.accessors.append(new_accessor)

            prim.attributes.TEXCOORD_0 = new_accessor_index
            prim.material = atlas_material_index
            remapped_any = True

    if not remapped_any:
        return glb_path, "Não consegui remapear nenhuma peça pro atlas combinado; modelo subiu sem mudar."

    if gltf.buffers:
        gltf.buffers[0].byteLength = len(new_blob)
    gltf.set_binary_blob(new_blob)

    output_path = os.path.join(work_dir, "atlas.glb")
    gltf.save_binary(output_path)
    return output_path, None


def merge_glb_files(glb_paths, work_dir):
    """Combina vários .glb num único arquivo: concatena buffers, materiais,
    texturas, meshes e nós, ajustando todos os índices internos. Cada peça
    mantém seu próprio material/textura (nada é sobrescrito). A posição
    relativa entre as peças só fica correta se a fonte original exportou
    cada arquivo usando o mesmo referencial de coordenadas (comum quando se
    usa 'exportar selecionados' a partir do mesmo arquivo de origem) — não
    tem como adivinhar posição relativa entre arquivos totalmente
    independentes.
    """
    if len(glb_paths) == 1:
        return glb_paths[0]

    merged = GLTF2()
    merged.scenes = [Scene(nodes=[])]
    merged.scene = 0
    merged.buffers = []
    merged.bufferViews = []
    merged.accessors = []
    merged.meshes = []
    merged.materials = []
    merged.textures = []
    merged.images = []
    merged.samplers = []
    merged.nodes = []

    combined_blob = b""
    texture_fields = (
        ("normalTexture", None),
        ("occlusionTexture", None),
        ("emissiveTexture", None),
    )

    for glb_path in glb_paths:
        gltf = GLTF2().load_binary(glb_path)
        blob = gltf.binary_blob() or b""

        pad = (-len(combined_blob)) % 4
        combined_blob += b"\x00" * pad
        buffer_offset = len(combined_blob)
        combined_blob += blob

        bufferview_offset = len(merged.bufferViews)
        for bv in gltf.bufferViews:
            new_bv = copy.deepcopy(bv)
            new_bv.buffer = 0
            new_bv.byteOffset = (bv.byteOffset or 0) + buffer_offset
            merged.bufferViews.append(new_bv)

        accessor_offset = len(merged.accessors)
        for acc in gltf.accessors:
            new_acc = copy.deepcopy(acc)
            if new_acc.bufferView is not None:
                new_acc.bufferView += bufferview_offset
            merged.accessors.append(new_acc)

        image_offset = len(merged.images)
        for img in gltf.images:
            new_img = copy.deepcopy(img)
            if new_img.bufferView is not None:
                new_img.bufferView += bufferview_offset
            merged.images.append(new_img)

        sampler_offset = len(merged.samplers)
        for samp in gltf.samplers:
            merged.samplers.append(copy.deepcopy(samp))

        texture_offset = len(merged.textures)
        for tex in gltf.textures:
            new_tex = copy.deepcopy(tex)
            if new_tex.source is not None:
                new_tex.source += image_offset
            if new_tex.sampler is not None:
                new_tex.sampler += sampler_offset
            merged.textures.append(new_tex)

        material_offset = len(merged.materials)
        for mat in gltf.materials:
            new_mat = copy.deepcopy(mat)
            if new_mat.pbrMetallicRoughness:
                if new_mat.pbrMetallicRoughness.baseColorTexture:
                    new_mat.pbrMetallicRoughness.baseColorTexture.index += texture_offset
                if new_mat.pbrMetallicRoughness.metallicRoughnessTexture:
                    new_mat.pbrMetallicRoughness.metallicRoughnessTexture.index += texture_offset
            for field_name, _ in texture_fields:
                tex_info = getattr(new_mat, field_name, None)
                if tex_info is not None:
                    tex_info.index += texture_offset
            merged.materials.append(new_mat)

        mesh_offset = len(merged.meshes)
        for mesh in gltf.meshes:
            new_mesh = copy.deepcopy(mesh)
            for prim in new_mesh.primitives:
                if prim.material is not None:
                    prim.material += material_offset
                if prim.indices is not None:
                    prim.indices += accessor_offset
                if prim.attributes:
                    for attr_name in (
                        "POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "TEXCOORD_1",
                        "COLOR_0", "JOINTS_0", "WEIGHTS_0",
                    ):
                        val = getattr(prim.attributes, attr_name, None)
                        if val is not None:
                            setattr(prim.attributes, attr_name, val + accessor_offset)
            merged.meshes.append(new_mesh)

        node_offset = len(merged.nodes)
        for node in gltf.nodes:
            new_node = copy.deepcopy(node)
            if new_node.mesh is not None:
                new_node.mesh += mesh_offset
            if new_node.children:
                new_node.children = [c + node_offset for c in new_node.children]
            merged.nodes.append(new_node)

        scene_idx = gltf.scene if gltf.scene is not None else 0
        root_nodes = gltf.scenes[scene_idx].nodes if gltf.scenes else []
        for r in root_nodes:
            merged.scenes[0].nodes.append(r + node_offset)

    merged.buffers = [Buffer(byteLength=len(combined_blob))]
    merged.set_binary_blob(combined_blob)

    output_path = os.path.join(work_dir, "combined.glb")
    merged.save_binary(output_path)
    return output_path


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
    """Converte qualquer formato suportado num único .glb autocontido.

    Se o arquivo já for .glb e já tiver pelo menos uma imagem embutida, ele é
    usado exatamente como está. Reprocessar pelo assimp um .glb que já
    funciona pode, em modelos com várias partes/ossos, embaralhar a posição
    relativa das peças — então só vale arriscar isso quando o arquivo já
    está com problema (sem nenhuma textura) de qualquer forma.
    """
    ext = os.path.splitext(mesh_path)[1].lower()

    if ext == ".glb":
        try:
            existing = GLTF2().load_binary(mesh_path)
            if existing.images:
                return mesh_path, None
        except Exception:
            pass  # se não conseguir nem ler, tenta reprocessar abaixo

    output_path = os.path.join(work_dir, "converted.glb")
    result = subprocess.run(
        ["assimp", "export", mesh_path, output_path],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode == 0 and os.path.exists(output_path):
        warning = None
        if ext == ".glb":
            warning = (
                "Esse .glb não tinha textura embutida, então reprocessei pra tentar "
                "recuperar — em modelos com várias partes isso pode (raramente) deixar "
                "alguma peça fora do lugar. Se acontecer, me avisa."
            )
        return output_path, warning

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


def _quat_multiply(q1, q2):
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return (
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    )


def _quat_from_axis_angle(axis, angle_rad):
    half = angle_rad / 2.0
    s = math.sin(half)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(half))


def _euler_degrees_to_quaternion(rx, ry, rz):
    """Combina rotações nos 3 eixos (em graus), aplicando X primeiro, depois Y, depois Z."""
    qx = _quat_from_axis_angle((1, 0, 0), math.radians(rx))
    qy = _quat_from_axis_angle((0, 1, 0), math.radians(ry))
    qz = _quat_from_axis_angle((0, 0, 1), math.radians(rz))
    return _quat_multiply(qz, _quat_multiply(qy, qx))


def _quat_to_matrix_col_major(q):
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    m00, m01, m02 = 1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)
    m10, m11, m12 = 2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)
    m20, m21, m22 = 2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)
    return [
        m00, m10, m20, 0.0,
        m01, m11, m21, 0.0,
        m02, m12, m22, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _matrix_multiply_col_major(a, b):
    def get(m, row, col):
        return m[col * 4 + row]

    result = [0.0] * 16
    for col in range(4):
        for row in range(4):
            s = 0.0
            for k in range(4):
                s += get(a, row, k) * get(b, k, col)
            result[col * 4 + row] = s
    return result


def rotate_model(glb_path, rx, ry, rz, work_dir):
    """Gira o modelo todo em torno da origem (via o transform dos nós-raiz,
    sem tocar na geometria), pelos ângulos dados em graus. Útil pra
    corrigir modelos que chegam tortos/inclinados de propósito (pose de
    descanso, convenção de eixo diferente etc.)."""
    if not rx and not ry and not rz:
        return glb_path

    gltf = GLTF2().load_binary(glb_path)
    if not gltf.scenes:
        return glb_path

    scene_idx = gltf.scene if gltf.scene is not None else 0
    root_indices = gltf.scenes[scene_idx].nodes or []
    if not root_indices:
        return glb_path

    correction = _euler_degrees_to_quaternion(rx, ry, rz)

    for idx in root_indices:
        node = gltf.nodes[idx]
        if node.matrix:
            rot_matrix = _quat_to_matrix_col_major(correction)
            node.matrix = _matrix_multiply_col_major(rot_matrix, node.matrix)
        else:
            existing = tuple(node.rotation) if node.rotation else (0.0, 0.0, 0.0, 1.0)
            node.rotation = list(_quat_multiply(correction, existing))

    output_path = os.path.join(work_dir, "rotated.glb")
    gltf.save_binary(output_path)
    return output_path


def auto_orient(glb_path, work_dir):
    """[EXPERIMENTAL] Tenta deixar o modelo 'de pé' automaticamente, girando
    90° pra alinhar o lado MAIS LONGO do modelo com o eixo vertical (Y).

    É uma heurística simples baseada só no formato da caixa que envolve o
    modelo (bounding box) -- funciona bem pra objetos alongados (espada,
    bastão, pessoa em pé), mas pode ERRAR em objetos que são naturalmente
    largos/baixos de propósito (escudo, mesa, veículo), porque o código não
    sabe o que o objeto realmente é, só mede o formato dele.

    Só age se os campos de rotação manual estiverem zerados -- se você já
    ajustou X/Y/Z na mão, isso é ignorado, pra não conflitar com o que você
    decidiu de propósito."""
    gltf = GLTF2().load_binary(glb_path)
    bbox = _compute_bounding_box(gltf)
    if not bbox:
        return glb_path, None

    bmin, bmax = bbox
    dx = bmax[0] - bmin[0]
    dy = bmax[1] - bmin[1]
    dz = bmax[2] - bmin[2]

    if dy >= dx and dy >= dz:
        return glb_path, None  # o lado mais longo já é o eixo Y -- nada a fazer

    if dx >= dz:
        rx, ry, rz = 0, 0, 90  # traz o comprimento de X pro eixo Y
    else:
        rx, ry, rz = 90, 0, 0  # traz o comprimento de Z pro eixo Y

    return rotate_model(glb_path, rx, ry, rz, work_dir), None


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


def strip_non_color_textures(glb_path, work_dir):
    """Remove de todo material qualquer textura que NÃO seja a cor base
    (`baseColorTexture`) -- ou seja, tira mapa de normal/relevo, metálico-
    -rugosidade, oclusão e emissivo. Roda sempre, em todo upload, porque a
    Roblox MeshPart só usa a cor base mesmo, e mapas de normal/relevo
    costumam ter aquela aparência arroxeada/azulada que não serve pra nada
    aqui -- só causa confusão visual."""
    gltf = GLTF2().load_binary(glb_path)
    changed = False
    for mat in gltf.materials:
        if mat.normalTexture is not None:
            mat.normalTexture = None
            changed = True
        if mat.occlusionTexture is not None:
            mat.occlusionTexture = None
            changed = True
        if mat.emissiveTexture is not None:
            mat.emissiveTexture = None
            changed = True
        if mat.pbrMetallicRoughness and mat.pbrMetallicRoughness.metallicRoughnessTexture is not None:
            mat.pbrMetallicRoughness.metallicRoughnessTexture = None
            changed = True

    if not changed:
        return glb_path

    output_path = os.path.join(work_dir, "color_only.glb")
    gltf.save_binary(output_path)
    return output_path


def consolidate_meshes_by_material(glb_path, work_dir):
    """Depois do atlas, agrupa num ÚNICO objeto de mesh todos os meshes que
    usam o mesmo material. Só trocar a textura (atlas) não reduz a
    quantidade de meshes por si só -- é esse passo que de fato faz a Roblox
    importar como 1 mesh em vez de várias.

    Só participa da fusão um mesh 'puro' (todos os primitivos dele usam o
    MESMO material) -- um mesh com materiais misturados fica como está, sem
    arriscar perder algum pedaço dele por engano."""
    gltf = GLTF2().load_binary(glb_path)

    mesh_material = {}
    for mesh_idx, mesh in enumerate(gltf.meshes):
        materials_used = {p.material for p in mesh.primitives}
        if len(materials_used) == 1:
            mat = next(iter(materials_used))
            if mat is not None:
                mesh_material[mesh_idx] = mat

    material_to_meshes = {}
    for mesh_idx, mat in mesh_material.items():
        material_to_meshes.setdefault(mat, []).append(mesh_idx)

    groups_to_merge = {mat: meshes for mat, meshes in material_to_meshes.items() if len(meshes) > 1}
    if not groups_to_merge:
        return glb_path, None

    node_for_mesh = {}
    for node_idx, node in enumerate(gltf.nodes):
        if node.mesh is not None:
            node_for_mesh.setdefault(node.mesh, []).append(node_idx)

    for mat_idx, mesh_indices in groups_to_merge.items():
        combined_primitives = []
        for m_idx in mesh_indices:
            combined_primitives.extend(gltf.meshes[m_idx].primitives)

        new_mesh_index = len(gltf.meshes)
        gltf.meshes.append(Mesh(primitives=combined_primitives))

        first_node = None
        for m_idx in mesh_indices:
            for node_idx in node_for_mesh.get(m_idx, []):
                if first_node is None:
                    first_node = node_idx
                    gltf.nodes[node_idx].mesh = new_mesh_index
                else:
                    gltf.nodes[node_idx].mesh = None  # nó "fantasma": não renderiza nada, mas não quebra a hierarquia

    output_path = os.path.join(work_dir, "consolidated.glb")
    gltf.save_binary(output_path)
    return output_path, None


def merge_parts(glb_path, work_dir):
    """'Assa' a posição de cada peça/nó diretamente nos vértices e reduz a
    cena a um conjunto mínimo de meshes (via o pós-processamento
    PreTransformVertices do assimp). Resolve o caso de modelos com várias
    partes separadas (ex: lâmina + bainha) que vinham desencontradas quando
    cada peça era importada individualmente por ID na Roblox — depois disso,
    a posição relativa correta já está embutida na geometria de cada peça,
    então não depende mais de hierarquia/transform externos pra se montar.
    """
    output_path = os.path.join(work_dir, "merged.glb")
    result = subprocess.run(
        ["assimp", "export", glb_path, output_path, "-ptv"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0 and os.path.exists(output_path):
        return output_path, None
    return glb_path, (
        "Não consegui combinar a posição das peças automaticamente (continua "
        "como antes — pode precisar reposicionar manualmente)."
    )


def poll_operation(operation_id):
    """Espera a Roblox terminar a moderação/processamento de uma criação ou
    atualização de asset, e devolve o assetId resultante."""
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


def upload_to_roblox(glb_path, display_name, description):
    """Cria um novo asset (Model) na Roblox via Open Cloud e espera a moderação."""
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

    return poll_operation(operation_id)


def convert_glb_to_fbx(glb_path, work_dir):
    """A Roblox só aceita .fbx pra ATUALIZAR (PATCH) o conteúdo de um asset
    já existente — .glb não é aceito nesse endpoint específico (é uma
    limitação da própria API, ainda em beta). Por isso, só pra esse caso,
    convertemos o resultado final de volta pra .fbx."""
    output_path = os.path.join(work_dir, "for_update.fbx")
    result = subprocess.run(
        ["assimp", "export", glb_path, output_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"Falha ao gerar .fbx: {result.stderr.strip()[-500:]}")
    return output_path


def update_roblox_asset(asset_id, fbx_path, display_name, description):
    """Atualiza o CONTEÚDO de um asset Model já existente (cria uma nova
    versão), via o endpoint PATCH (beta) da Open Cloud Assets API."""
    request_payload = {
        "assetType": "Model",
        "assetId": str(asset_id),
        "displayName": display_name[:50] if display_name else "Modelo sem nome",
        "description": (description or "")[:1000],
        "creationContext": {"creator": {"userId": ROBLOX_USER_ID}},
    }

    url = f"{ROBLOX_ASSETS_URL}/{asset_id}"
    with open(fbx_path, "rb") as f:
        files = {
            "request": (None, json.dumps(request_payload), "application/json"),
            "fileContent": (os.path.basename(fbx_path), f, "model/fbx"),
        }
        resp = requests.patch(
            url,
            headers={"x-api-key": ROBLOX_API_KEY},
            files=files,
            timeout=60,
        )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Roblox recusou a atualização ({resp.status_code}): {resp.text[:500]}")

    operation_path = resp.json().get("path", "")
    operation_id = operation_path.split("/")[-1]
    if not operation_id:
        raise RuntimeError(f"Resposta inesperada da Roblox: {resp.text[:500]}")

    return poll_operation(operation_id)


def process_single_source(saved_path, work_dir, idx):
    """Leva um arquivo de origem (zip ou modelo direto) até um .glb individual."""
    mesh_path = saved_path

    if saved_path.lower().endswith(".zip"):
        extract_dir = os.path.join(work_dir, f"extracted_{idx}")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(saved_path, "r") as z:
            z.extractall(extract_dir)
        mesh_path = find_main_mesh_file(extract_dir)
        if not mesh_path:
            raise RuntimeError(f"Não encontrei modelo 3D dentro de '{os.path.basename(saved_path)}'.")

    ext = os.path.splitext(mesh_path)[1].lower()
    if ext == ".blend":
        raise RuntimeError(
            f"'{os.path.basename(saved_path)}' é um .blend, não suportado aqui (precisa do Blender pra abrir)."
        )
    if ext not in MESH_EXTENSIONS:
        raise RuntimeError(f"Formato '{ext}' não é suportado ('{os.path.basename(saved_path)}').")

    return convert_to_glb(mesh_path, work_dir)


def process_job(job_id, saved_paths, display_name, description, texture_path, target_size, merge_enabled, rotation_xyz, update_asset_id, bake_atlas_enabled, auto_orient_enabled, exclude_parts):
    """Roda em background: extrai/converte/combina/normaliza/envia, guarda o resultado em JOBS."""
    try:
        with tempfile.TemporaryDirectory() as work_dir:
            warnings = []

            part_glbs = []
            for idx, saved_path in enumerate(saved_paths):
                glb_path, convert_warning = process_single_source(saved_path, work_dir, idx)
                if convert_warning:
                    warnings.append(convert_warning)
                part_glbs.append(glb_path)

            if len(part_glbs) > 1:
                try:
                    glb_path = merge_glb_files(part_glbs, work_dir)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"Não consegui combinar os {len(part_glbs)} arquivos num modelo só: {exc}")
            else:
                glb_path = part_glbs[0]

            if texture_path:
                try:
                    glb_path = attach_fallback_texture(glb_path, texture_path, work_dir)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui aplicar a textura manual ({exc}); o modelo subiu sem ela.")

            try:
                glb_path = strip_non_color_textures(glb_path, work_dir)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Não consegui limpar texturas extras ({exc}).")

            diag = diagnose_missing_texture(glb_path, manual_texture_provided=bool(texture_path))
            if diag:
                warnings.append(diag)

            if merge_enabled or bake_atlas_enabled:
                glb_path, merge_warning = merge_parts(glb_path, work_dir)
                if merge_warning:
                    warnings.append(merge_warning)

            if bake_atlas_enabled:
                try:
                    exclude_names = {p["name"] for p in exclude_parts if p.get("name")}
                    exclude_indices = {p["index"] for p in exclude_parts if not p.get("name") and p.get("index") is not None}
                    atlas_path, atlas_warning = bake_texture_atlas(glb_path, work_dir, exclude_names, exclude_indices)
                    if atlas_path:
                        glb_path = atlas_path
                    if atlas_warning:
                        warnings.append(atlas_warning)

                    consolidated_path, consolidate_warning = consolidate_meshes_by_material(glb_path, work_dir)
                    if consolidated_path:
                        glb_path = consolidated_path
                    if consolidate_warning:
                        warnings.append(consolidate_warning)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui combinar as texturas/meshes num atlas ({exc}); subiu com as texturas originais separadas.")

            rx, ry, rz = rotation_xyz
            if rx or ry or rz:
                try:
                    glb_path = rotate_model(glb_path, rx, ry, rz, work_dir)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui aplicar a rotação ({exc}); subiu na orientação original.")
            elif auto_orient_enabled:
                try:
                    oriented_path, orient_warning = auto_orient(glb_path, work_dir)
                    if oriented_path:
                        glb_path = oriented_path
                    if orient_warning:
                        warnings.append(orient_warning)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui auto-orientar o modelo ({exc}); subiu na orientação original.")

            if target_size:
                try:
                    scaled_path, _factor = normalize_scale(glb_path, target_size, work_dir)
                    if scaled_path:
                        glb_path = scaled_path
                    else:
                        warnings.append("Não consegui medir o tamanho desse modelo pra padronizar; subiu no tamanho original.")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"Não consegui padronizar o tamanho automaticamente ({exc}); subiu no tamanho original.")

            if update_asset_id:
                try:
                    fbx_path = convert_glb_to_fbx(glb_path, work_dir)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"Não consegui gerar o .fbx exigido pra atualizar um asset existente: {exc}")
                asset_id = update_roblox_asset(update_asset_id, fbx_path, display_name, description)
                warnings.append("Enviado como ATUALIZAÇÃO (nova versão) do asset existente, não como asset novo.")
            else:
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
        for p in saved_paths:
            try:
                os.remove(p)
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

    uploaded_files = [f for f in request.files.getlist("file") if f and f.filename]
    if not uploaded_files:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    display_name = request.form.get("displayName", "").strip() or os.path.splitext(uploaded_files[0].filename)[0]
    description = request.form.get("description", "").strip()
    update_asset_id = request.form.get("updateAssetId", "").strip() or None

    try:
        target_size = float(request.form.get("targetSize", DEFAULT_TARGET_SIZE))
    except (TypeError, ValueError):
        target_size = DEFAULT_TARGET_SIZE
    target_size = max(0.1, min(target_size, 500))

    merge_enabled = request.form.get("mergeParts", "true").strip().lower() != "false"
    bake_atlas_enabled = request.form.get("bakeAtlas", "false").strip().lower() == "true"
    auto_orient_enabled = request.form.get("autoOrient", "true").strip().lower() != "false"

    try:
        exclude_parts = json.loads(request.form.get("excludeParts", "[]") or "[]")
        if not isinstance(exclude_parts, list):
            exclude_parts = []
    except (TypeError, ValueError):
        exclude_parts = []

    def _parse_angle(field):
        try:
            return float(request.form.get(field, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    rotation_xyz = (_parse_angle("rotateX"), _parse_angle("rotateY"), _parse_angle("rotateZ"))

    upload_dir = tempfile.mkdtemp(prefix="upload_")
    saved_paths = []
    for i, uf in enumerate(uploaded_files):
        path = os.path.join(upload_dir, f"{i}_{uf.filename}")
        uf.save(path)
        if os.path.getsize(path) > MAX_UPLOAD_BYTES:
            shutil.rmtree(upload_dir, ignore_errors=True)
            return jsonify({"error": f"O arquivo '{uf.filename}' é maior que o limite de 20MB da Roblox."}), 400
        saved_paths.append(path)

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
        args=(job_id, saved_paths, display_name, description, texture_path, target_size, merge_enabled, rotation_xyz, update_asset_id, bake_atlas_enabled, auto_orient_enabled, exclude_parts),
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
