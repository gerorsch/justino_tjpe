# backend/Dockerfile

# 1. Imagem base
FROM python:3.10-slim


# 2. Variáveis de ambiente
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Instalar dependências do sistema (OCR, PDF)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# 4. Definir diretório de trabalho
WORKDIR /app

# 5. Copiar e instalar dependências Python
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# 6. Copiar todo o código-fonte
COPY . ./

# 7. Comando padrão para iniciar o FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
