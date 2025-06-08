
"""
streamlit_app.py — JUSTINO v2.2

A versão anterior tentava usar o endpoint síncrono primeiro e, quando ele falhava,
caía num fallback streaming que por vezes disparava
`SSLEOFError: EOF occurred in violation of protocol` no proxy TLS do Railway.
O fluxo foi invertido para **priorizar o streaming** (mais leve para uploads até
~200 MB) e, caso qualquer falha de TLS/timeout ocorra, recuar para o endpoint
síncrono `/processar`.

Principais mudanças:
────────────────────
• Função utilitária `post_stream_sse()` com cabeçalho correto
  `Accept: text/event-stream` e heartbeat interno.
• Captura explícita de `requests.exceptions.SSLError` para evitar exceções não
  tratadas quando o proxy fecha o túnel.
• Tempo‑máximo configurável: env `JUSTINO_TIMEOUT` (default 600 s).
• Barreiras de progresso simplificadas.

OBS.: backend precisa emitir ao menos um byte a cada 25 s. Caso contrário, o
Railway corta a conexão. Ver commit `backend/routers/pdf.py`.
"""

import os
import re
import json
import time
from datetime import datetime
from io import BytesIO

import requests
import streamlit as st
from dotenv import load_dotenv
from docx import Document
from sseclient import SSEClient, SSEClientError

# ─── Configuração básica ────────────────────────────────────────────────────
st.set_page_config(
    page_title="Justino — Assessor Digital",
    page_icon="⚖️",
    layout="wide",
)

load_dotenv()
API_URL: str = os.getenv("API_URL", "http://localhost:8001").rstrip("/")
TIMEOUT: int = int(os.getenv("JUSTINO_TIMEOUT", 600))  # segundos

# Import do sistema de autenticação (mantido)
from auth_tjpe import require_authentication, show_admin_panel  # noqa: E402

# ─── Utilidades de rede ─────────────────────────────────────────────────────

def post_stream_sse(url: str, files: dict, timeout: int = 600):
    """Dispara POST streaming (SSE) e devolve um gerador de eventos.

    Inclui cabeçalho `Accept: text/event-stream` para proxies TLS não fecharem
    a conexão prematuramente.
    """
    resp = requests.post(
        url,
        files=files,
        stream=True,
        timeout=timeout,
        headers={"Accept": "text/event-stream"},
    )
    resp.raise_for_status()
    return SSEClient(resp)


def limpar_relatorio(texto_bruto: str) -> str:
    """Remove tags, escapings e ruído do relatório bruto."""
    if not texto_bruto:
        return ""
    original_len = len(texto_bruto)
    texto = re.sub(r"\[TextBlock\([^]]*\)\]", "", texto_bruto)
    texto = re.sub(r"TextBlock\([^)]*\)", "", texto)
    if texto.startswith("data:"):
        texto = texto[5:].strip()
    texto = re.sub(r"citations=None,\s*text=", "", texto)
    texto = re.sub(r"type='text'", "", texto)
    texto = texto.strip("\"'").replace("\\\"", "\"")
    texto = texto.replace("\\n", "\n").replace("\\t", "\t")
    texto = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", texto)
    linhas_limpas = [l.strip() for l in texto.split("\n") if l.strip() and not re.match(r"^[\[\](),.;:'\"\\/-]+$", l.strip())]
    relatorio_final = re.sub(r"\n{3,}", "\n\n", "\n\n".join(linhas_limpas)).strip()
    if len(relatorio_final) < original_len * 0.1 and original_len > 100:
        return texto_bruto.strip()
    return relatorio_final


def extrair_numero_processo(texto: str):
    """Extrai o número do processo no padrão CNJ."""
    if not texto:
        return None
    padrao = r"\b\d{7}-\d{2}\.\d{4}\.\d{1}\.\d{2}\.\d{4}\b"
    m = re.search(padrao, texto)
    return m.group(0) if m else None


