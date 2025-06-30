import os
from dotenv import load_dotenv
from typing import List, Optional, Callable
from difflib import SequenceMatcher
import anthropic

load_dotenv()

LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _extract_text_from_response(response_content) -> str:
    if not response_content:
        return ""
    if isinstance(response_content, str):
        return response_content

    if isinstance(response_content, list):
        text_parts = []
        for block in response_content:
            if hasattr(block, 'text'):
                text_parts.append(str(block.text))
            elif isinstance(block, dict) and 'text' in block:
                text_parts.append(str(block['text']))
            elif isinstance(block, str):
                text_parts.append(block)
            else:
                text_parts.append(str(block))
        result = ''.join(text_parts)
        return result.replace('\\n', '\n').replace('\\r', '')

    result = str(response_content)
    return result.replace('\\n', '\n').replace('\\r', '')


def _is_redundant(new_text, existing_texts, threshold=0.85):
    for text in existing_texts:
        ratio = SequenceMatcher(None, new_text, text).ratio()
        if ratio > threshold:
            return True
    return False


def _extrair_trechos_relevantes(doc, max_chars=800):
    rel = doc.get("relatorio", "")[:300].strip()
    fund = doc.get("fundamentacao", "")[:max_chars].strip()
    disp = doc.get("dispositivo", "")[:300].strip()

    partes = []
    if rel:
        partes.append(f"Relatório: {rel}")
    if fund:
        partes.append(f"Fundamentação: {fund}")
    if disp:
        partes.append(f"Dispositivo: {disp}")
    return "\n".join(partes)


def _call_llm(prompt: str, on_progress: Optional[Callable[[str], None]] = None) -> str:
    try:
        if on_progress:
            on_progress("🤖 Consultando Claude...")
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            temperature=LLM_TEMPERATURE,
            messages=[{"role": "user", "content": prompt}]
        )
        if on_progress:
            on_progress("📝 Processando resposta...")
        text = _extract_text_from_response(response.content)
        return text.strip()
    except Exception as e:
        error_msg = f"Erro na chamada da API Anthropic: {e}"
        print(error_msg)
        if on_progress:
            on_progress(f"❌ {error_msg}")
        return f"Erro na geração de conteúdo: {str(e)}"


