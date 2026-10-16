# Contas do YouTube e do Instagram (grátis)

Tudo aqui é gratuito. Faça uma vez por conta. O nome que você usar no comando (`gta6`, `ia`, `politica`) precisa ser igual ao de `contas:` no YAML do canal.

## YouTube

1. Entre em https://console.cloud.google.com e crie um projeto (ex.: "Canal Dark").
2. Em **APIs e serviços > Biblioteca**, ative a **YouTube Data API v3**.
3. Em **Tela de consentimento OAuth** (Google Auth Platform): tipo **Externo**, preencha nome e e-mail, adicione o escopo `.../auth/youtube.upload` e o seu e-mail como usuário de teste.
4. **Publique o app ("Em produção")**. Em modo de teste o token expira a cada 7 dias e você teria que reconectar toda semana. O Google vai mostrar "app não verificado" no login: clique em *Avançado > Acessar* (é o seu próprio app).
5. Em **Credenciais > Criar credenciais > ID do cliente OAuth**, escolha **App para computador** e baixe o JSON. Salve como `dados/client_secret.json`.
6. Rode `canaldark auth youtube gta6`. O navegador abre; entre com a conta Google **do canal** e autorize.

Avisos importantes:
- **Vídeos ficam privados até a auditoria.** Projetos novos que enviam pela API têm os uploads travados como privados até passar pela auditoria gratuita do YouTube ("YouTube API Services – Audit and Quota Extension Form"). Peça logo no começo. Enquanto isso, você pode deixar cada vídeo público no YouTube Studio.
- **Cota diária.** O projeto começa com 10.000 unidades/dia e cada upload consome uma parte grande disso (confira o custo atual em https://developers.google.com/youtube/v3/determine_quota_cost). Se publicar muitos vídeos por dia em vários canais, peça aumento de cota no mesmo formulário. Quando a cota acaba, o vídeo espera o próximo horário; nada se perde.
- Token revogado ou expirado aparece no log em vermelho com o comando para reconectar. A conta fica pausada e os vídeos esperam em "editado".

## Instagram

Requisitos: a conta do Instagram precisa ser **profissional** (Criador de conteúdo ou Empresa) e estar ligada a uma **Página do Facebook**.

1. No app do Instagram: **Configurações > Tipo de conta > Mudar para conta profissional**. Depois, ligue à sua Página do Facebook (crie uma se não tiver).
2. Em https://developers.facebook.com crie um app do tipo **Empresa** e adicione o produto **Instagram** (API com login do Facebook).
3. No **Explorador da API do Graph**, gere um token de usuário com as permissões `instagram_basic`, `instagram_content_publish`, `pages_show_list` e `pages_read_engagement`. Troque por um **token de longa duração** (60 dias) na ferramenta de depuração de token.
4. Descubra o ID da conta do Instagram: no Explorador, `GET me/accounts?fields=instagram_business_account`. O número em `instagram_business_account.id` é o seu `--ig-user-id`.
5. Rode `canaldark auth instagram gta6 --ig-user-id <ID>` e cole o token quando pedir.

Avisos importantes:
- O token de longa duração vence em cerca de 60 dias. Quando vencer, o painel avisa e você repete os passos 3 e 5.
- A Meta permite mais, mas o Canal Dark limita cada conta a **25 posts em 24 h** para não parecer spam. O que passar disso espera.
- O envio é direto (upload resumível da Meta), sem expor arquivos do seu computador. O modo por link público com `cloudflared` existe só como alternativa.
