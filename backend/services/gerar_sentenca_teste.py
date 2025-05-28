import os
import uuid
import asyncio
import json
import re
from pathlib import Path as FSPath
from typing import List, Optional, AsyncGenerator

from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from preprocessing.process_report_pipeline import Config, generate as gerar_relatorio
from services.retrieval_rerank import recuperar_documentos_similares as semantic_search_rerank
from services.llm import gerar_sentenca_llm
from services.docx_parser import parse_docx_bytes

app = FastAPI(title="RAG TJPE API")


# ─────────────────────────── Utilitários ───────────────────────────

def extrair_numero_processo(texto: str) -> Optional[str]:
    """
    Extrai o número do processo do texto do relatório
    """
    # Padrões comuns de número de processo
    padroes = [
        r'\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}',  # Formato: 0000000-00.0000.0.00.0000
        r'\d{10}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}', # Formato: 0000000000-00.0000.0.00.0000
        r'\d{4}\.\d{2}\.\d{6}-\d{1}',               # Formato: 0000.00.000000-0
        r'(?:processo|autos)(?:\s+n[º°]?\.?\s*|\s+)(\d+[-\.\d]+)',  # "processo nº" ou "autos"
    ]
    
    for padrao in padroes:
        matches = re.findall(padrao, texto, re.IGNORECASE)
        if matches:
            return matches[0] if isinstance(matches[0], str) else matches[0]
    
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


# ─────────────────────────── Health‐check ───────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ───────────────────────── Rotas REST (síncronas) ──────────────────────

class ExtrairRelatorioResp(BaseModel):
    relatorio: str
    numero_processo: Optional[str] = None


@app.post("/processar", response_model=ExtrairRelatorioResp)
async def processar_pdf(pdf: UploadFile = File(...)):
    tmp_id = uuid.uuid4().hex
    tmp_path = f"/tmp/{tmp_id}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(await pdf.read())

    cfg = Config()
    texto = gerar_relatorio(FSPath(tmp_path), cfg)
    
    # Extrai número do processo se presente
    numero_processo = extrair_numero_processo(texto)
    
    return ExtrairRelatorioResp(
        relatorio=texto,
        numero_processo=numero_processo
    )


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
    # 1) Monta lista inicial com arquivos enviados, se houver
    docs: List[dict] = []
    if arquivos_referencia:
        for upload in arquivos_referencia:
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
        sentenca=sentenca,
        sentenca_url=f"/download/sentenca/{sent_id}.docx",
        referencias_url=f"/download/referencias/{refs_id}.zip",
        numero_processo=numero_processo
    )


# ───────────────────────────── Downloads ──────────────────────────────

@app.get("/download/sentenca/{file_id}.docx")
def download_sentenca(file_id: str):
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
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func)


@app.post("/stream/processar")
async def stream_processar_pdf(pdf: UploadFile = File(...)) -> EventSourceResponse:
    tmp_id = uuid.uuid4().hex
    tmp_path = f"/tmp/{tmp_id}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(await pdf.read())

    cfg = Config()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def on_progress(msg: str):
        queue.put_nowait(msg)

    def worker():
        report = gerar_relatorio(FSPath(tmp_path), cfg, on_progress=on_progress)
        queue.put_nowait("__COMPLETE__:" + report)

    asyncio.create_task(_run_in_thread(worker))

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            item = await queue.get()
            if item.startswith("__COMPLETE__:"):
                yield f"event: complete\ndata: {item.split('__COMPLETE__:',1)[1]}\n\n"
                break
            else:
                yield f"event: message\ndata: {item}\n\n"

    return EventSourceResponse(event_generator(), ping=10)


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
    queue: asyncio.Queue[str] = asyncio.Queue()
    
    # Monta lista de docs igual ao endpoint REST
    docs: List[dict] = []
    if arquivos_referencia:
        for upload in arquivos_referencia:
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

    async def worker():
        try:
            sentenca = await gerar_sentenca_llm(
                relatorio=relatorio,
                docs=docs,
                instrucoes_usuario=instrucoes_usuario,
                # Removido on_progress da chamada pois causa erro
            )
            
            # Salva com número do processo
            salvar_sentenca_como_docx(
                relatorio=relatorio,
                fundamentacao_dispositivo=sentenca,
                arquivo_path=sent_path,
                numero_processo=numero_processo
            )
            salvar_docs_referencia(docs, refs_path)
            
            payload = json.dumps({
                "sentenca": sentenca,
                "sentenca_url": f"/download/sentenca/{sent_id}.docx",
                "referencias_url": f"/download/referencias/{refs_id}.zip",
                "numero_processo": numero_processo
            })
            queue.put_nowait("__COMPLETE__:" + payload)
            
        except Exception as e:
            # Log do erro para debug
            print(f"Erro na geração da sentença: {str(e)}")
            error_payload = json.dumps({
                "error": str(e),
                "sentenca": "",
                "sentenca_url": "",
                "referencias_url": "",
                "numero_processo": numero_processo
            })
            queue.put_nowait("__ERROR__:" + error_payload)

    asyncio.create_task(worker())

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            item = await queue.get()
            if item.startswith("__COMPLETE__:"):
                yield f"event: complete\ndata: {item.split('__COMPLETE__:',1)[1]}\n\n"
                break
            elif item.startswith("__ERROR__:"):
                yield f"event: error\ndata: {item.split('__ERROR__:',1)[1]}\n\n"
                break
            else:
                yield f"event: message\ndata: {item}\n\n"

    return EventSourceResponse(event_generator(), ping=10)


# ─────────────────────────── Endpoint para obter último relatório ───────────────

@app.get("/ultimo-relatorio")
async def obter_ultimo_relatorio():
    """
    Endpoint auxiliar para recuperar o último relatório processado
    (útil para debugging quando o streaming não funciona)
    """
    # Busca pelo arquivo de relatório mais recente em /tmp
    import glob
    from pathlib import Path
    
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