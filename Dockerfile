FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput
EXPOSE 8000
# Self-setup for hosts without a shell (Render free plan). createsuperuser reads DJANGO_SUPERUSER_*
# and fails harmlessly when they're unset or the user already exists.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py seed_demo && (python manage.py createsuperuser --noinput || true) && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --timeout 60"]
