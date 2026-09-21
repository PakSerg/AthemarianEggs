FROM python:3.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libzbar0 \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
    && apt-get -y install --no-install-recommends libpq-dev gcc netcat-openbsd curl ca-certificates cron \
    && rm -rf /var/lib/apt/lists/*

# Точка Банк (Cyclops pre/prod-слой) отдаёт сертификат, подписанный корневым
# «Минцифры России» / Russian Trusted Root CA — его нет ни в стандартном
# ca-certificates, ни в бандле certifi, которым по умолчанию пользуется
# requests. Добавляем цепочку в системное хранилище и явно указываем
# requests/urllib3 использовать его через REQUESTS_CA_BUNDLE.
RUN curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
        -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && curl -fsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
        -o /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt \
    && update-ca-certificates

ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

RUN python manage.py collectstatic --noinput

EXPOSE 8000
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]