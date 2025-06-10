from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass
from types import SimpleNamespace
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
load_dotenv()

from langchain_community.document_loaders import PyPDFLoader
from langchain.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain.llms.base import BaseLLM
import anthropic
from pypdf.errors import PdfReadError
from pypdf import PdfReader

# ───────────────────────────────────────── config ──────────────────────────
@dataclass
class Config:
    summary_model: str = os.getenv("SUMMARY_MODEL", "gpt-4.1-mini")
    report_model:  str = os.getenv("REPORT_MODEL",  "claude-sonnet-4-20250514")
    temperature:   float = float(os.getenv("TEMPERATURE", 0.3))
    max_tokens:    int = int(os.getenv("MAX_TOKENS", 2048))
    fallback_chars:int = int(os.getenv("FALLBACK_CHARS", 10000))
    verbose:       bool  = os.getenv("VERBOSE", "false").lower() in ("1","true","yes","t")

def log(msg: str, cfg: Config):
    if cfg.verbose:
        print(msg, file=sys.stderr)

# ────────────────────────────── detectar peça ─────────────────────────────
PIECE_KWS: Dict[str, list[str]] = {
    "peticao_inicial": ["petição inicial", "peticao inicial"],
    "contestacao":     ["contestação", "contestacao"],
    "decisao":         ["decisão", "decisao"],
    "despacho":        ["despacho"],
    "sentenca":        ["sentença", "sentenca"],
    "replica":         ["réplica", "replica"],
}

def classify_page(txt: str) -> str:
    low = txt.lower()
    for lab, kws in PIECE_KWS.items():
        if any(kw in low for kw in kws):
            return lab
    return "outros"

def extract_process_number(first_page_text: str) -> Optional[str]:
    """
    Extrai o número do processo da primeira página do PDF
    """
    if not first_page_text:
        return None
    
    # Padrões de número de processo (ordem de prioridade)
    patterns = [
        # Formato CNJ padrão: 0000000-00.0000.0.00.0000
        r'\b\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}\b',
        
        # Formato CNJ com mais dígitos: 0000000000-00.0000.0.00.0000
        r'\b\d{10}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}\b',
        
        # Formato antigo: 0000.00.000000-0
        r'\b\d{4}\.\d{2}\.\d{6}-\d{1}\b',
        
        # Padrões com texto: "Número: 0000000-00.0000.0.00.0000"
        r'(?:número|processo|autos)(?:\s*:?\s*|\s+n[º°]?\.?\s*)(\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4})',
        
        # Padrões com texto mais genéricos
        r'(?:processo|autos)(?:\s+n[º°]?\.?\s*|\s+)(\d+[-\.\d]+)',
        
        # Padrão específico do PJe: "Processo Eletrônico nº ..."
        r'processo\s+eletr[ôo]nico\s+n[º°]?\s*(\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4})',
    ]
    
    text_lower = first_page_text.lower()
    
    for i, pattern in enumerate(patterns):
        matches = re.findall(pattern, text_lower, re.IGNORECASE)
        if matches:
            # Para padrões com grupo de captura, pega o grupo
            if i >= 3:  # Padrões com grupos de captura
                number = matches[0] if isinstance(matches[0], str) else matches[0]
            else:  # Padrões diretos
                number = matches[0]
            
            # Validação adicional para formato CNJ
            if re.match(r'\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}', number):
                return number
            elif re.match(r'\d{10}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}', number):
                return number
            elif len(number) > 10:  # Outros formatos longos
                return number
    
    return None

def group_pages(pages: List, cfg: Config) -> tuple[Dict[str, List[str]], Optional[str]]:
    """
    Agrupa páginas por tipo de peça e extrai número do processo da primeira página
    """
    groups: Dict[str, List[str]] = {}
    cur: Optional[str] = None
    buf: List[str] = []
    process_number = None
    
    for i, p in enumerate(pages):
        # Extrai número do processo da primeira página
        if i == 0:
            process_number = extract_process_number(p.page_content)
            if process_number:
                log(f"📋 Número do processo identificado: {process_number}", cfg)
            else:
                log("⚠️ Número do processo não encontrado na primeira página", cfg)
        
        lab = classify_page(p.page_content)
        if lab != cur:
            if buf:
                groups.setdefault(cur or "outros", []).append("\n".join(buf))
                buf = []
            cur = lab
            log(f"→ nova peça '{lab}' na página {i+1}", cfg)
        buf.append(p.page_content)
    
    if buf:
        groups.setdefault(cur or "outros", []).append("\n".join(buf))
    
    return groups, process_number

