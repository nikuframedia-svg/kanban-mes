# Imagens de folhas antigas após migração

O endereço da fotografia devolvia 404 quando `image_path` ainda identificava uma
pasta de outra instalação, ou quando o original não estava na pasta de imagens.
A resolução procura o mesmo nome na pasta atual e só usa essa alternativa se o
SHA-256 corresponder ao registado. Não modifica o caminho histórico na base.
A fotografia, o OCR e as recuperações de cabeçalho/contagem usam esta resolução.

A reposição de um ficheiro em falta aceita exclusivamente os bytes correspondentes
ao hash da folha. Não sobrescreve ficheiros diferentes, exige a revisão atual e
audita a reposição como operação de sistema. É permitida em folhas validadas
porque não altera nenhum campo, produção, OCR, revisão ou geração da folha.

`GET /sheet/{uid}/photo/status` distingue disponibilidade e identidade esperada.
`POST /sheet/{uid}/photo/restore` recebe `revision` e o ficheiro multipart `image`.
Não existe formulário adicional para o operador. O mecanismo destina-se à
recuperação a partir de cópias verificadas, nunca a substituir o documento.

O comando `scripts/recover_original_images.py --source-db CAMINHO --base-url URL
--report RELATORIO` apenas simula. `--apply` repõe, uma imagem de cada vez, e
confirma o hash do original servido por HTTP. `--uid` pode limitar as folhas.
Falhas/conflitos interrompem o lote; repetir ignora imagens já disponíveis.
A base de origem é aberta apenas para leitura. Os documentos não são publicados
no GitHub: a transferência é feita diretamente para a aplicação correspondente.

Depois de instalar os commits no Windows pelo update habitual, verificar primeiro
a folha 6, repor apenas os originais ainda em falta e repetir o inventário HTTP.
Não reler OCR, validar folhas ou recriar registos para recuperar uma fotografia.
