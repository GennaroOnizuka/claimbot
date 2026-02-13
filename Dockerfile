# CLAIMBOT: Python + Node (per claim PROXY). Esegue check_cash.py in loop.
FROM python:3.12-slim

# Node 20 per claim-proxy
RUN apt-get update && apt-get install -y --no-install-recommends curl \
  && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
  && apt-get install -y nodejs \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Dipendenze Node (claim-proxy)
COPY claim-proxy/package.json claim-proxy/
RUN cd claim-proxy && npm install --omit=dev

# Codice
COPY check_cash.py claims.py executor.py .
COPY claim-proxy/claim-proxy.mjs claim-proxy/

# .env va impostato su Render (Environment) o montato a runtime
CMD ["python3", "check_cash.py"]
