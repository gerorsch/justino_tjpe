import os
import uuid
import asyncio
import json
import re
import glob
import time
from pathlib import Path as FSPath
from typing import List, Optional, AsyncGenerator

from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from preprocessing.process_report_pipeline import Config, generate as gerar_relatorio
from services.retrieval_rerank import recuperar_documentos_similares as semantic_search_rerank
from services.llm import gerar_sentenca_llm
from services.docx_utils import salvar_sentenca_como_docx, salvar_docs_referencia
from services.docx_parser import parse_docx_bytes
from preprocessing.sentence_indexing_rag import setup_elasticsearch

app = FastAPI(title="RAG TJPE API")


# Configurar CORS

# Configurar CORS para produção
def get_allowed_origins():
    """Retorna lista de origens permitidas baseada no ambiente"""
    
    # URLs de produção para justino.digital
    production_origins = [
        "https://justino.digital",
        "https://www.justino.digital",
        "https://api.justino.digital",
    ]
    
    # URLs de desenvolvimento local
    development_origins = [
        "http://localhost:8501",
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8501",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ]
    
    # Verificar se está em produção ou desenvolvimento
    environment = os.getenv("ENVIRONMENT", "development").lower()
    
    if environment == "production":
        # Apenas URLs de produção
        allowed_origins = production_origins.copy()
        
        # Adicionar URLs customizadas da variável de ambiente
        custom_origins = os.getenv("ALLOWED_ORIGINS", "")
        if custom_origins:
            custom_list = [origin.strip() for origin in custom_origins.split(",") if origin.strip()]
            allowed_origins.extend(custom_list)
            
    else:
        # Desenvolvimento: incluir localhost + produção para testes
        allowed_origins = development_origins + production_origins
        
        # Adicionar URLs customizadas
        custom_origins = os.getenv("ALLOWED_ORIGINS", "")
        if custom_origins:
            custom_list = [origin.strip() for origin in custom_origins.split(",") if origin.strip()]
            allowed_origins.extend(custom_list)
    
    # Remover duplicatas mantendo ordem
    seen = set()
    unique_origins = []
    for origin in allowed_origins:
        if origin not in seen:
            seen.add(origin)
            unique_origins.append(origin)
    
    return unique_origins

# Obter origens permitidas
allowed_origins = get_allowed_origins()

# Log das origens para debug (remover em produção se necessário)
print(f"🌐 CORS - Origens permitidas: {allowed_origins}")

# Configurar middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,  # URLs específicas em produção
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Resto do seu código FastAPI...
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "environment": os.getenv("ENVIRONMENT", "development"),
        "allowed_origins": len(allowed_origins)
    }

# ─────────────────────────── Eventos de Inicialização ───────────────────────────

@app.on_event("startup")
async def startup_event():
    """
    1) Limpa arquivos /tmp
    2) Garante que o índice do Elasticsearch exista e, se vazio, popule com sentenças
    """
    # ———————————— limpeza atual ————————————
    print("🧹 Limpando arquivos temporários antigos...")
    now = time.time()
    patterns = ["/tmp/sentenca_*.docx", "/tmp/referencias_*.zip", "/tmp/*.pdf"]
    removed_count = 0
    
    for pattern in patterns:
        for file in glob.glob(pattern):
            try:
                if now - os.path.getctime(file) > 86400:  # 24 horas
                    os.remove(file)
                    removed_count += 1
            except:
                pass
    
    if removed_count > 0:
        print(f"✅ Removidos {removed_count} arquivos temporários antigos")
    
    # ———————————— novo bloco ————————————
    print("🔍 Configurando Elasticsearch (índice + dados)…")
    try:
        setup_elasticsearch()
        print("✅ Elasticsearch pronto para uso")
    except Exception as e:
        print(f"❌ Falha no setup do Elasticsearch: {e}")
        # opcional: raise para abortar startup
        # raise



# ─────────────────────────── Utilitários ───────────────────────────

