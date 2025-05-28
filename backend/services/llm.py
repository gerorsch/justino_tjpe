import os
from dotenv import load_dotenv
from typing import List, Optional, Callable
import anthropic

load_dotenv()

# Parâmetros de configuração
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))

# Inicializa o client da Anthropic
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def _extract_text_from_response(response_content) -> str:
    """
    Extrai o texto da resposta do Claude de forma segura.
    A API do Anthropic retorna response.content como uma lista de TextBlocks.
    """
    if not response_content:
        return ""
    
    # CORREÇÃO: Verificar se é uma string diretamente
    if isinstance(response_content, str):
        return response_content
    
    # CORREÇÃO: Se é lista de TextBlocks (caso comum)
    if isinstance(response_content, list):
        text_parts = []
        for block in response_content:
            if hasattr(block, 'text'):
                # Objeto TextBlock com atributo text
                text_parts.append(str(block.text))
            elif isinstance(block, dict) and 'text' in block:
                # Dicionário com chave text
                text_parts.append(str(block['text']))
            elif isinstance(block, str):
                # String direta
                text_parts.append(block)
            else:
                # Converte para string e loga para debug
                text_str = str(block)
                print(f"⚠️ Tipo inesperado de bloco: {type(block)} - {text_str[:100]}")
                text_parts.append(text_str)
        
        result = ''.join(text_parts)
        # CORREÇÃO: Normaliza quebras de linha
        result = result.replace('\\n', '\n').replace('\\r', '')
        return result
    
    # Fallback final
    result = str(response_content)
    result = result.replace('\\n', '\n').replace('\\r', '')
    return result

async def _call_llm(prompt: str, on_progress: Optional[Callable[[str], None]] = None) -> str:
    """
    Chama a API da Anthropic (Claude) e retorna a resposta do modelo.
    """
    try:
        if on_progress:
            on_progress("🤖 Consultando Claude...")
            
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            temperature=LLM_TEMPERATURE,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        if on_progress:
            on_progress("📝 Processando resposta...")
        
        # CORREÇÃO: Extrai o texto corretamente da resposta
        text = _extract_text_from_response(response.content)
        return text.strip()
        
    except Exception as e:
        error_msg = f"Erro na chamada da API Anthropic: {e}"
        print(error_msg)
        if on_progress:
            on_progress(f"❌ {error_msg}")
        return f"Erro na geração de conteúdo: {str(e)}"

async def gerar_resposta_llm(pergunta: str, documentos: List[dict]) -> str:
    """
    Gera uma resposta genérica para uma pergunta, utilizando o texto dos documentos como contexto.
    """
    try:
        context = "\n\n".join([doc.get("relatorio") or doc.get("text", "") for doc in documentos])
        prompt = f"Contexto:\n{context}\n\nPergunta: {pergunta}\nResposta:"
        return await _call_llm(prompt)
    except Exception as e:
        print(f"Erro em gerar_resposta_llm: {e}")
        return f"Erro ao gerar resposta: {str(e)}"

async def gerar_sentenca_llm(
    relatorio: str, 
    docs: List[dict], 
    instrucoes_usuario: str = "", 
    on_progress: Optional[Callable[[str], None]] = None,
    **kwargs
) -> str:
    """
    Gera a fundamentação e o dispositivo de uma sentença judicial com base no relatório e em documentos de referência.
    
    Args:
        relatorio: O relatório do processo
        docs: Lista de documentos de referência
        instrucoes_usuario: Instruções adicionais do usuário (opcional)
        on_progress: Callback para updates de progresso (opcional)
        **kwargs: Parâmetros adicionais (ignorados, para compatibilidade)
    """
    try:
        # Validação de entrada
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
        
        # Constrói os exemplos dos documentos
        exemplos = []
        for i, d in enumerate(docs, 1):
            exemplo = f"Exemplo {i}:\n"
            
            if d.get('relatorio'):
                exemplo += f"Relatório: {d['relatorio'][:500]}...\n\n"
            
            if d.get('fundamentacao'):
                exemplo += f"Fundamentação: {d['fundamentacao'][:1000]}...\n\n"
            
            if d.get('dispositivo'):
                exemplo += f"Dispositivo: {d['dispositivo'][:500]}...\n"
            
            exemplos.append(exemplo)
        
        contexto = "\n\n---\n\n".join(exemplos)

        # Adiciona as instruções do usuário se fornecidas
        instrucoes_adicionais = ""
        if instrucoes_usuario and instrucoes_usuario.strip():
            instrucoes_adicionais = f"\n\n### INSTRUÇÕES ADICIONAIS DO USUÁRIO:\n{instrucoes_usuario.strip()}\n"

        if on_progress:
            on_progress("✍️ Montando prompt da sentença...")

        prompt = f"""
### CONTEXTO
Você é um assistente judicial especializado na elaboração de sentenças. Com base no relatório do processo e nos documentos de referência fornecidos, você deve gerar uma sentença judicial completa. Siga rigorosamente a estrutura e requisitos abaixo, mas NÃO inclua os títulos das seções (como 'Fundamentação', 'Mérito', 'Dispositivo', etc.) na resposta final. Apenas escreva o texto corrido da sentença, respeitando a ordem lógica das seções, mas sem indicar seus títulos explicitamente. A seguir há exemplos de sentenças judiciais com relatório, fundamentação e dispositivo:

{contexto}

Agora, dado o NOVO RELATÓRIO abaixo, gere a fundamentação e o dispositivo:

NOVO RELATÓRIO:
{relatorio}
{instrucoes_adicionais}
## ESTRUTURA DA SENTENÇA

### 1. QUESTÕES PRELIMINARES
- Analise o processo e identifique se há questões preliminares suscitadas na contestação.
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
Intimem-se, atentando-se para a regra prevista no art.346 do CPC/2015.
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
- As citações devem ser exatamente iguais às dos documentos de referência.
"""
        
        if on_progress:
            on_progress("🎯 Gerando sentença...")
        
        resultado = await _call_llm(prompt, on_progress)
        
        if on_progress:
            on_progress("✅ Sentença gerada com sucesso!")
        
        return resultado
        
    except Exception as e:
        error_msg = f"Erro na geração da sentença: {str(e)}"
        print(error_msg)
        if on_progress:
            on_progress(f"❌ {error_msg}")
        return f"Erro ao gerar sentença: {str(e)}"