#!/usr/bin/env bash
set -e

wait_for_service() {
    local host=$1
    local port=$2
    local service=$3

    echo "Waiting for $service..."
    while ! nc -z $host $port; do
        sleep 1
    done
    echo "$service is ready!"
}

wait_for_service db 5432 "PostgreSQL"
wait_for_service redis 6379 "Redis"

# Отдельной сборки фронтенда нет: лендинг и кабинет — обычные шаблоны Django
# со статикой из static/. Vite из проекта-образца не переносился — вёрстка
# здесь своя, а собирать нечего.

echo "Applying migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 120