async def gerar_sentenca_llm(
    relatorio: str, 
    docs: List[dict], 
    instrucoes_usuario: str = "", 
    on_progress: Optional[Callable[[str], None]] = None,
    **kwargs
) -> str:
    try:
        if not relatorio or not relatorio.strip():
            error_msg = "Erro: Relatório não fornecido ou vazio."
            if on_progress:
                on_progress(f"❌ {error_msg}")
            return error_msg

        if not docs:
            error_msg = "Erro: Nenhum documento de referência fornecido."
            if on_progress:
                on_progress(f"❌ {error_msg}")
            return error_msg

        if on_progress:
            on_progress("📚 Preparando documentos de referência...")

        docs_ordenados = sorted(docs, key=lambda d: d.get("rerank_score", 0), reverse=True)
        exemplos = []
        trechos_adicionados = []

        for i, doc in enumerate(docs_ordenados):
            trecho = _extrair_trechos_relevantes(doc)
            if not _is_redundant(trecho, trechos_adicionados):
                exemplos.append(f"Exemplo {i+1}:\n{trecho}\n")
                trechos_adicionados.append(trecho)
            if len(exemplos) >= 5:
                break

        contexto = "\n\n---\n\n".join(exemplos)

        instrucoes_adicionais = ""
        if instrucoes_usuario and instrucoes_usuario.strip():
            instrucoes_adicionais = (
                "\n\n### INSTRUÇÕES ADICIONAIS DO USUÁRIO:\n" 
                + instrucoes_usuario.strip() + "\n"
            )

        if on_progress:
            on_progress("✍️ Montando prompt da sentença...")

        prompt = f"""
            ### CONTEXTO
            Você é um juiz especializado na elaboração de sentenças. Com base no relatório do processo e nos documentos de referência fornecidos, você deve gerar uma sentença judicial completa. Siga rigorosamente a estrutura e requisitos abaixo, mas NÃO inclua os títulos das seções (como 'Fundamentação', 'Mérito', 'Dispositivo', etc.) na resposta final. Apenas escreva o texto corrido da sentença, respeitando a ordem lógica das seções, mas sem indicar seus títulos explicitamente. A seguir há exemplos de sentenças judiciais com relatório, fundamentação e dispositivo:

            {contexto}

            Agora, dado o NOVO RELATÓRIO abaixo, gere a fundamentação e o dispositivo:

            NOVO RELATÓRIO:
            {relatorio}
            {instrucoes_adicionais}
            ## ESTRUTURA DA SENTENÇA

            ### 1. QUESTÕES PRELIMINARES
            - Analise o processo e identifique se há questões preliminares suscitadas na contestação (exemplo: inépcia da inicial, impugnação ao valor da causa, falta de interesse de agir, prescrição).
            - Se houver preliminares, desenvolva a fundamentação para cada uma delas separadamente.
            - Se não houver preliminares, inicie com a frase: "Ausentes questões preliminares, passo ao mérito."

            ### 2. MÉRITO
            - Inicie afirmando claramente o(s) fato(s) que constitui(em) a causa de pedir do autor.
            - Em seguida, apresente o principal argumento do réu em sua defesa.
            - Desenvolva a fundamentação com base nos documentos de referência, analisando:
            - Os fatos comprovados nos autos
            - As provas produzidas
            - A legislação aplicável
            - A jurisprudência pertinente
            - A doutrina relevante

            #### REGRAS IMPORTANTES PARA A FUNDAMENTAÇÃO:
            - As citações de lei, doutrina ou jurisprudência devem ser reproduzidas EXATAMENTE como constam nos documentos de referência, sem alterações.
            - A argumentação deve ser coerente, lógica e completa.
            - Utilize linguagem técnica-jurídica apropriada.
            - Analise todos os pedidos formulados na inicial.

            ### 3. DISPOSITIVO
            - Elabore o dispositivo da sentença, decidindo sobre todos os pedidos.
            - Fixe os honorários advocatícios conforme critérios do art. 85 do CPC.

            ### 4. CONCLUSÃO OBRIGATÓRIA
            Após o dispositivo e a condenação em honorários, encerre a sentença com EXATAMENTE o seguinte texto, sem nenhuma alteração:

            "Opostos embargos de declaração com efeito modificativo, intime-se a parte embargada para, querendo, manifestar-se no prazo de 05 (cinco) dias. (art. 1.023, § 2º, do CPC/2015), e decorrido o prazo, com ou sem manifestação, voltem conclusos. 
            
            Na hipótese de interposição de recurso de apelação, intime-se a parte apelada para apresentar contrarrazões (art. 1010, §1º, do CPC/2015). Havendo alegação – em sede de contrarrazões - de questões resolvidas na fase de conhecimento as quais não comportaram agravo de instrumento, intime-se a parte adversa (recorrente) para, em 15 (quinze) dias, manifestar-se a respeito delas (art. 1.009, §§ 1º e 2º, do CPC/2015). Havendo interposição de apelação adesiva, intime-se a parte apelante para contrarrazões, no prazo de 15 (quinze) dias (art. 1010, §2º, do CPC/2015). Em seguida, com ou sem resposta, sigam os autos ao e. Tribunal de Justiça do Estado de Pernambuco, com os cumprimentos deste Juízo (art. 1010, §3º, do CPC/2015).
            
            Após o trânsito em julgado, nada mais sendo requerido, arquivem-se os autos, com as cautelas de estilo, independentemente de nova determinação.
            
            Comunicações processuais necessárias.
            
            Cumpra-se.
            Recife-PE, data da assinatura digital.

            Maria Betânia Martins da Hora
            
            Juíza de Direito"

            ## INSTRUÇÕES FINAIS
            - Leia atentamente o relatório e todos os documentos de referência antes de iniciar a redação.
            - Siga rigorosamente a estrutura indicada.
            - Certifique-se de que o texto final está coeso, coerente e tecnicamente preciso.
            - Não omita nenhum dos elementos obrigatórios da sentença.
            - As citações de leis, doutrina e jurisprudência devem ser exatamente iguais às dos documentos de referência.

            """
        if on_progress:
            on_progress("🎯 Gerando sentença...")
        resultado = _call_llm(prompt, on_progress)
        if on_progress:
            on_progress("✅ Sentença gerada com sucesso!")
        return resultado

    except Exception as e:
        error_msg = f"Erro na geração da sentença: {e}"
        print(error_msg)
        if on_progress:
            on_progress(f"❌ {error_msg}")
        return f"Erro ao gerar sentença: {e}"
