# Upload de modelos 3D pro Roblox (via celular)

Site simples que recebe um modelo 3D (e textura) e manda direto pro seu
inventário do Roblox usando a **Open Cloud Assets API** oficial. Feito pra
rodar 100% pelo navegador do celular — você não precisa do Roblox Studio.

## Como funciona

1. Você abre o site no celular e escolhe um arquivo: `.glb`, `.gltf`, `.obj`,
   `.fbx` ou um `.zip` contendo o mesh + a(s) textura(s).
2. O servidor extrai o `.zip` (se for o caso) e usa o `assimp` (conversor 3D
   open-source) pra transformar tudo num único `.glb`, com a textura já
   embutida dentro do arquivo.
3. O `.glb` é enviado pra Roblox via `POST /assets/v1/assets`, autenticado
   com sua API Key.
4. O site fica consultando o status até a Roblox aprovar (moderação) e te
   mostra o Asset ID quando terminar.

## Passo 1 — Criar sua API Key da Roblox

1. Acesse **https://create.roblox.com/dashboard/credentials** (Open Cloud →
   API Keys) logado na sua conta.
2. Clique em **Create API Key**.
3. Em **Access Permissions**, adicione **Assets** e marque **Read** e
   **Write**.
4. No campo de "Resources"/escopo, garanta que está liberado para a sua
   própria conta (User), não um grupo (a não ser que você queira subir como
   asset de um grupo).
5. Copie a chave gerada — ela só aparece uma vez.

## Passo 2 — Pegar seu User ID

Abra seu perfil em `https://www.roblox.com/users/SEU_ID/profile` — o número
na URL é o seu `ROBLOX_USER_ID`.

## Passo 3 — Subir esse projeto pro GitHub

```
git init
git add .
git commit -m "primeira versão"
gh repo create roblox-uploader --private --source=. --push
```

(ou crie o repositório manualmente pelo app/site do GitHub e suba os
arquivos por lá, se preferir não usar linha de comando.)

## Passo 4 — Deploy no Render

1. No painel do Render, **New → Web Service**.
2. Conecte o repositório do GitHub que você acabou de criar.
3. Render vai detectar o `Dockerfile` automaticamente — deixe como está
   (Environment: Docker).
4. Em **Environment Variables**, adicione:
   - `ROBLOX_API_KEY` → a chave do Passo 1
   - `ROBLOX_USER_ID` → o número do Passo 2
   - `APP_SECRET` → uma senha que você mesmo inventa, pra proteger o site
     (sem ela, qualquer pessoa que ache a URL do seu Render poderia usar a
     sua API key pra subir asset na sua conta)
5. Clique em **Deploy**.

## Passo 5 — Usar

Abra a URL que o Render te deu (algo como
`https://roblox-uploader.onrender.com`) no navegador do celular, digite a
senha (`APP_SECRET`) uma vez — ela fica salva no navegador — e mande seus
modelos.

## Modelo sem textura aplicada (FBX puro, textura solta etc.)

Se o `.fbx`/`.obj` não tiver a textura linkada internamente, use o campo
**"Textura manual"** na página: ele injeta a imagem escolhida como a
textura base de qualquer material do modelo que esteja sem nenhuma.