def decodificar_unicode(texto: str) -> str:
    """
    Decodifica caracteres unicode e limpa o texto da sentença
    """
    if not texto:
        return texto
    
    try:
        # 1. Tenta decodificar unicode
        texto_limpo = texto.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        texto_limpo = texto
    
    # 2. Normaliza quebras de linha
    texto_limpo = texto_limpo.replace('\\n', '\n')
    texto_limpo = texto_limpo.replace('\\r', '')
    texto_limpo = texto_limpo.replace('\r\n', '\n')
    texto_limpo = texto_limpo.replace('\r', '\n')
    
    # 3. Remove quebras excessivas mas preserva formatação de parágrafos
    import re
    texto_limpo = re.sub(r'\n{3,}', '\n\n', texto_limpo)
    
    # 4. Remove espaços em branco no final das linhas
    linhas = [linha.rstrip() for linha in texto_limpo.split('\n')]
    texto_limpo = '\n'.join(linhas)
    
    return texto_limpo.strip()

def extrair_numero_processo(texto: str) -> Optional[str]:
    """
    Extrai o número do processo do texto do relatório.
    Prioriza o formato CNJ padrão que aparece no cabeçalho do PJe.
    """
    # Processa primeiro as primeiras linhas onde geralmente está o número no PJe
    linhas_iniciais = '\n'.join(texto.split('\n')[:20])  # Primeiras 20 linhas
    
    # Padrões em ordem de prioridade (formato CNJ primeiro)
    padroes = [
        # 1. Formato CNJ padrão do PJe: 0000000-00.0000.0.00.0000
        r'\b\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}\b',
        
        # 2. Formato CNJ com zeros à esquerda: 0000000000-00.0000.0.00.0000  
        r'\b\d{10}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}\b',
        
        # 3. Com texto "Número:" (comum no PJe)
        r'(?:número|n[º°])\s*:?\s*(\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4})',
        
        # 4. Formato antigo
        r'\b\d{4}\.\d{2}\.\d{6}-\d{1}\b',
        
        # 5. Outros padrões com texto
        r'(?:processo|autos)(?:\s+n[º°]?\.?\s*|\s*:?\s*)(\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4})',
    ]
    
    # Primeiro procura nas linhas iniciais
    for i, padrao in enumerate(padroes):
        matches = re.findall(padrao, linhas_iniciais, re.IGNORECASE)
        if matches:
            numero = matches[0] if isinstance(matches[0], str) else matches[0]
            # Valida se é formato CNJ válido
            if re.match(r'\d{7,10}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}', numero):
                return numero
    
    # Se não encontrar nas primeiras linhas, procura no texto completo
    for i, padrao in enumerate(padroes):
        matches = re.findall(padrao, texto, re.IGNORECASE)
        if matches:
            numero = matches[0] if isinstance(matches[0], str) else matches[0]
            if re.match(r'\d{7,10}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}', numero):
                return numero
    
    return None

def gerar_nome_arquivo_sentenca(numero_processo: Optional[str] = None) -> str:
    """
    Gera um nome de arquivo inteligente para a sentença
    """
    from datetime import datetime
    
    if numero_processo:
        # Remove caracteres especiais do número do processo
        numero_limpo = numero_processo.replace('-', '').replace('.', '').replace('/', '')
        return f"sentenca_{numero_limpo}_{datetime.now().strftime('%Y%m%d')}"
    else:
        return f"sentenca_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def gerar_nome_arquivo_referencias(numero_processo: Optional[str] = None) -> str:
    """
    Gera um nome de arquivo inteligente para as referências
    """
    from datetime import datetime
    
    if numero_processo:
        numero_limpo = numero_processo.replace('-', '').replace('.', '').replace('/', '')
        return f"referencias_{numero_limpo}"
    else:
        return f"referencias_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

def validar_arquivo_pdf(pdf: UploadFile) -> None:
    """
    Valida se o arquivo é um PDF válido e tem tamanho adequado
    """
    # Validação de tamanho (200MB)
    if pdf.size and pdf.size > 200 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Arquivo muito grande. Máximo: 200MB")
    
    # Validação de tipo
    if not pdf.content_type or "pdf" not in pdf.content_type.lower():
        raise HTTPException(status_code=400, detail="Arquivo deve ser um PDF")
    
    # Validação de nome do arquivo
    if not pdf.filename or not pdf.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Arquivo deve ter extensão .pdf")

def limpar_arquivo_temporario(path: str) -> None:
    """
    Remove arquivo temporário com tratamento de erro
    """
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"⚠️ Erro ao remover arquivo temporário {path}: {e}")


# ─────────────────────────── Health‐check ───────────────────────────