# ─────────────────────────────── prompts ──────────────────────────────────
SUMMARY_PT = PromptTemplate(
    template=textwrap.dedent("""
        Leia o texto e liste **cada ato processual relevante** já no formato:
        – frase concisa (ID 123456789).
        Não inclua títulos como "Petição Inicial".
        Texto:
        {texto}
    """),
    input_variables=["texto"],
)

# Prompt modificado para incluir número do processo
INSTRUCOES_COM_PROCESSO = textwrap.dedent("""
    TAREFA
    Elabore um relatório analítico e detalhado do processo judicial fornecido, com base nos documentos constantes dos autos. Utilize estilo direto, informando de maneira objetiva o conteúdo de cada ato processual relevante, com a respectiva identificação por ID.

    NÚMERO DO PROCESSO
    O processo tem o número: {numero_processo}

    INSTRUÇÕES ESPECÍFICAS
    - Inicie o relatório com "Processo nº {numero_processo}"
    - Não inclua os títulos formais das peças (ex: "Petição Inicial", "Despacho", "Decisão", etc.).
    - Identifique os atos com uma frase introdutória direta e o número do ID entre parênteses, como nos exemplos abaixo:
      - Foi concedida a justiça gratuita (ID 36457517).
      - Tutela de urgência deferida (ID 37574668).
      - Réplica apresentada (ID 42715461).
    - Ao tratar de manifestações das partes (petições), explique brevemente seu conteúdo jurídico.
    - Na contestação, redija um parágrafo mais desenvolvido, contendo:
      - Os principais fatos narrados;
      - Os fundamentos jurídicos alegados;
      - O pedido final;
      - E se foram juntados documentos e procurações.
    - A Réplica deve ser indicada apenas com a frase: "Réplica no ID ___.", sem síntese adicional.
    - Inclua todas as petições, exceto as de habilitação de advogado.
    - Ignore todas as certidões, exceto:
      - Certidões de citação positiva;
      - Certidões de decurso de prazo (ex: "decorrido o prazo sem manifestação").

    MODELO DE FORMATAÇÃO DO RELATÓRIO
    Processo nº {numero_processo}
    
    Vistos, etc.
    NOME DO AUTOR, qualificado na inicial, por intermédio de advogado legalmente habilitado por instrumento de mandado, propôs AÇÃO EM ITÁLICO contra NOME DO RÉU, também qualificado, com o objetivo de sintetizar o pedido da ação em minúsculas.
    A parte autora alegou que [...] (ID: ___).
    Foi deferida a gratuidade judiciária (ID: ___).
    Tutela de urgência concedida [...] (ID: ___).
    Contestação apresentada (ID: ___), na qual a parte ré [...]
    Réplica no ID ___.
    Manifestação da parte autora [...] (ID ___).
    Decurso de prazo certificado (ID ___).
    [Outros atos relevantes, em sequência cronológica].
""")