Como isso funciona por baixo dos panos: a maioria dos modelos já vem com
**UV mapping** correto (a informação de "como a textura se encaixa na
superfície"), mesmo quando não tem a imagem da textura linkada. O servidor
abre o `.glb` já convertido e injeta a imagem que você mandou em qualquer
material sem textura, reaproveitando o UV que já existe no mesh.

Isso **não funciona** se:
- O modelo não tem UV mapping nenhum (raro, mas existe) — a textura vai
  ficar distorcida ou esticada de forma errada.
- Você não tem a imagem da textura em lugar nenhum (não tem como inventar
  cor que não existe).

Se o modelo tiver várias texturas (diffuse, normal, roughness...), escolha
a que tiver "diffuse", "albedo", "basecolor" ou "color" no nome — é a única
que a Roblox MeshPart realmente usa (não há suporte completo a PBR nesse
fluxo de upload).

## Modelo `.glb` que mostra textura na fonte, mas sobe sem cor

Mesmo um `.glb` "pronto" pode chegar sem textura na Roblox por causa de
formatos internos que a Roblox (e o conversor) não leem:

- **Compressão Draco** na geometria (`KHR_draco_mesh_compression`) — comum
  em downloads "otimizados" do Sketchfab.
- **Texturas Basis Universal/KTX2** (`KHR_texture_basisu`) — formato de
  textura comprimida que poucas ferramentas leem fora de engines como
  Unity/Unreal.
- Materiais definidos via `KHR_materials_pbrSpecularGlossiness` ou
  `KHR_materials_unlit`, em vez do padrão `pbrMetallicRoughness` que a
  Roblox espera.

Agora o site **sempre reprocessa** o `.glb` recebido (mesmo já sendo
`.glb`) tentando normalizar esses formatos, e se mesmo assim não sobrar
nenhuma imagem embutida, ele te avisa qual desses motivos é o mais provável
direto na tela de resultado — assim você sabe se vale a pena procurar outra
versão do modelo ou só anexar a textura manualmente.

## Tamanho inconsistente entre modelos

A Roblox não converte unidades — ela importa os números de posição do
arquivo **diretamente como studs**. Um modelo exportado em metros (altura
1.7) e outro em centímetros (altura 170) saem em escalas completamente
diferentes na Roblox, mesmo sendo "corretos" nos próprios formatos.

O site agora **padroniza automaticamente** o tamanho de todo modelo
enviado: ele calcula o lado maior do modelo e aplica uma escala uniforme
(via o transform da cena, sem editar a malha) pra esse lado bater com o
valor que você define no campo **"Tamanho"** (padrão: 6 studs, ~escala de
personagem). Ajuste esse número conforme o tipo de objeto — uma espada pode
pedir um valor menor, um prédio um valor bem maior.

Essa medição usa o bounding box da geometria sem considerar transforms
aninhados complexos — funciona bem pra props e personagens simples (a
maioria dos modelos prontos pra baixar), mas pode não ser perfeita em rigs
com múltiplos ossos/armaduras aninhadas.

## Peças vêm desencontradas (ex: lâmina e bainha "uma dentro da outra")

Esse é um problema diferente do de textura: é sobre **posição relativa**.
Quando um modelo tem várias partes (lâmina + bainha, corpo + roupa, etc.) e
você importa cada peça separadamente por ID — porque sem o Studio não dá
pra inserir o "Model" inteiro já montado — cada peça normalmente entra
zerada (`CFrame` na origem), porque a posição relativa entre elas ficava
guardada na hierarquia de nós do arquivo, não na peça em si.

Agora tem um campo **"Combinar peças separadas"** (ativado por padrão) que
resolve isso de um jeito mais robusto: em vez de depender da hierarquia, o
site "assa" a posição de cada peça **direto nos vértices**, todas no mesmo
sistema de coordenadas. Assim, mesmo importando cada peça individualmente
por ID, elas já chegam no lugar certo uma em relação à outra — porque a
posição já está embutida na própria geometria, não depende mais de nenhum
transform externo.

Detalhe técnico (pra quem quiser entender o "por quê"): isso usa o
pós-processamento `PreTransformVertices` do `assimp` via a flag `-ptv`.
**Não tive como testar essa flag específica num ambiente real antes de
entregar** — se por algum motivo a sintaxe estiver errada, o site
simplesmente cai de volta pro comportamento de antes (peças soltas, sem
quebrar nada) e mostra um aviso na tela. Testa com um modelo de várias
partes e me conta o resultado.

Se algum dia você quiser as partes propositalmente separadas (sem fundir),
desmarque essa opção antes de enviar.

## Limites importantes da Roblox (Open Cloud)

- Até **20 MB** por arquivo.
- Tipo `Model` aceita `.fbx`, `.gltf`, `.glb`, `.rbxm`/`.rbxmx` e entra no
  seu inventário como um **Package** (Model com MeshParts dentro).
- A aprovação passa por moderação automática da Roblox — pode demorar
  alguns segundos a minutos. Esse site faz o polling sozinho.
- **Atenção a direitos de uso**: só suba modelos que você tem permissão de
  usar (licença livre/CC0, comprado com direito de redistribuição, ou
  criado por você). Subir modelo de terceiros sem permissão viola os Termos
  de Uso da Roblox e pode resultar em moderação/banimento do asset ou da
  conta.

## Limitações conhecidas

- O `assimp` converte bem a maioria dos formatos comuns (`.obj`, `.fbx`,
  `.gltf`), mas modelos muito complexos ou com materiais incomuns podem não
  converter 100% perfeito — você pode precisar testar mais de uma fonte.
- Arquivos `.blend` não são suportados (exigem o Blender pra abrir);
  procure a versão já exportada em `.glb`/`.obj`/`.fbx`.
- Como é single-user (só sua conta), não tem sistema de login — só a senha
  simples (`APP_SECRET`). Se quiser permitir outras pessoas usarem com as
  próprias contas delas, isso exigiria OAuth 2.0 da Roblox, que é uma
  arquitetura bem diferente — me chama se quiser evoluir pra isso depois.

## Vários arquivos separados (combinar peças de arquivos diferentes)

Agora o campo "Modelo 3D" aceita selecionar **mais de um arquivo de uma vez**
(ex: `lamina.obj` + `bainha.obj`, ou dois `.zip` separados). O site converte
cada um individualmente e depois combina tudo num único `.glb` final.

A posição relativa entre eles só sai correta se a fonte original exportou
cada arquivo usando o **mesmo referencial de coordenadas** (comum quando se
usa "exportar selecionados" a partir do mesmo arquivo de origem no
Blender/Maya/etc.) — não tem como adivinhar a posição relativa entre
arquivos totalmente independentes que nunca compartilharam um sistema de
coordenadas.

## "1 mesh + 1 textura" dentro do pacote da Roblox (experimental)

Confirmado na documentação oficial: um `MeshPart` da Roblox só aceita **uma
textura base por vez** — não existe "vários materiais num mesh só" nativo.
Por isso, quando um modelo tem peças com texturas diferentes (lâmina +
bainha), a Roblox sempre vai criar mais de um `MeshPart`/Mesh dentro do
pacote, não importa o que a gente faça na conversão.

A única forma real de virar "1 mesh + 1 textura" é combinar as texturas
num **atlas** (uma imagem só, com cada textura original numa região) e
redesenhar o UV de cada peça pra apontar pra sua região dentro dessa
imagem combinada — e foi isso que a opção **"Combinar texturas em 1 atlas
(experimental)"** faz.

Isso é manipulação direta de buffer binário (coordenadas UV) e **eu não
tive como testar contra a Roblox real** antes de entregar — por isso vem
desligada por padrão. Ativa só se quiser tentar, e me conta o resultado.
Riscos conhecidos: a textura final fica "espremida" pra caber no atlas
(perde um pouco de qualidade/nitidez), e modelos com textura em padrão
repetido (tile) costumam ficar com costura visível.

## Atualizar um asset já existente

Tem um campo **"Asset ID pra atualizar"**: se você colar o ID de um asset
que você já tem, o site sobe uma **nova versão** do conteúdo dele em vez de
criar um novo. Limitação real da própria Roblox: o endpoint de atualização
**só aceita arquivo `.fbx`**, não `.glb` — por isso, só nesse caso
específico, o site converte o resultado final pra `.fbx` antes de enviar
(um passo extra que não existe no fluxo normal de criação).

## Apagar um asset da conta

**Não é possível via API.** A Open Cloud Assets API da Roblox não tem
nenhum endpoint de exclusão — só Create, Update (conteúdo e metadados) e
Get/rollback de versão. O botão "×" no histórico do site remove o item só
da **lista local** (no seu navegador), não da sua conta Roblox. Pra
apagar/arquivar de verdade, é só pelo Creator Dashboard no site da Roblox
mesmo (manual).

## Outros recursos adicionados

- **Miniatura no histórico**: captura uma imagem pequena do visualizador no
  momento em que o upload termina (se a pré-visualização estava disponível
  pra esse arquivo).
- **Atalhos de rotação**: botões "+90° X/Y/Z" e "Zerar" ao lado dos campos
  numéricos, pra ajustes rápidos sem digitar.
- **Baixar imagem do visualizador**: botão que salva um PNG do que está
  sendo mostrado no visualizador 3D, pra usar como preview/thumbnail em
  outro lugar.


## Modelos vêm em orientações diferentes (deitado, em pé, torto)

Tem um checkbox novo, **"Auto-orientar 'de pé' (experimental)"** (ativado
por padrão), que tenta resolver isso sem você precisar testar ângulo por
ângulo. A lógica é simples: ele mede a caixa que envolve o modelo
(bounding box) e, se o lado mais longo não estiver alinhado com o eixo
vertical, gira 90° pra alinhar.

**Isso é uma heurística, não uma solução garantida.** Funciona bem pra
objetos alongados (espada, bastão, pessoa em pé) porque "o lado mais longo
= a direção que deveria ficar vertical" é uma suposição razoável nesses
casos. Mas vai **errar** em objetos que são largos/baixos de propósito
(escudo, mesa, veículo) — o código não sabe o que o objeto realmente é, só
mede o formato da caixa em volta dele.

Os campos de **rotação manual sempre têm prioridade**: se você preencher
qualquer um dos campos X/Y/Z, a auto-orientação é ignorada nesse upload
(evita ela "competir" com um ajuste que você já fez de propósito). Se
isso errar pra um objeto específico, é só desativar o checkbox e ajustar
manualmente como antes.

## Seletor de partes (manter peças separadas do atlas)

Quando o checkbox **"Combinar texturas em 1 atlas"** está marcado e o
modelo (`.glb`/`.gltf` único, com pré-visualização ativa) tem 2 ou mais
materiais, aparece uma lista de partes embaixo do checkbox. Você pode:

- Tocar diretamente numa peça lá no visualizador (usa a API
  `materialFromPoint` do `model-viewer`, que identifica qual material está
  embaixo do toque), ou
- Marcar direto na lista.

A peça marcada fica destacada em vermelho no visualizador (só um aviso
visual, não afeta o arquivo final) e **não entra no atlas** — continua com
a própria textura original, em mesh separado. É pra casos como uma serra
numa arma, que precisa continuar independente pra poder girar sozinha,
mesmo combinando o resto.

**Limitações honestas:**
- Só funciona quando a pré-visualização está disponível (1 arquivo
  `.glb`/`.gltf` direto — não funciona com `.fbx`/`.obj`/`.zip` ou múltiplos
  arquivos, mesma limitação de sempre da pré-visualização).
- A seleção é enviada por **nome do material** (ou por posição, se o
  material não tiver nome). Se o arquivo original não nomeia os materiais,
  a correspondência por posição é um best-effort — funciona bem pra upload
  de arquivo único, mas não é garantida.
- Se a peça que você quer manter separada não tiver uma textura de cor
  própria (`baseColorTexture`) detectável, ela já fica de fora do atlas de
  qualquer forma, então marcar não muda nada nesse caso.
