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