INSTRUCOES_SEM_PROCESSO = textwrap.dedent("""
    TAREFA
    Elabore um relatório analítico e detalhado do processo judicial fornecido, com base nos documentos constantes dos autos. Utilize estilo direto, informando de maneira objetiva o conteúdo de cada ato processual relevante, com a respectiva identificação por ID.

    INSTRUÇÕES ESPECÍFICAS
    - Não inclua os títulos formais das peças (ex: "Petição Inicial", "Despacho", "Decisão", etc.).
    - Identifique os atos com uma frase introdutória direta e o número do ID entre parênteses, como nos exemplos abaixo:
      - Foi concedida a justiça gratuita (ID 36457517).
      - Tutela de urgência deferida (ID 37574668).
      - Réplica apresentada (ID 42715461).
    - Ao tratar de manifestações das partes (petições), explique brevemente seu conteúdo jurídico.
    - Na contestação, redija um parágrafo mais desenvolvido, contendo:
      - Os principais fatos narrados;
      - Os fundamentos jurídicos alegados;
      - O pedido final;
      - E se foram juntados documentos e procurações.
    - A Réplica deve ser indicada apenas com a frase: "Réplica no ID ___.", sem síntese adicional.
    - Inclua todas as petições, exceto as de habilitação de advogado.
    - Ignore todas as certidões, exceto:
      - Certidões de citação positiva;
      - Certidões de decurso de prazo (ex: "decorrido o prazo sem manifestação").

    MODELO DE FORMATAÇÃO DO RELATÓRIO
    Vistos, etc.
    NOME DO AUTOR, qualificado na inicial, por intermédio de advogado legalmente habilitado por instrumento de mandado, propôs AÇÃO EM ITÁLICO contra NOME DO RÉU, também qualificado, com o objetivo de sintetizar o pedido da ação em minúsculas.
    A parte autora alegou que [...] (ID: ___).
    Foi deferida a gratuidade judiciária (ID: ___).
    Tutela de urgência concedida [...] (ID: ___).
    Contestação apresentada (ID: ___), na qual a parte ré [...]
    Réplica no ID ___.
    Manifestação da parte autora [...] (ID ___).
    Decurso de prazo certificado (ID ___).
    [Outros atos relevantes, em sequência cronológica].
""")

REPORT_PT = PromptTemplate(
    template="""{instr}

CONTEÚDO DOS AUTOS:
{linhas_atos}

RELATÓRIO:""",
    input_variables=["instr", "linhas_atos"],
)

# ─────────────────────────── wrapper Claude CORRIGIDO ─────────────────────────────
class AnthropicClaudeWrapper:
    def __init__(self, model: str, max_tokens: int = 2048, temperature: float = 0.3):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _extract_text_from_response(self, response_content) -> str:
        """
        Extrai o texto da resposta do Claude de forma segura.
        A API do Anthropic retorna response.content como uma lista de TextBlocks.
        """
        if not response_content:
            return ""
        
        if isinstance(response_content, str):
            # Se já é string, retorna diretamente
            return response_content
        
        if isinstance(response_content, list):
            # Se é lista de TextBlocks, extrai o texto de cada um
            text_parts = []
            for block in response_content:
                if hasattr(block, 'text'):
                    # Objeto TextBlock com atributo text
                    text_parts.append(block.text)
                elif isinstance(block, dict) and 'text' in block:
                    # Dicionário com chave text
                    text_parts.append(block['text'])
                elif isinstance(block, str):
                    # String direta
                    text_parts.append(block)
                else:
                    # Fallback: converte para string
                    text_parts.append(str(block))
            
            return ''.join(text_parts)
        
        # Fallback final: converte para string
        return str(response_content)

    def invoke(self, input_data: dict) -> str:
        user_msg = input_data.get("prompt") or (
            input_data["instr"]
            + "\n\nCONTEÚDO DOS AUTOS:\n"
            + input_data["linhas_atos"]
            + "\n\nRELATÓRIO:"
        )
        
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[{"role": "user", "content": user_msg}],
            )
            
            # CORREÇÃO: Extrai o texto corretamente da resposta do Claude
            text = self._extract_text_from_response(response.content)
            return text.strip()
            
        except Exception as e:
            print(f"Erro na chamada da API Anthropic: {e}", file=sys.stderr)
            return f"Erro na geração de conteúdo: {str(e)}"

# ───────────────────────── funções LLM CORRIGIDAS ────────────────────────────────────

def _extract_text_safely(response) -> str:
    """
    Função auxiliar para extrair texto de respostas de LLM de forma segura.
    Funciona tanto para responses do Claude quanto do OpenAI.
    """
    if not response:
        return ""
    
    # Se tem atributo content
    if hasattr(response, "content"):
        content = response.content
        
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            # Lista de TextBlocks (Claude)
            text_parts = []
            for block in content:
                if hasattr(block, 'text'):
                    text_parts.append(block.text)
                elif isinstance(block, dict) and 'text' in block:
                    text_parts.append(block['text'])
                elif isinstance(block, str):
                    text_parts.append(block)
                else:
                    text_parts.append(str(block))
            return ''.join(text_parts)
        else:
            return str(content)
    
    # Se é string direta
    if isinstance(response, str):
        return response
    
    # Fallback final
    return str(response)

