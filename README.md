# VaxiJen API

FastAPI server for protein vaccine candidate prediction using [VaxiJen](https://www.ddg-pharmfac.net/vaxijen/).

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/docs for interactive Swagger UI.

## API

### `POST /predict`

```json
{
  "sequence": "GAVLIPFYW",
  "organism": "bacteria",
  "threshold": 0.5
}
```

Response:
```json
{
  "prediction": "ANTIGEN",
  "score": 2.178,
  "organism": "bacteria"
}
```

### `POST /predict/batch`

```json
{
  "sequences": [
    {"sequence": "GAVLIPFYW", "organism": "bacteria"},
    {"sequence": "ACDEFGHIKLMNPQRSTVWY", "organism": "virus"}
  ]
}
```

### `GET /targets`

Returns available organism targets: bacteria, virus, tumour, parasite, fungal.

## How it works

1. On first request, launches Chrome to bypass Cloudflare and extract cookies
2. Uses httpx with those cookies for fast predictions (~0.3s each)
3. Cookies cached for 1 hour — no browser needed after initial setup
4. If cookies expire, automatically relaunches browser

## Example curl

```bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"sequence": "GAVLIPFYW", "organism": "bacteria"}'
```