# @app.get("/health")
# async def health():
#     return {"status": "ok", "timestamp": time.time()}


# ───────────────────────── Rotas REST (síncronas) ──────────────────────

class ExtrairRelatorioResp(BaseModel):
    relatorio: str
    numero_processo: Optional[str] = None


@app.post("/processar", response_model=ExtrairRelatorioResp)
async def processar_pdf(pdf: UploadFile = File(...)):
    """
    Processa um PDF e extrai o relatório do processo
    """
    # Validações
    validar_arquivo_pdf(pdf)
    
    tmp_id = uuid.uuid4().hex
    tmp_path = f"/tmp/{tmp_id}.pdf"
    
    try:
        # Salva arquivo temporário
        with open(tmp_path, "wb") as f:
            f.write(await pdf.read())

        # Processa o PDF
        cfg = Config()
        texto = gerar_relatorio(FSPath(tmp_path), cfg)
        
        # Extrai número do processo se presente
        numero_processo = extrair_numero_processo(texto)
        
        return ExtrairRelatorioResp(
            relatorio=texto,
            numero_processo=numero_processo
        )
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro no processamento: {str(e)}")
    
    finally:
        # Limpa arquivo temporário
        limpar_arquivo_temporario(tmp_path)


class Documento(BaseModel):
    id: str
    relatorio: str
    fundamentacao: str
    dispositivo: str
    score: float
    rerank_score: float


class GerarSentencaResp(BaseModel):
    documentos: List[Documento]
    sentenca: str
    sentenca_url: str
    referencias_url: str
    numero_processo: Optional[str] = None


@app.post("/gerar-sentenca", response_model=GerarSentencaResp)
async def gerar_sentenca_endpoint(
    relatorio: str = Form(...),
    instrucoes_usuario: Optional[str] = Form(None),
    numero_processo: Optional[str] = Form(None),
    top_k: int = Form(10),
    rerank_top_k: int = Form(5),
    arquivos_referencia: Optional[List[UploadFile]] = File(None),
    buscar_na_base: bool = Form(False),
):
    """
    Gera uma sentença completa baseada no relatório e documentos de referência
    """
    # Validação de parâmetros
    if not relatorio.strip():
        raise HTTPException(status_code=400, detail="Relatório não pode estar vazio")
    
    if top_k < 1 or top_k > 20:
        raise HTTPException(status_code=400, detail="Top K deve estar entre 1 e 20")
    
    if rerank_top_k < 1 or rerank_top_k > 10:
        raise HTTPException(status_code=400, detail="Rerank Top K deve estar entre 1 e 10")

    try:
        # 1) Monta lista inicial com arquivos enviados, se houver
        docs: List[dict] = []
        if arquivos_referencia:
            for upload in arquivos_referencia:
                if not upload.filename.lower().endswith('.docx'):
                    raise HTTPException(status_code=400, detail=f"Arquivo {upload.filename} deve ser DOCX")
                
                data = await upload.read()
                sec = parse_docx_bytes(data)
                sec["id"] = upload.filename or uuid.uuid4().hex
                docs.append(sec)
            
            # 1a) Se marcado, também busca na base
            if buscar_na_base:
                extra = semantic_search_rerank(
                    relatorio, top_k=top_k, rerank_top_k=rerank_top_k
                )
                docs.extend(extra)
        else:
            # 1b) Sem arquivos enviados, busca obrigatória
            docs = semantic_search_rerank(
                relatorio, top_k=top_k, rerank_top_k=rerank_top_k
            )
            if not docs:
                raise HTTPException(status_code=404, detail="Nenhum documento semelhante encontrado")

        # 2) Geração via LLM
        sentenca = await gerar_sentenca_llm(
            relatorio=relatorio,
            docs=docs,
            instrucoes_usuario=instrucoes_usuario,
        )

        # 3) Gera nomes de arquivo baseados no número do processo
        nome_base_sentenca = gerar_nome_arquivo_sentenca(numero_processo)
        nome_base_referencias = gerar_nome_arquivo_referencias(numero_processo)
        
        sent_id = f"{nome_base_sentenca}_{uuid.uuid4().hex[:8]}"
        refs_id = f"{nome_base_referencias}_{uuid.uuid4().hex[:8]}"
        
        sent_path = f"/tmp/{sent_id}.docx"
        refs_path = f"/tmp/{refs_id}.zip"

        # 4) Salvar .docx e ZIP de referências com número do processo
        salvar_sentenca_como_docx(
            relatorio=relatorio,
            fundamentacao_dispositivo=sentenca,
            arquivo_path=sent_path,
            numero_processo=numero_processo
        )
        salvar_docs_referencia(docs, refs_path)

        # 5) Montar e retornar
        retorno = [
            Documento(
                id=d.get("id", ""),
                relatorio=d.get("relatorio", ""),
                fundamentacao=d.get("fundamentacao", ""),
                dispositivo=d.get("dispositivo", ""),
                score=d.get("score", 0.0),
                rerank_score=d.get("rerank_score", 0.0),
            ) for d in docs
        ]
        
        return GerarSentencaResp(
            documentos=retorno,
            sentenca=decodificar_unicode(sentenca),
            sentenca_url=f"/download/sentenca/{sent_id}.docx",
            referencias_url=f"/download/referencias/{refs_id}.zip",
            numero_processo=numero_processo
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na geração da sentença: {str(e)}")


# ───────────────────────────── Downloads ──────────────────────────────

@app.get("/download/sentenca/{file_id}.docx")
def download_sentenca(file_id: str):
    """
    Download da sentença gerada
    """
    # Validação básica do file_id
    if not re.match(r'^[a-zA-Z0-9_-]+$', file_id):
        raise HTTPException(status_code=400, detail="ID de arquivo inválido")
    
    path = f"/tmp/{file_id}.docx"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Sentença não encontrada")
    
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{file_id}.docx"
    )