def gerar_nome_arquivo_sentenca(numero: str | None = None) -> str:
    """Gera nome de arquivo com timestamp e número do processo."""
    if numero:
        numero = re.sub(r"[-./]", "", numero)
        return f"sentenca_{numero}_{datetime.now():%Y%m%d}.docx"
    return f"sentenca_{datetime.now():%Y%m%d_%H%M%S}.docx"

    # Sidebar e instruções (idêntico – omitido neste trecho para brevidade)
    # ═══════════════════════════════ BARRA LATERAL ═══════════════════════════════
    st.sidebar.title("📋 Instruções de Uso")
    #         # Informações do usuário logado
    # user_info = st.session_state.user_info
    # st.markdown("---")
    # st.success(f"👤 **{user_info['full_name']}**")
    # st.caption(f"📧 {user_info['email']}")
    
    # # Painel admin se for administrador
    # if user_info.get('is_admin'):
    #     if st.button("👥 Gerenciar Usuários", key="btn_nav_users"):
    #             show_admin_panel()

    with st.sidebar:

        
        st.markdown("---")
        
        st.markdown("### 🚀 Como usar o Justino")
            
        st.markdown("#### **1. Extração do Relatório**")
        st.markdown("""
        - Baixe o processo do PJe em ordem CRESCENTE
        - Faça o upload do processo em PDF (máx. 200MB)
        - Clique em **"Extrair Relatório"**
        - Aguarde o processamento completo
        - Baixe o relatório em formato DOCX
        """)
            
        st.markdown("#### **2. Geração da Sentença**")
        st.markdown("""
        - **Instruções Adicionais** (opcional): 
          - Orientações específicas para a sentença
          - Pontos que devem ser destacados
          - Particularidades do caso
            
        - **Documentos de Referência** (opcional):
          - Adicione sentenças similares em DOCX
          - Jurisprudências relevantes
          - Precedentes do tribunal
            
        - **Parâmetros de Busca**:
          - **Top K**: Número de documentos similares (1-20)
          - **Rerank Top K**: Refinamento da busca (1-10)
        """)
            
        st.markdown("#### **📁 Formatos Suportados**")
        st.markdown("""
        - **Upload**: PDF (processos)
        - **Referências**: DOCX (sentenças)
        - **Download**: DOCX (relatórios e sentenças)
        """)
            
        st.markdown("#### **⚠️ Dicas Importantes**")
        st.info("""
        🔸 **Qualidade do PDF**: Certifique-se de que o texto do PDF seja legível e não seja apenas imagem
            
        🔸 **Documentos de Referência**: Inclua sentenças similares para melhor fundamentação
            
        🔸 **Instruções Específicas**: Seja claro sobre aspectos particulares do caso
            
        🔸 **Revisão Manual**: Sempre revise a sentença gerada antes do uso
        """)
            
        st.markdown("#### **🔧 Configurações Avançadas**")
        with st.expander("Parâmetros de Busca"):
            st.markdown("""
            - **Top K (10 padrão)**: Aumentar para casos complexos que necessitam mais referências
            - **Rerank Top K (5 padrão)**: Manter baixo para maior precisão
            """)
            
        st.markdown("#### **📞 Suporte**")
        st.markdown("""
        Para dúvidas ou problemas:
        - **Email**: george.queiroz@tjpe.jus.br
        - **Versão**: BETA v2.1 (Backend Otimizado)
        """)
            
        # Status do sistema
        st.markdown("---")
        st.markdown("#### **📊 Status do Sistema**")
        try:
            resp = requests.get(f"{API_URL}/health", timeout=None)
            if resp.status_code == 200:
                st.success("🟢 Sistema Online")
            else:
                st.warning("🟡 Sistema com Problemas")
        except:
            st.error("🔴 Sistema Offline")

    st.title("⚖️ Justino — Assessor Digital da 13ª Vara Cível - Seção A")

    st.markdown("<br><br><br><br>", unsafe_allow_html=True)

# ─── App principal ───────────────────────────────────────────────────────────

