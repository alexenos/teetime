FROM python:3.11-slim

WORKDIR /app

# Rebuilt once a week, and otherwise reused from the previous image (#424).
# cloudbuild.yaml builds with --cache-from the last pushed image, so this layer,
# and every later layer whose inputs are unchanged, keeps its digest and is
# stored once in Artifact Registry instead of once per deploy. CHROMIUM_REFRESH
# is the ISO week, so the first deploy of a new week re-runs apt-get and picks up
# Chromium and security updates; later deploys that week reuse it. A newer
# python:3.11-slim base also rebuilds it, since every layer depends on the base.
ARG CHROMIUM_REFRESH=unset

# Install Chrome/Chromium and dependencies for headless Selenium
RUN echo "Chromium refresh: ${CHROMIUM_REFRESH}" && apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    tzdata \
    chromium \
    chromium-driver \
    # Dependencies for headless Chrome
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Set ChromeDriver path for Selenium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV CHROME_BIN=/usr/bin/chromium

RUN pip install poetry==2.4.3

COPY pyproject.toml poetry.lock* ./

RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root --only main

COPY . .

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