@app.get("/download/referencias/{file_id}.zip")
def download_referencias(file_id: str):
    """
    Download das referências (ZIP)
    """
    # Validação básica do file_id
    if not re.match(r'^[a-zA-Z0-9_-]+$', file_id):
        raise HTTPException(status_code=400, detail="ID de arquivo inválido")
    
    path = f"/tmp/{file_id}.zip"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="ZIP não encontrado")
    
    return FileResponse(
        path, 
        media_type="application/zip",
        filename=f"{file_id}.zip"
    )


# ───────────────────────────── Rotas SSE ─────────────────────────────

async def _run_in_thread(func):
    """
    Executa função em thread separada
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func)


@app.post("/stream/processar")
async def stream_processar_pdf(pdf: UploadFile = File(...)) -> EventSourceResponse:
    """
    Processa PDF com streaming de progresso
    """
    # Validação rápida
    if pdf.size and pdf.size > 200 * 1024 * 1024:
        async def error_generator():
            yield f"event: error\ndata: Arquivo muito grande. Máximo: 200MB\n\n"
        return EventSourceResponse(error_generator())
    
    tmp_id = uuid.uuid4().hex
    tmp_path = f"/tmp/{tmp_id}.pdf"
    
    try:
        with open(tmp_path, "wb") as f:
            f.write(await pdf.read())
    except Exception as e:
        async def error_generator():
            yield f"event: error\ndata: Erro ao salvar arquivo: {str(e)}\n\n"
        return EventSourceResponse(error_generator())

    cfg = Config()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def on_progress(msg: str):
        queue.put_nowait(msg)

    def worker():
        try:
            report = gerar_relatorio(FSPath(tmp_path), cfg, on_progress=on_progress)
            queue.put_nowait("__COMPLETE__:" + report)
        except Exception as e:
            queue.put_nowait(f"__ERROR__:Erro na extração: {str(e)}")
        finally:
            # Limpa arquivo temporário
            limpar_arquivo_temporario(tmp_path)

    asyncio.create_task(_run_in_thread(worker))

    async def event_generator() -> AsyncGenerator[str, None]:
        timeout_count = 0
        while True:
            try:
                # Timeout de 5 minutos para evitar conexões "penduradas"
                item = await asyncio.wait_for(queue.get(), timeout=300)
                
                if item.startswith("__COMPLETE__:"):
                    yield f"event: complete\ndata: {item.split('__COMPLETE__:',1)[1]}\n\n"
                    break
                elif item.startswith("__ERROR__:"):
                    yield f"event: error\ndata: {item.split('__ERROR__:',1)[1]}\n\n"
                    break
                else:
                    yield f"event: message\ndata: {item}\n\n"
                    timeout_count = 0  # Reset timeout count
                    
            except asyncio.TimeoutError:
                timeout_count += 1
                if timeout_count > 3:  # 3 timeouts = 15 minutos
                    yield f"event: error\ndata: Timeout na extração do relatório\n\n"
                    break
                yield f"event: message\ndata: Processando... (aguarde)\n\n"

    return EventSourceResponse(event_generator(), ping=30)

@app.post("/stream/gerar-sentenca")
async def stream_gerar_sentenca(
    relatorio: str = Form(...),
    instrucoes_usuario: Optional[str] = Form(None),
    numero_processo: Optional[str] = Form(None),
    top_k: int = Form(10),
    rerank_top_k: int = Form(5),
    arquivos_referencia: Optional[List[UploadFile]] = File(None),
    buscar_na_base: bool = Form(False),
) -> EventSourceResponse:
    """
    Gera sentença com streaming de progresso
    """
    queue: asyncio.Queue[str] = asyncio.Queue()
    
    # Validações básicas
    if not relatorio.strip():
        async def error_generator():
            yield f"event: error\ndata: Relatório não pode estar vazio\n\n"
        return EventSourceResponse(error_generator())
    
    try:
        # Preparação inicial dos documentos (rápida, pode ser síncrona)
        docs: List[dict] = []
        if arquivos_referencia:
            for upload in arquivos_referencia:
                if not upload.filename.lower().endswith('.docx'):
                    error_msg = f"Arquivo {upload.filename} deve ser DOCX"
                    async def error_generator():
                        yield f"event: error\ndata: {error_msg}\n\n"
                    return EventSourceResponse(error_generator())
                
                data = await upload.read()
                sec = parse_docx_bytes(data)
                sec["id"] = upload.filename or uuid.uuid4().hex
                docs.append(sec)
            
            if buscar_na_base:
                extra = semantic_search_rerank(
                    relatorio, top_k=top_k, rerank_top_k=rerank_top_k
                )
                docs.extend(extra)
        else:
            docs = semantic_search_rerank(
                relatorio, top_k=top_k, rerank_top_k=rerank_top_k
            )

        # Gera nomes de arquivo baseados no número do processo
        nome_base_sentenca = gerar_nome_arquivo_sentenca(numero_processo)
        nome_base_referencias = gerar_nome_arquivo_referencias(numero_processo)
        
        sent_id = f"{nome_base_sentenca}_{uuid.uuid4().hex[:8]}"
        refs_id = f"{nome_base_referencias}_{uuid.uuid4().hex[:8]}"
        
        sent_path = f"/tmp/{sent_id}.docx"
        refs_path = f"/tmp/{refs_id}.zip"

        def on_progress(msg: str):
            queue.put_nowait(msg)

        # CORREÇÃO: Função worker que roda em thread separada
        def worker():
            try:
                queue.put_nowait("🔄 Iniciando geração da sentença...")
                
                # CORREÇÃO: Criar um loop asyncio para a thread
                import asyncio
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                
                try:
                    # CORREÇÃO: Chama a função async corretamente
                    sentenca = new_loop.run_until_complete(
                        gerar_sentenca_llm(
                            relatorio=relatorio,
                            docs=docs,
                            instrucoes_usuario=instrucoes_usuario,
                            on_progress=on_progress,  # CORREÇÃO: Passa o callback
                        )
                    )
                    
                    queue.put_nowait("📝 Processando texto da sentença...")
                    
                    # Limpa e normaliza o texto da sentença
                    sentenca_limpa = decodificar_unicode(sentenca)
                    
                    queue.put_nowait("💾 Salvando sentença...")
                    
                    # Salva com número do processo
                    salvar_sentenca_como_docx(
                        relatorio=relatorio,
                        fundamentacao_dispositivo=sentenca_limpa,
                        arquivo_path=sent_path,
                        numero_processo=numero_processo
                    )
                    
                    queue.put_nowait("📁 Preparando documentos de referência...")
                    salvar_docs_referencia(docs, refs_path)
                    
                    # Monta payload com texto limpo
                    payload_data = {
                        "sentenca": sentenca_limpa,
                        "sentenca_url": f"/download/sentenca/{sent_id}.docx",
                        "referencias_url": f"/download/referencias/{refs_id}.zip",
                        "numero_processo": numero_processo
                    }
                    
                    # Serializa JSON com configurações específicas
                    payload = json.dumps(
                        payload_data, 
                        ensure_ascii=False,
                        separators=(',', ':'),
                        indent=None
                    )

                    queue.put_nowait("__COMPLETE__:" + payload)
                    
                finally:
                    new_loop.close()
                
            except Exception as e:
                # Log do erro para debug
                print(f"❌ Erro na geração da sentença: {str(e)}")
                import traceback
                traceback.print_exc()
                
                error_payload = json.dumps({
                    "error": str(e),
                    "sentenca": "",
                    "sentenca_url": "",
                    "referencias_url": "",
                    "numero_processo": numero_processo
                }, ensure_ascii=False)
                queue.put_nowait("__ERROR__:" + error_payload)

        asyncio.create_task(_run_in_thread(worker))

        async def event_generator() -> AsyncGenerator[str, None]:
            timeout_count = 0
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=180)  # 3 minutos
                    
                    if item.startswith("__COMPLETE__:"):
                        yield f"event: complete\ndata: {item.split('__COMPLETE__:',1)[1]}\n\n"
                        break
                    elif item.startswith("__ERROR__:"):
                        yield f"event: error\ndata: {item.split('__ERROR__:',1)[1]}\n\n"
                        break
                    else:
                        yield f"event: message\ndata: {item}\n\n"
                        timeout_count = 0
                        
                except asyncio.TimeoutError:
                    timeout_count += 1
                    if timeout_count > 5:  # 15 minutos total
                        yield f"event: error\ndata: Timeout na geração da sentença\n\n"
                        break
                    yield f"event: message\ndata: Gerando sentença... (aguarde)\n\n"

        return EventSourceResponse(event_generator(), ping=30)
        
    except Exception as exc:
        error_message = f"Erro na preparação: {str(exc)}"
        print(f"❌ Erro na preparação: {exc}")
        import traceback
        traceback.print_exc()
        
        async def error_generator():
            yield f"event: error\ndata: {error_message}\n\n"
        return EventSourceResponse(error_generator())


# ─────────────────────────── Endpoints Administrativos ───────────────

@app.get("/ultimo-relatorio")
async def obter_ultimo_relatorio():
    """
    Endpoint auxiliar para recuperar o último relatório processado
    (útil para debugging quando o streaming não funciona)
    """
    arquivos = glob.glob("/tmp/relatorio_*.txt")
    if not arquivos:
        raise HTTPException(status_code=404, detail="Nenhum relatório encontrado")
    
    # Pega o mais recente
    arquivo_mais_recente = max(arquivos, key=os.path.getctime)
    
    try:
        with open(arquivo_mais_recente, 'r', encoding='utf-8') as f:
            conteudo = f.read()
        return {"relatorio": conteudo}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao ler relatório: {str(e)}")


@app.post("/admin/limpar-temp")
async def limpar_arquivos_temporarios():
    """
    Endpoint para limpar arquivos temporários manualmente
    """
    patterns = ["/tmp/sentenca_*.docx", "/tmp/referencias_*.zip", "/tmp/*.pdf"]
    removed_count = 0
    
    for pattern in patterns:
        for file in glob.glob(pattern):
            try:
                os.remove(file)
                removed_count += 1
            except:
                pass
    
    return {"message": f"Removidos {removed_count} arquivos temporários"}


@app.get("/admin/status")
async def status_sistema():
    """
    Retorna status detalhado do sistema
    """
    # Conta arquivos temporários
    temp_files = 0
    patterns = ["/tmp/sentenca_*.docx", "/tmp/referencias_*.zip", "/tmp/*.pdf"]
    for pattern in patterns:
        temp_files += len(glob.glob(pattern))
    
    # Espaço em disco (simplificado)
    try:
        import shutil
        total, used, free = shutil.disk_usage("/tmp")
        disk_info = {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "free_gb": round(free / (1024**3), 2)
        }
    except:
        disk_info = {"error": "Não foi possível obter informações do disco"}
    
    return {
        "status": "online",
        "timestamp": time.time(),
        "temp_files": temp_files,
        "disk": disk_info
    }


# ─────────────────────────── Handler de Erro Global ───────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """
    Handler global para exceções não tratadas
    """
    print(f"❌ Erro não tratado: {exc}")
    return {"error": "Erro interno do servidor", "detail": str(exc)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)