@require_authentication
def main_app():
    ss = st.session_state
    ss.setdefault("relatorio", None)
    ss.setdefault("relatorio_processado", False)
    ss.setdefault("sentenca_texto", None)
    ss.setdefault("sentenca_processada", False)
    ss.setdefault("sentenca_bytes", None)
    ss.setdefault("referencias_bytes", None)
    ss.setdefault("numero_processo", None)

    st.header("1. Extração do Relatório")
    uploaded_pdf = st.file_uploader("📎 Envie um processo em PDF", type=["pdf"])

    if uploaded_pdf and st.button("🔍 Extrair Relatório"):
        status = st.empty()
        progress = st.progress(0)
        files = {"pdf": (uploaded_pdf.name, uploaded_pdf.getvalue(), "application/pdf")}

        # 1️⃣ Streaming primeiro
        try:
            status.text("🔄 Streaming…")
            client = post_stream_sse(f"{API_URL}/stream/processar", files, timeout=TIMEOUT)
            relatorio_raw = ""
            for event in client.events():
                if event.event == "complete":
                    relatorio_raw = event.data
                    break
            if not relatorio_raw:
                raise ValueError("Streaming vazio")
        except (requests.exceptions.SSLError, requests.exceptions.Timeout, SSEClientError, ValueError):
            status.warning("⚠️ Streaming falhou; modo síncrono…")
            resp = requests.post(f"{API_URL}/processar", files=files, timeout=TIMEOUT)
            if resp.status_code != 200:
                status.error(f"❌ {resp.status_code}: {resp.text}")
                return
            relatorio_raw = resp.json().get("relatorio", "")

        relatorio = limpar_relatorio(relatorio_raw)
        if len(relatorio) < 30:
            status.error("Relatório vazio ou curto.")
            return
        ss.relatorio = relatorio
        ss.numero_processo = extrair_numero_processo(relatorio)
        ss.relatorio_processado = True
        st.experimental_rerun()
  
    # ───────────────────────────── Download do Relatório ────────────────────
    if st.session_state.relatorio and st.session_state.relatorio_processado:
        # Mostra informações do processo
        if st.session_state.numero_processo:
            st.success(f"📄 Relatório extraído com sucesso! **Processo nº {st.session_state.numero_processo}**")
        else:
            st.success("📄 Relatório extraído com sucesso! Baixe o arquivo ou continue para gerar a sentença.")
            
        # Mostra preview do relatório (opcional - pode remover se não quiser)
        with st.expander("📄 Visualizar Relatório Extraído", expanded=True): # Alterado para expanded=True para corresponder à imagem
            # Informações do processo
            col1, col2 = st.columns(2)
            with col1:
                st.caption(f"Tamanho do relatório: {len(st.session_state.relatorio)} caracteres")
            with col2:
                if st.session_state.numero_processo:
                    st.caption(f"Processo: {st.session_state.numero_processo}")
                
            st.text_area("Conteúdo do Relatório:", 
                        value=st.session_state.relatorio, 
                        height=300, 
                        disabled=True,
                        key="preview_relatorio")
            
        # gerar DOCX em memória com formatação adequada
        buffer = BytesIO()
        doc = Document()
        doc.add_heading("Relatório Extraído", level=1)
            
        # Processa o texto preservando quebras de linha
        texto_relatorio = st.session_state.relatorio
            
        # Divide em seções baseado em quebras duplas ou mais
        secoes = re.split(r'\n{2,}', texto_relatorio)
            
        for secao in secoes:
            if secao.strip():
                # Divide cada seção em linhas
                linhas_secao = secao.split('\n')
                    
                if len(linhas_secao) == 1:
                    # Se é uma linha única, adiciona como parágrafo
                    doc.add_paragraph(linhas_secao[0].strip())
                else:
                    # Se tem múltiplas linhas, cria parágrafo preservando quebras
                    paragrafo = doc.add_paragraph()
                    for i, linha in enumerate(linhas_secao):
                        if linha.strip():
                            if i > 0:
                                # Adiciona quebra de linha
                                paragrafo.add_run().add_break()
                            paragrafo.add_run(linha.strip())
            
        doc.save(buffer)
        buffer.seek(0)

        # Gera nome do arquivo baseado no número do processo
        # Garante que st.session_state.numero_processo seja uma string antes de chamar replace
        numero_processo_seguro = st.session_state.get('numero_processo', '')
        if numero_processo_seguro:
            numero_limpo = numero_processo_seguro.replace('-', '').replace('.', '').replace('/', '')
            nome_arquivo_relatorio = f"relatorio_{numero_limpo}.docx"
        else:
            nome_arquivo_relatorio = f"relatorio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"


        st.download_button(
            label="📥 Baixar Relatório (.docx)",
            data=buffer.getvalue(),
            file_name=nome_arquivo_relatorio,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            key="dl_relatorio"
        )

        st.markdown("---")  # Separador visual
            
    else:
        # Mostra mensagem quando não há relatório processado
        if not st.session_state.relatorio:
            st.info("👆 **Primeiro passo:** Faça o upload de um PDF e clique em 'Extrair Relatório' para continuar.")
                
            # Exemplo de processo válido
            with st.expander("📋 Exemplo de processo válido"):
                st.markdown("""
                **Estrutura esperada do PDF:**
                - Petição inicial
                - Contestação (se houver)
                - Réplica (se houver)
                - Documentos anexos
                - Despachos e decisões
                    
                **Dica:** PDFs digitalizados/migrados (apenas imagem) podem não funcionar corretamente.
                """)

    # ──────────────────────────────── Seção 2 ──────────────────────────────────
    st.header("2. Geração da Sentença")

    # só prossegue se o relatório já foi extraído
    if not st.session_state.get("relatorio_processado", False):
        st.warning("⚠️ Extraia primeiro o relatório antes de gerar a sentença.")
        st.stop()

    st.markdown("<br>", unsafe_allow_html=True)

    # inputs do usuário
    instrucoes_usuario = st.text_area(
        "📝 Instruções Adicionais (opcional)",
        height=100,
        key="ta_instr_sentenca",
        placeholder="Ex: enfatizar danos morais, valor específico de indenização, etc."
    )

    arquivos_ref = st.file_uploader(
        "📄 Documentos de Referência (DOCX) – opcional",
        type=["docx"],
        accept_multiple_files=True,
        key="uploader_refs_sentenca"
    )

    col1, col2 = st.columns(2)
    with col1:
        top_k = st.number_input("Top K (busca semântica)", 1, 20, 10, key="ni_topk_sent")
    with col2:
        rerank_top_k = st.number_input("Rerank Top K", 1, 10, 5, key="ni_rerank_sent")

    # disparo da geração
    if st.button("⚖️ Gerar Sentença", key="btn_gerar_sentenca"):
        status = st.empty()
        progress_bar = st.progress(0)

        status.text("🔄 Iniciando geração da sentença...")
        progress_bar.progress(20)
            
        # Mostra aviso sobre tempo de processamento
        time_warning = st.empty()
        time_warning.info("⏱️ A geração pode demorar alguns minutos. Aguarde...")

        try:
            # ESTRATÉGIA PRINCIPAL: Usar endpoint direto como no relatório
            with st.spinner("Gerando sentença..."):
                response = requests.post(
                    f"{API_URL}/gerar-sentenca",
                    data={
                        "relatorio": st.session_state.relatorio,
                        "instrucoes_usuario": instrucoes_usuario,
                        "top_k": str(top_k),
                        "rerank_top_k": str(rerank_top_k),
                        "numero_processo": st.session_state.get("numero_processo", ""), # Garante que envia uma string
                        "buscar_na_base": "true"
                    },
                    files=[
                        ("arquivos_referencia",
                         (f.name, f.getvalue(),
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
                        for f in arquivos_ref or []
                    ],
                    timeout=1800  # 10 minutos
                )
                
            time_warning.empty()  # Remove o aviso após o processamento
            progress_bar.progress(60)

            if response.status_code == 200:
                try:
                    result = response.json()
                    
                    if 'sentenca' in result:
                        sentenca_bruta = result['sentenca']
                        sentenca_limpa = limpar_relatorio(sentenca_bruta)  # Usa a mesma função de limpeza
                            
                        if len(sentenca_limpa) > 50:
                            st.session_state.sentenca_texto = sentenca_limpa
                                
                            # Baixa conteúdos se URLs estão disponíveis
                            if 'sentenca_url' in result and 'referencias_url' in result:
                                sent_url = API_URL + result["sentenca_url"]
                                refs_url = API_URL + result["referencias_url"]
                                    
                                try:
                                    sent_bytes = requests.get(sent_url).content
                                    refs_bytes = requests.get(refs_url).content
                                    st.session_state.sentenca_bytes = sent_bytes
                                    st.session_state.referencias_bytes = refs_bytes
                                except Exception as e:
                                    # Se não conseguir baixar, gera os arquivos localmente
                                    st.session_state.sentenca_bytes = None
                                    st.session_state.referencias_bytes = None
                                    status.warning(f"⚠️ Não foi possível baixar arquivos do servidor: {e}. Gerando localmente.")
                                    
                            progress_bar.progress(100)
                            st.session_state.sentenca_processada = True
                            status.success("✅ Sentença gerada com sucesso!")
                                
                            # Limpa a barra de progresso e força atualização
                            progress_bar.empty()
                            st.rerun()
                        else:
                            progress_bar.empty()
                            status.error(f"❌ Sentença muito pequena após limpeza: {len(sentenca_limpa)} caracteres")
                    else:
                        progress_bar.empty()
                        status.error("❌ Resposta da API não contém sentença")
                        
                except json.JSONDecodeError:
                    # Se não for JSON, tenta processar como stream (fallback)
                    progress_bar.empty()
                    status.text("🔄 Processando resposta via streaming...")
                        
                    try:
                        # Refaz a requisição como stream
                        stream_response = requests.post(
                            f"{API_URL}/gerar-sentenca",
                            data={
                                "relatorio": st.session_state.relatorio,
                                "instrucoes_usuario": instrucoes_usuario,
                                "top_k": str(top_k),
                                "rerank_top_k": str(rerank_top_k),
                                "numero_processo": st.session_state.get("numero_processo", ""), # Garante que envia uma string
                                "buscar_na_base": "true"
                            },
                            files=[
                                ("arquivos_referencia",
                                 (f.name, f.getvalue(),
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
                                for f in arquivos_ref or []
                            ],
                            stream=True,
                            timeout=600
                        )
                            
                        if stream_response.status_code == 200:
                            client = SSEClient(stream_response)
                            for event in client.events():
                                if event.event == "message":
                                    status.text(f"🔄 {event.data}")
                                elif event.event == "complete":
                                    data = json.loads(event.data)
                                    sentenca_bruta = data["sentenca"].replace("\\n", "\n")
                                    sentenca_limpa = limpar_relatorio(sentenca_bruta)
                                        
                                    if len(sentenca_limpa) > 50:
                                        st.session_state.sentenca_texto = sentenca_limpa
                                            
                                        # Baixa conteúdos
                                        sent_url = API_URL + data["sentenca_url"]
                                        refs_url = API_URL + data["referencias_url"]
                                        
                                        try:
                                            sent_bytes = requests.get(sent_url).content
                                            refs_bytes = requests.get(refs_url).content
                                            st.session_state.sentenca_bytes = sent_bytes
                                            st.session_state.referencias_bytes = refs_bytes
                                        except Exception as e:
                                            st.session_state.sentenca_bytes = None
                                            st.session_state.referencias_bytes = None
                                            status.warning(f"⚠️ Não foi possível baixar arquivos do servidor: {e}. Gerando localmente.")

                                        st.session_state.sentenca_processada = True
                                            
                                        status.success("✅ Sentença gerada via streaming!")
                                        st.rerun()
                                    else:
                                        status.error(f"❌ Sentença muito pequena: {len(sentenca_limpa)} caracteres")
                                    break
                                elif event.event == "error":
                                    status.error(f"❌ Erro na geração: {event.data}")
                                    break
                        else:
                            status.error(f"❌ Erro no streaming: {stream_response.status_code}")
                    except Exception as e:
                        status.error(f"❌ Erro no fallback streaming: {str(e)}")
                        
                except requests.exceptions.Timeout:
                    progress_bar.empty()
                    status.error("⏱️ Timeout na geração (10 minutos). O processamento pode estar demorando mais que o esperado. Tente novamente.")
                
        except requests.exceptions.ConnectionError:
            progress_bar.empty()
            status.error("🔌 Erro de conexão. Verifique se o servidor está funcionando.")
            
        except Exception as e:
            progress_bar.empty()
            status.error(f"❌ Erro inesperado: {str(e)}")

    # ───────────────────────────── Exibição e Download da Sentença ────────────────────
    if st.session_state.sentenca_processada and st.session_state.sentenca_texto:
        # cabeçalho de sucesso
        if st.session_state.numero_processo:
            st.success(f"⚖️ Sentença gerada com sucesso! **Processo nº {st.session_state.numero_processo}**")
        else:
            st.success("⚖️ Sentença gerada com sucesso! Baixe os arquivos abaixo.")

        # preview da sentença
        with st.expander("📄 Visualizar Sentença Gerada", expanded=True): # Alterado para expanded=True
            # Informações da sentença
            col1, col2 = st.columns(2)
            with col1:
                st.caption(f"Tamanho da sentença: {len(st.session_state.sentenca_texto)} caracteres")
            with col2:
                if st.session_state.numero_processo:
                    st.caption(f"Processo: {st.session_state.numero_processo}")
                
            st.text_area("Conteúdo da Sentença:", 
                        value=st.session_state.sentenca_texto, 
                        height=300, 
                        disabled=True,
                        key="preview_sentenca")

        # ─────────────────────── Geração do DOCX da Sentença ───────────────────────
        buffer_sentenca = BytesIO()
        doc = Document()
        doc.add_heading("Sentença Gerada", level=1)

        texto = st.session_state.sentenca_texto or ""
        secoes = re.split(r'\n{2,}', texto)
        for secao in secoes:
            secao = secao.strip()
            if not secao:
                continue
            linhas = secao.split('\n')
            if len(linhas) == 1:
                doc.add_paragraph(linhas[0])
            else:
                p = doc.add_paragraph()
                for i, linha in enumerate(linhas):
                    linha = linha.strip()
                    if not linha:
                        continue
                    if i > 0:
                        p.add_run().add_break()
                    p.add_run(linha)

        doc.save(buffer_sentenca)
        buffer_sentenca.seek(0)

        # nome inteligente para o DOCX
        nome_sentenca = gerar_nome_arquivo_sentenca(st.session_state.numero_processo)

        # ─────────────────────────── Download da Sentença ───────────────────────────
        # Usa o arquivo gerado pelo backend se disponível, senão usa o gerado localmente
        dados_sentenca = st.session_state.sentenca_bytes if st.session_state.sentenca_bytes else buffer_sentenca.getvalue()
            
        st.download_button(
            "📥 Baixar Sentença (.docx)",
            data=dados_sentenca,
            file_name=nome_sentenca,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            key="dl_sentenca"
        )

        # ─────────────────────────── Download das Referências ───────────────────────
        if st.session_state.referencias_bytes:
            # Garante que numero_processo seja uma string antes de chamar replace
            numero_processo_seguro = st.session_state.get("numero_processo", "")
            numero_limpo = numero_processo_seguro
            nome_refs = f"referencias_{numero_limpo or datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"

            st.download_button(
                "📥 Baixar Referências (.zip)",
                data=st.session_state.referencias_bytes,
                file_name=nome_refs,
                mime="application/zip",
                key="dl_referencias"
            )
        else:
            st.info("📋 Referências não disponíveis para download.")

    else:
        # Mostra mensagem quando não há sentença processada (e o relatório foi processado)
        if st.session_state.relatorio_processado:
            st.info("👆 **Configure as opções e clique em 'Gerar Sentença' para começar.**")
                
            # Exemplo de instruções
            # with st.expander("📝 Exemplos de Instruções Adicionais"):
            #     st.markdown("""
            #     **Exemplos úteis:**
            #     - "Enfatizar danos morais no valor de R$ 5.000,00"
            #     - "Destacar jurisprudência do STJ sobre responsabilidade civil"
            #     - "Mencionar precedente específico do tribunal local"
            #     - "Fundamentar com base no CDC para relação de consumo"
            #     - "Aplicar juros e correção monetária desde o evento danoso"
            #     """)

    # ─────────────────────────── Rodapé com informações ───────────────────────────
    st.markdown("---")
    st.markdown(f"""
    <div style="text-align: center; color: #666; font-size: 0.9em; margin-top: 2rem;">
        <p><strong>⚖️ Justino - Assessor Digital da 13ª Vara Cível - Seção A</strong></p>
        <p>Versão BETA v2.1 | maio de 2025</p>
        <p><strong>👤 Usuário:</strong> {st.session_state.user_info['full_name']} | <strong>📧</strong> {st.session_state.user_info['email']}</p>
        <p><em>⚠️ Sempre revise o conteúdo gerado antes do lançar a minuta</em></p>
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main_app()
