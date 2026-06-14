FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # spaCy-Modelle fuer DE + EN (Presidio NER). DE fuer deutsche Anfragen.
    && python -m spacy download de_core_news_lg \
    && python -m spacy download en_core_web_lg

COPY app.py .

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
