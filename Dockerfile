# 인터넷 서버(허깅페이스 Spaces 등)에 올릴 때 쓰는 설정
FROM python:3.12-slim

# 스캔본 PDF 글자 인식(한국어·영어)
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# 허깅페이스는 사용자 1000번으로 실행
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 PAPER_AI_SERVER=1 PORT=7860
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .
# 의미 검색 모델을 미리 받아 둠 (첫 사용자가 기다리지 않게)
RUN mkdir -p data && python -c "from core import llm; llm._embed_builtin(['준비'])"

EXPOSE 7860
CMD ["python", "server.py"]
