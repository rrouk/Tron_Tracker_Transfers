# Используем официальный образ Python как базовый
FROM python:3.11-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл зависимостей (нужно создать requirements.txt)
# Сначала ставим зависимости, чтобы Docker мог использовать кеш
RUN pip install --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем файлы приложения
# .env копировать сюда не нужно, он будет пробрасываться через docker-compose
COPY bot.py .


# Команда для запуска приложения
# Флаг -u отключает буферизацию вывода, что полезно для логов в Docker
CMD ["python", "bot.py"]