def get_llm(model: str, cfg: Config):
    use_claude = os.getenv("USE_CLAUDE_FOR_REPORT", "false").lower() in ("1", "true", "yes", "t")
    if use_claude and model.startswith("claude"):
        return AnthropicClaudeWrapper(model=model, max_tokens=cfg.max_tokens, temperature=cfg.temperature)
    else:
        return ChatOpenAI(model_name=model, temperature=cfg.temperature, max_tokens=cfg.max_tokens)

def summarize(text: str, llm: BaseLLM, cfg: Config) -> str:
    try:
        resp = (SUMMARY_PT | llm).invoke({"texto": text})
        content = _extract_text_safely(resp)
        return content.strip()
    except Exception as e:
        print(f"Erro na função summarize: {e}", file=sys.stderr)
        return f"Documento processado com {len(text)} caracteres."


def build_report(atos: str, process_number: Optional[str], cfg: Config) -> str:
    """
    Constrói o relatório final, incluindo número do processo se disponível
    """
    try:
        llm = get_llm(cfg.report_model, cfg)
        
        # Escolhe as instruções corretas baseado na disponibilidade do número do processo
        if process_number:
            instructions = INSTRUCOES_COM_PROCESSO.format(numero_processo=process_number)
        else:
            instructions = INSTRUCOES_SEM_PROCESSO
        
        formatted_prompt = REPORT_PT.format(instr=instructions, linhas_atos=atos)
        
        if hasattr(llm, "invoke"):
            resp = llm.invoke({"prompt": formatted_prompt})
        else:
            resp = llm.predict(formatted_prompt)
        
        # CORREÇÃO: Usa função auxiliar para extrair texto de forma segura
        content = _extract_text_safely(resp)
        
        # Se temos número do processo mas ele não aparece no início do relatório, adiciona
        if process_number and not content.strip().startswith(f"Processo nº {process_number}"):
            content = f"Processo nº {process_number}\n\n{content.strip()}"
        
        return content.strip()
    
    except Exception as e:
        print(f"Erro na função build_report: {e}", file=sys.stderr)
        # Retorna um relatório básico em caso de erro
        if process_number:
            return f"Processo nº {process_number}\n\nRelatório: Processo analisado com base nos atos processuais fornecidos.\n\n{atos}"
        else:
            return f"Relatório: Processo analisado com base nos atos processuais fornecidos.\n\n{atos}"

def clean_textblock_artifacts(text: str) -> str:
    """
    Remove resíduos de TextBlock que possam aparecer no texto final.
    """
    if not text:
        return ""
    
    # Remove padrões como "TextBlock(citations=None, text="
    text = re.sub(r'TextBlock\([^)]*\)', '', text)
    
    # Remove padrões como "[TextBlock(citations=None, text="...")]"
    text = re.sub(r'\[TextBlock\([^]]*\)\]', '', text)
    
    # Remove "citations=None, text=" e variações
    text = re.sub(r'citations=None,\s*text=', '', text)
    text = re.sub(r'citations=[^,]*,\s*text=', '', text)
    text = re.sub(r"type='text'", '', text)
    
    # Remove aspas extras no início/fim
    text = text.strip('"\'')
    
    # Remove quebras de linha excessivas
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

