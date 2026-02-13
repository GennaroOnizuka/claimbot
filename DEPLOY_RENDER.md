# Deploy CLAIMBOT su Render (via GitHub)

Guida per mettere il bot su GitHub e farlo girare 24/7 come **Background Worker** su Render.

---

## 1. Da terminale: prepara e pusha su GitHub

### 1.1 Inizializza Git (se non l’hai già fatto)

```bash
cd /Users/matteogianino/Desktop/CLAIMBOT
git init
```

### 1.2 Verifica che .env non venga tracciato

```bash
git status
```

Non deve comparire `.env` tra i file da aggiungere. Se compare, controlla che `.gitignore` contenga `.env`.

### 1.3 Aggiungi i file e fai il primo commit

```bash
git add .
git status
git commit -m "CLAIMBOT: loop claim Polymarket + Relayer PROXY (Node)"
```

### 1.4 Crea il repository su GitHub

1. Vai su [github.com/new](https://github.com/new)
2. Nome repo (es. `claimbot` o `CLAIMBOT`)
3. **Non** spuntare “Add a README” (ce l’hai già in locale)
4. Crea il repository

### 1.5 Collega e pusha

Sostituisci `TUO_USERNAME` e `NOME_REPO` con i tuoi:

```bash
git remote add origin https://github.com/TUO_USERNAME/NOME_REPO.git
git branch -M main
git push -u origin main
```

Se GitHub ti chiede autenticazione, usa un **Personal Access Token** (Settings → Developer settings → Personal access tokens) al posto della password.

---

## 2. Su Render: crea il Background Worker

### 2.1 Nuovo servizio

1. Vai su [dashboard.render.com](https://dashboard.render.com)
2. **New +** → **Background Worker**

### 2.2 Collega GitHub

1. **Connect a repository** → autorizza Render su GitHub se serve
2. Scegli il repo (es. `TUO_USERNAME/claimbot`)
3. Conferma

### 2.3 Impostazioni del worker

| Campo | Valore |
|--------|--------|
| **Name** | `claimbot` (o come preferisci) |
| **Region** | Scegli la più vicina (es. Frankfurt) |
| **Branch** | `main` |
| **Root Directory** | lascia vuoto |
| **Runtime** | **Docker** |
| **Dockerfile Path** | `Dockerfile` (default) |

### 2.4 Variabili d’ambiente (Environment)

In **Environment** aggiungi **tutte** le variabili che hai nel `.env` locale (solo valori, niente `export`):

- `PRIVATE_KEY`
- `POLY_SAFE_ADDRESS`
- `SIGNATURE_TYPE`
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`
- `BUILDER_API_KEY`
- `BUILDER_SECRET`
- `BUILDER_PASSPHRASE`
- `PROXY_URL` (se ti serve il proxy)

Usa **Add Environment Variable** e incolla nome e valore. Per i segreti usa **Secret** dove disponibile.

### 2.5 Avvio

- **Start Command**: lascia vuoto (il Dockerfile usa già `CMD ["python3", "check_cash.py"]`).

Clicca **Create Background Worker**. Render farà build (Docker) e avvierà il worker; nei **Logs** vedrai l’output di `check_cash.py` (cash, claim, attesa 10 min, ecc.).

---

## 3. Cosa fa il worker su Render

- Esegue in loop `python3 check_cash.py`.
- Ogni ciclo: legge il cash, chiede le posizioni claimabili, esegue i claim via Node (Relayer PROXY), aspetta 10 minuti, ripete.
- Se non ci sono claim, stampa “Claim disponibili: 0” e aspetta comunque 10 minuti.

---

## 4. Note

- **Proxy**: se sei in un paese bloccato, su Render il worker potrebbe non aver bisogno di `PROXY_URL` (IP datacenter). Se le chiamate falliscono, prova ad aggiungere `PROXY_URL` come su locale.
- **Node senza proxy**: lo script Node per il claim viene lanciato senza `HTTP_PROXY`/`HTTPS_PROXY` per evitare errori “plain HTTP to HTTPS”. Su Render di solito non serve proxy.
- **Costo**: il piano free di Render ha limiti (ore/mese per i worker). Controlla [render.com/pricing](https://render.com/pricing).
- **Log**: per vedere cosa fa il bot usa **Logs** nel servizio su Render.

---

## 5. Riepilogo comandi (copia-incolla)

```bash
cd /Users/matteogianino/Desktop/CLAIMBOT
git init
git add .
git status
git commit -m "CLAIMBOT: loop claim Polymarket + Relayer PROXY (Node)"
# Poi su GitHub: crea repo, poi:
git remote add origin https://github.com/TUO_USERNAME/NOME_REPO.git
git branch -M main
git push -u origin main
```

Dopo il push: su Render → New → Background Worker → connetti il repo → Runtime: Docker → imposta le env → Create.
