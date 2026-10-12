"""Regras de leitura das folhas de cantoneiras, à maneira do OCR original
(nikuframedia-svg/ocr, prompts/ocr6_v3.txt): alinhamento das colunas, forma
de cada coluna, exemplos reais e confusões de caligrafia.

Exemplos e números medidos no plano real a 11/10 (31 695 modelos distintos),
escolhidos fora das folhas usadas no teste. Medido em folhas validadas: com
estas regras o Grok passou de 43% para 81% dos modelos certos.
"""

ALIGN = (
    "ALINHAMENTO DAS COLUNAS (crítico): a tabela tem colunas FIXAS. Em CADA linha "
    "devolve um valor por coluna, pela ordem, e SEMPRE todas as colunas — célula "
    "vazia ou ilegível é null SÓ nessa coluna. Nunca desloques o valor da coluna "
    "seguinte para a esquerda para tapar o buraco, e nunca copies um valor da linha "
    "de cima ou de baixo. Cada valor pertence à coluna por baixo da qual está "
    "fisicamente; se não tem a forma dessa coluna, estás a ler a coluna errada — "
    "confirma o alinhamento.\n"
)

CANTONEIRAS = ALIGN + (
    "FORMA DE CADA COLUNA:\n"
    "- OF: EXATAMENTE 6 dígitos (ex.: 264119, 265873). OV: 7 dígitos (ex.: 2604417).\n"
    "- PERFIL: cantoneira «lado x espessura» ou «lado x lado x espessura», como estiver "
    "escrito (ex.: 45x5, 100 X 10, L60x6, 50x50x4).\n"
    "- MODELO: código da peça, letras e dígitos, às vezes com hífen. Exemplos reais "
    "(copia tal como escrito, não normalizes): ZE-669, IT553D, 24Q2339, DLS6861, "
    "EA12B56, G4HA5014DT, W217608, M303-172, DLTN75A, IT160, A30H627, ZD-2326R.\n"
    "- QTD: número pequeno de peças. PERF. COMP.: um visto/X ou vazio.\n"
    "CONFUSÕES DE CALIGRAFIA (medidas nos códigos reais destas folhas):\n"
    "- Um traço vertical no INÍCIO do modelo, antes de uma letra, é a letra I "
    "(IT553D, não 1T553D): há 575 códigos «I+letra» e só 4 «1+letra».\n"
    "- A cauda do G não é um 7: ZG-…, nunca ZG7-… (não existe nenhum código ZG7).\n"
    "- Um círculo entre letras e dígitos é o algarismo 0 (quase nunca a letra O).\n"
    "- 5/S, 8/B, 2/Z: decide pela forma do código (letras no início, dígitos depois).\n"
)


def for_templates(names) -> str:
    """Regras a juntar ao prompt quando a folha pode ser de produção de cantoneiras."""
    return CANTONEIRAS if "cantoneiras_kanban" in set(names) else ""