# ───────────────────────── geração principal ──────────────────────────────
def generate(
    pdf: Path,
    cfg: Config,
    on_progress: Optional[Callable[[str], None]] = None
) -> str:
    summary_llm = get_llm(cfg.summary_model, cfg)
    # ── CACHE: se já houver relatório para este PDF, retorna imediatamente

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    cache_path = Path(f"/tmp/report_{digest}.txt")
    if cache_path.exists():
        log("♻️  Usando relatório em cache", cfg)
        return cache_path.read_text(encoding="utf-8")
    try:
        # 1) Carrega o PDF, com fallback em caso de PdfReadError
        try:
            pages = PyPDFLoader(str(pdf)).load()
        except PdfReadError as e:
            log(f"⚠️ PyPDFLoader falhou ({e}), usando fallback com PdfReader(strict=False)", cfg)
            reader = PdfReader(str(pdf), strict=False)
            pages = []
            for idx, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except PdfReadError:
                    log(f"   – erro extraindo texto da página {idx}, pulando conteúdo", cfg)
                    text = ""
                pages.append(SimpleNamespace(page_content=text))

        # progresso inicial
        msg = f"📄 PDF com {len(pages)} páginas carregado"
        log(msg, cfg)
        if on_progress:
            on_progress(msg)

        # 2) Agrupa por peça e extrai número do processo
        grupos, process_number = group_pages(pages, cfg)
        
        if process_number:
            log(f"✅ Processo identificado: {process_number}", cfg)
            if on_progress:
                on_progress(f"📋 Processo nº {process_number} identificado")

        # 3) Lê e resume chunks
        linhas: List[str] = []

        # 1) Primeiro, monte a lista de (label, texto) a resumir
        chunks_para_resumir: List[Tuple[str, str]] = []
        for label, blocos in grupos.items():
            sec_msg = f"🔍 Lendo seção '{label}' ({len(blocos)} chunks)"
            log(sec_msg, cfg)
            if on_progress:
                on_progress(sec_msg)

            # Junta todos os blocos da seção
            texto_secao = "\n".join(blocos)

            # Se for muito grande, divide em sub-chunks grandes
            if len(texto_secao) > cfg.fallback_chars:
                parts = [
                    texto_secao[i : i + cfg.fallback_chars]
                    for i in range(0, len(texto_secao), cfg.fallback_chars)
                ]
            else:
                parts = [texto_secao]

            # Registra cada parte para resumir depois
            for pi, part in enumerate(parts, start=1):
                if len(parts) > 1:
                    sub_msg = f"   ↳ subchunk {pi}/{len(parts)}"
                    log(sub_msg, cfg)
                    if on_progress:
                        on_progress(sub_msg)
                chunks_para_resumir.append((label, part))

        # 2) Agora, faça efetivamente menos chamadas ao LLM
        # (supondo que você já tenha summary_llm = get_llm(...) criado fora)
       # 2) Paraleliza chamadas de resumo
        # (substitui o loop sequencial por ThreadPoolExecutor)

        def _job(label_text):
            label, texto = label_text
            # use summary_llm diretamente (ou adapte summarize to accept llm)
            resumo = summary_llm.predict(texto)
            return clean_textblock_artifacts(resumo)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = { pool.submit(_job, lt): lt for lt in chunks_para_resumir }
            for fut in as_completed(futures):
                linhas.append(fut.result())

        # 3) Concatena tudo para gerar o report
        atos = "\n".join(linhas)

        # 4) Construção do relatório final
        start_msg = "⚙️ Construindo relatório final..."
        log(start_msg, cfg)
        if on_progress:
            on_progress(start_msg)
        
        # Passa o número do processo para o build_report
        report = build_report(atos, process_number, cfg)
        
        # LIMPEZA FINAL: Remove qualquer artefato de TextBlock restante
        report_limpo = clean_textblock_artifacts(report)
        cache_path.write_text(report_limpo, encoding="utf-8")
        
        done_msg = "✅ Relatório pronto"
        log(done_msg, cfg)
        if on_progress:
            on_progress(done_msg)

        return report_limpo
    
    except Exception as e:
        error_msg = f"Erro na geração do relatório: {str(e)}"
        print(error_msg, file=sys.stderr)
        if on_progress:
            on_progress(f"❌ {error_msg}")
        # Retorna um relatório básico em caso de erro total
        return f"Erro no processamento: {str(e)}"

# ───────────────────────────── CLI ────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Relatório analítico peça-aware (dual-model com suporte ao Claude)"
    )
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = Config(verbose=not args.quiet)
    rel = generate(args.pdf, cfg)

    if args.output:
        args.output.write_text(rel, encoding="utf-8")
        log(f"Relatório salvo em {args.output}", cfg)
    else:
        print(rel)