FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home ads
COPY internal_ads ./internal_ads
RUN mkdir /app/data && chown ads:ads /app/data
USER ads
EXPOSE 8787
CMD ["python", "-m", "internal_ads.server"]
