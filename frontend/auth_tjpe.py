import streamlit as st
import time
import hashlib
import hmac
import sqlite3
import os
import smtplib
import secrets
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict

class AuthTJPE:
    def __init__(self, db_path: str = "/app/data/users_tjpe.db"):
        self.db_path = db_path
        self.session_timeout = 8 * 60 * 60  # 8 horas em segundos
        self.code_timeout = 10 * 60  # 10 minutos para código de verificação
        self.init_database()
        self.create_admin_user()
    
    def init_database(self):
        """Inicializa banco de dados de usuários"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Tabela de usuários aprovados
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS approved_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT 'admin',
                is_active BOOLEAN DEFAULT 1,
                last_login TIMESTAMP
            )
        ''')
        
        # Tabela de códigos de verificação
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS verification_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT 0
            )
        ''')
        
        # Tabela de sessões ativas
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                session_token TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                ip_address TEXT,
                user_agent TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def create_admin_user(self):
        """Cria usuário admin se não existir"""
        admin_email = "george.queiroz@tjpe.jus.br"  # Seu e-mail do TJPE
        
        if not self.is_user_approved(admin_email):
            self.add_approved_user(
                email=admin_email,
                full_name="George Queiroz - Admin",
                created_by="system"
            )
            print(f"✅ Usuário admin criado: {admin_email}")
    
    def is_tjpe_email(self, email: str) -> bool:
        """Verifica se é e-mail do TJPE"""
        return email.lower().endswith("@tjpe.jus.br")
    
    def is_user_approved(self, email: str) -> bool:
        """Verifica se usuário está aprovado"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT 1 FROM approved_users 
            WHERE email = ? AND is_active = 1
        ''', (email.lower(),))
        
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    
    def add_approved_user(self, email: str, full_name: str, created_by: str = "admin") -> bool:
        """Adiciona usuário à lista de aprovados (apenas admin)"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO approved_users (email, full_name, created_by)
                VALUES (?, ?, ?)
            ''', (email.lower(), full_name, created_by))
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            return False  # Usuário já existe
    
    def generate_verification_code(self) -> str:
        """Gera código de 6 dígitos"""
        # Para testes - código fixo se DEBUG_MODE estiver ativo
        if os.getenv("DEBUG_MODE", "false").lower() == "true":
            return "123456"
        
        return f"{secrets.randbelow(900000) + 100000:06d}"
    
    def send_verification_email(self, email: str, code: str, user_name: str) -> bool:
        """Envia código por e-mail (simulado para MVP)"""
        try:
            # CONFIGURAÇÃO DE E-MAIL (você precisa configurar)
            smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_user = os.getenv("SMTP_USER", "")  # Seu e-mail
            smtp_pass = os.getenv("SMTP_PASSWORD", "")  # Sua senha de app
            
            if not smtp_user or not smtp_pass:
                # Para MVP, apenas loga o código (REMOVER EM PRODUÇÃO)
                print(f"🔐 CÓDIGO DE VERIFICAÇÃO PARA {email}: {code}")
                return True
            
            # Criar mensagem
            msg = MIMEMultipart()
            msg['From'] = smtp_user
            msg['To'] = email
            msg['Subject'] = "Código de Acesso - Justino Digital"
            
            body = f"""
            Olá {user_name},
            
            Seu código de acesso ao sistema Justino Digital é:
            
            {code}
            
            Este código é válido por 10 minutos.
            
            Se você não solicitou este acesso, ignore este e-mail.
            
            Atenciosamente,
            Sistema Justino Digital
            13ª Vara Cível do TJPE
            """
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Enviar e-mail
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
            server.quit()
            
            return True
            
        except Exception as e:
            print(f"❌ Erro ao enviar e-mail: {e}")
            # Para MVP, sempre simula sucesso
            print(f"🔐 CÓDIGO DE VERIFICAÇÃO PARA {email}: {code}")
            return True
    
    def create_verification_code(self, email: str) -> bool:
        """Cria código de verificação"""
        if not self.is_tjpe_email(email):
            return False
        
        if not self.is_user_approved(email):
            return False
        
        # Limpar códigos expirados
        self.cleanup_expired_codes()
        
        # Gerar novo código
        code = self.generate_verification_code()
        expires_at = datetime.now() + timedelta(seconds=self.code_timeout)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Invalidar códigos anteriores
        cursor.execute('''
            UPDATE verification_codes 
            SET used = 1 
            WHERE email = ? AND used = 0
        ''', (email.lower(),))
        
        # Inserir novo código
        cursor.execute('''
            INSERT INTO verification_codes (email, code, expires_at)
            VALUES (?, ?, ?)
        ''', (email.lower(), code, expires_at))
        
        conn.commit()
        conn.close()
        
        # Buscar nome do usuário
        user_info = self.get_user_info(email)
        user_name = user_info['full_name'] if user_info else email
        
        # Enviar por e-mail
        return self.send_verification_email(email, code, user_name)
    
    def verify_code(self, email: str, code: str) -> bool:
        """Verifica código de acesso"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id FROM verification_codes 
            WHERE email = ? AND code = ? AND used = 0 AND expires_at > ?
        ''', (email.lower(), code, datetime.now()))
        
        result = cursor.fetchone()
        
        if result:
            # Marcar código como usado
            cursor.execute('''
                UPDATE verification_codes 
                SET used = 1 
                WHERE id = ?
            ''', (result[0],))
            
            conn.commit()
            conn.close()
            return True
        
        conn.close()
        return False
    
    def create_session(self, email: str) -> str:
        """Cria sessão após verificação"""
        session_token = secrets.token_urlsafe(32)
        expires_at = datetime.now() + timedelta(seconds=self.session_timeout)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Limpar sessões expiradas
        cursor.execute('DELETE FROM active_sessions WHERE expires_at < ?', (datetime.now(),))
        
        # Criar nova sessão
        cursor.execute('''
            INSERT INTO active_sessions (email, session_token, expires_at)
            VALUES (?, ?, ?)
        ''', (email.lower(), session_token, expires_at))
        
        # Atualizar último login
        cursor.execute('''
            UPDATE approved_users 
            SET last_login = CURRENT_TIMESTAMP 
            WHERE email = ?
        ''', (email.lower(),))
        
        conn.commit()
        conn.close()
        
        return session_token
    
    def validate_session(self, session_token: str) -> Optional[str]:
        """Valida sessão ativa"""
        if not session_token:
            return None
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT email FROM active_sessions 
            WHERE session_token = ? AND expires_at > ?
        ''', (session_token, datetime.now()))
        
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result else None
    
    def logout(self, session_token: str):
        """Remove sessão"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM active_sessions WHERE session_token = ?', (session_token,))
        
        conn.commit()
        conn.close()
    
    def get_user_info(self, email: str) -> Optional[Dict]:
        """Retorna informações do usuário"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT email, full_name, created_at, last_login, created_by
            FROM approved_users WHERE email = ?
        ''', (email.lower(),))
        
        user = cursor.fetchone()
        conn.close()
        
        if user:
            return {
                "email": user[0],
                "full_name": user[1],
                "created_at": user[2],
                "last_login": user[3],
                "created_by": user[4],
                "is_admin": user[0] == "george.queiroz@tjpe.jus.br"
            }
        return None
    
    def cleanup_expired_codes(self):
        """Remove códigos expirados"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM verification_codes WHERE expires_at < ?', (datetime.now(),))
        
        conn.commit()
        conn.close()
    
    def list_approved_users(self) -> list:
        """Lista usuários aprovados (apenas admin)"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT email, full_name, created_at, last_login, is_active
            FROM approved_users ORDER BY created_at DESC
        ''')
        
        users = cursor.fetchall()
        conn.close()
        
        return [
            {
                "email": user[0],
                "full_name": user[1],
                "created_at": user[2],
                "last_login": user[3],
                "is_active": bool(user[4])
            }
            for user in users
        ]

# ────────────────────────────────────────────────
# Funções para integração com Streamlit
# ────────────────────────────────────────────────

def init_auth_state():
    """Inicializa estado de autenticação"""
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "user_info" not in st.session_state:
        st.session_state.user_info = None
    if "auth_manager" not in st.session_state:
        st.session_state.auth_manager = AuthTJPE()
    if "login_step" not in st.session_state:
        st.session_state.login_step = "email"  # email -> code -> authenticated

def check_authentication() -> bool:
    """Verifica se usuário está autenticado"""
    init_auth_state()
    
    # Verificar token de sessão
    session_token = st.session_state.get("session_token")
    
    if session_token:
        email = st.session_state.auth_manager.validate_session(session_token)
        if email:
            st.session_state.authenticated = True
            st.session_state.user_info = st.session_state.auth_manager.get_user_info(email)
            return True
    
    st.session_state.authenticated = False
    st.session_state.user_info = None
    return False

def show_login_page():
    """Login em 2 etapas dentro do card, com fluxo correto de sessão + ACESSO DIRETO."""
    auth = st.session_state.auth_manager
    
    # CSS para styling
    st.markdown("""
    <style>
    .login-container {
        display: flex;
        justify-content: center;
        align-items: center;
        min-height: 70vh;
        padding: 2rem;
    }
    .login-box {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 20px;
        padding: 3rem;
        box-shadow: 0 20px 40px rgba(0,0,0,0.3);
        color: white;
        text-align: center;
        min-width: 400px;
    }
    .login-box h1 {
        margin-bottom: 0.5rem;
        font-size: 2.5rem;
        font-weight: 700;
    }
    .login-box p {
        margin-bottom: 2rem;
        opacity: 0.9;
        font-size: 1.1rem;
    }
    .direct-access-warning {
        background: rgba(255, 193, 7, 0.2);
        border-left: 4px solid #ffc107;
        padding: 1rem;
        margin: 1rem 0;
        border-radius: 0 10px 10px 0;
        color: #fff3cd;
    }
    .login-footer {
        text-align: center;
        margin-top: 3rem;
        color: #64748b;
        font-size: 0.9rem;
    }
    </style>
    """, unsafe_allow_html=True)

    placeholder = st.empty()

    # Inicializa passo de login
    if "login_step" not in st.session_state:
        st.session_state.login_step = "email"

    # ETAPA 1: Captura e-mail e envia código (COM ACESSO DIRETO)
    if st.session_state.login_step == "email":
        with placeholder.form("email_form"):
            st.markdown('<div class="login-container"><div class="login-box">', unsafe_allow_html=True)
            st.markdown("<h1>⚖️ Justino Digital</h1>", unsafe_allow_html=True)
            st.markdown("<p>13ª Vara Cível – TJPE<br>Sistema Restrito</p>", unsafe_allow_html=True)

            email_input = st.text_input(
                "E-mail institucional",
                placeholder="seu.nome@tjpe.jus.br",
                key="login_email"
            )
            
            # Botões organizados em colunas
            col1, col2 = st.columns([1, 1])
            
            with col1:
                send_btn = st.form_submit_button("➡️ Enviar código", type="primary")
            
            with col2:
                # BOTÃO DE ACESSO DIRETO PARA ADMIN
                if email_input == "george.queiroz@tjpe.jus.br":
                    direct_access_btn = st.form_submit_button("🔧 Acesso Direto", type="secondary")
                else:
                    direct_access_btn = False
            
            # Aviso sobre acesso direto (só aparece para admin)
            if email_input == "george.queiroz@tjpe.jus.br":
                st.markdown("""
                <div class="direct-access-warning">
                    <strong>⚠️ Modo Desenvolvimento:</strong><br>
                    Acesso direto disponível para administrador durante testes.
                </div>
                """, unsafe_allow_html=True)
            
            st.markdown("</div></div>", unsafe_allow_html=True)

        # PROCESSAMENTO DO ACESSO DIRETO
        if direct_access_btn and email_input == "george.queiroz@tjpe.jus.br":
            # Verifica se é usuário aprovado
            if auth.is_user_approved(email_input):
                # Cria sessão diretamente
                token = auth.create_session(email_input)
                
                # Marca como autenticado
                st.session_state.session_token = token
                st.session_state.authenticated = True
                st.session_state.user_info = auth.get_user_info(email_input)
                
                placeholder.empty()
                st.success("✅ Acesso direto autorizado! Bem-vindo, Admin.")
                time.sleep(1)
                st.rerun()
            else:
                st.error("❌ Usuário não autorizado no sistema.")

        # PROCESSAMENTO DO ENVIO DE CÓDIGO (método normal)
        if send_btn:
            if email_input and email_input.endswith("@tjpe.jus.br"):
                if auth.is_user_approved(email_input):
                    # Salva o e-mail para a próxima etapa
                    st.session_state.verification_email = email_input
                    # Dispara geração/envio do código
                    if auth.create_verification_code(email_input):
                        placeholder.empty()
                        st.success(f"📧 Código enviado para {email_input}")
                        
                        # Mostrar código na tela em modo debug
                        if os.getenv("DEBUG_MODE", "false").lower() == "true":
                            st.info("🔑 **Modo Debug:** Código padrão é `123456`")
                        
                        st.session_state.login_step = "code"
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error("❌ Erro ao enviar código. Tente novamente.")
                else:
                    st.error("❌ E-mail não autorizado. Contacte o administrador.")
            else:
                st.error("❌ Use um e-mail institucional válido (@tjpe.jus.br).")
        return

    # ETAPA 2: Verifica código e cria sessão
    if st.session_state.login_step == "code":
        with placeholder.form("code_form"):
            st.markdown('<div class="login-container"><div class="login-box">', unsafe_allow_html=True)
            st.markdown("<h1>🔐 Confirme seu acesso</h1>", unsafe_allow_html=True)
            st.markdown(f"<p>Digite o código enviado para:<br><strong>{st.session_state.get('verification_email', '')}</strong></p>", unsafe_allow_html=True)

            code_input = st.text_input(
                "Código de autenticação",
                placeholder="123456",
                key="login_code",
                max_chars=6
            )
            
            col1, col2 = st.columns([1, 1])
            
            with col1:
                validate_btn = st.form_submit_button("🔓 Validar código", type="primary")
            
            with col2:
                back_btn = st.form_submit_button("← Voltar", type="secondary")
            
            # Dica em modo debug
            if os.getenv("DEBUG_MODE", "false").lower() == "true":
                st.info("🔑 **Modo Debug:** Use código `123456`")
            
            st.markdown("</div></div>", unsafe_allow_html=True)

        # Voltar para tela de e-mail
        if back_btn:
            st.session_state.login_step = "email"
            placeholder.empty()
            st.rerun()

        # Validar código
        if validate_btn:
            email = st.session_state.get("verification_email", "")
            code = st.session_state.get("login_code", "")

            if auth.verify_code(email, code):
                # Cria sessão no backend
                token = auth.create_session(email)

                # Marca como autenticado
                st.session_state.session_token = token
                st.session_state.authenticated = True
                st.session_state.user_info = auth.get_user_info(email)

                placeholder.empty()
                st.success(f"✅ Bem-vindo, {st.session_state.user_info['full_name']}!")
                time.sleep(1)
                st.rerun()
            else:
                st.error("❌ Código inválido ou expirado. Tente novamente.")
        return

    # Rodapé
    st.markdown("""
    <div class="login-footer">
      🏛️ Sistema de Geração Automática de Sentenças • Suporte:
      <a href="mailto:george.queiroz@tjpe.jus.br" style="color:#94a3b8;">
        george.queiroz@tjpe.jus.br
      </a>
    </div>
    """, unsafe_allow_html=True)


def show_user_menu():
    """Exibe menu do usuário logado na sidebar"""
    if not st.session_state.authenticated:
        return
    
    user_info = st.session_state.user_info
    
    with st.sidebar:
        st.markdown("---")
        st.markdown(f"**👤 {user_info['full_name']}**")
        st.markdown(f"*{user_info['email']}*")
        
        if user_info.get('is_admin'):
            st.markdown("🛡️ *Administrador*")
        
        # Informações da sessão
        st.markdown(f"🕐 Conectado às {datetime.now().strftime('%H:%M')}")
        
        # Menu admin
        if user_info.get('is_admin'):
            if st.button("👥 Gerenciar Usuários", key="btn_nav_users_reports"):
                show_admin_panel()
        
        if st.button("🚪 Sair"):
            # Logout
            if "session_token" in st.session_state:
                st.session_state.auth_manager.logout(st.session_state.session_token)
            
            # Limpar estado
            for key in list(st.session_state.keys()):
                if key.startswith(('authenticated', 'user_info', 'session_token', 'verification_', 'login_step')):
                    del st.session_state[key]
            
            st.rerun()

def show_admin_panel():
    """Painel administrativo para gerenciar usuários"""
    if not st.session_state.user_info.get('is_admin'):
        return
    
    st.markdown("### 👥 Painel Administrativo")
    
    # Adicionar novo usuário
    with st.expander("➕ Adicionar Usuário Autorizado"):
        with st.form("add_user_form"):
            new_email = st.text_input("E-mail do TJPE", placeholder="nome.sobrenome@tjpe.jus.br")
            new_name = st.text_input("Nome Completo", placeholder="Nome Sobrenome")
            
            if st.form_submit_button("✅ Autorizar Usuário"):
                auth_manager = st.session_state.auth_manager
                
                if not auth_manager.is_tjpe_email(new_email):
                    st.error("❌ Apenas e-mails @tjpe.jus.br")
                elif auth_manager.add_approved_user(new_email, new_name):
                    st.success(f"✅ Usuário {new_email} adicionado!")
                else:
                    st.warning("⚠️ Usuário já existe")
    
    # Listar usuários
    st.markdown("#### 📋 Usuários Autorizados")
    users = st.session_state.auth_manager.list_approved_users()
    
    for user in users:
        col1, col2, col3 = st.columns([2, 2, 1])
        
        with col1:
            st.write(f"**{user['full_name']}**")
            st.caption(user['email'])
        
        with col2:
            if user['last_login']:
                st.write(f"🕐 {user['last_login']}")
            else:
                st.write("🔸 Nunca logou")
        
        with col3:
            status = "🟢 Ativo" if user['is_active'] else "🔴 Inativo"
            st.write(status)

def require_authentication(app_function):
    """Decorator que protege a aplicação"""
    def wrapper():
        init_auth_state()
        
        if not check_authentication():
            show_login_page()
            return
        
        # Mostrar menu do usuário
        show_user_menu()
        
        # Executar aplicação principal
        app_function()
    
    return wrapper