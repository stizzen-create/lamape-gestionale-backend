from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import httpx
import json
import base64
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()


async def _keep_alive():
    """Pinga /health ogni 14 minuti per evitare il sleep su Render free tier."""
    await asyncio.sleep(60)  # aspetta 1 min dopo il cold start prima del primo ping
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(
                    "https://lamape-gestionale-backend.onrender.com/health",
                    timeout=10,
                )
        except Exception:
            pass
        await asyncio.sleep(14 * 60)


@app.on_event("startup")
async def startup():
    asyncio.create_task(_keep_alive())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
GESTIONALE_TOKEN = os.getenv("GESTIONALE_TOKEN", "lamape-gestionale-2026")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")

BASE_URL = f"https://{SHOPIFY_STORE}/admin/api/2026-01"
SHOPIFY_HEADERS = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}

genai.configure(api_key=GEMINI_API_KEY)

import asyncio
import time
_products_cache: list = []
_products_cache_time: float = 0
CACHE_TTL = 300  # 5 minuti


def verify_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {GESTIONALE_TOKEN}":
        raise HTTPException(status_code=401, detail="Non autorizzato")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/shopify/auth")
def shopify_auth():
    scopes = "read_products,read_inventory,write_inventory"
    redirect_uri = "https://lamape-gestionale-backend.onrender.com/shopify/callback"
    url = (
        f"https://{SHOPIFY_STORE}/admin/oauth/authorize"
        f"?client_id={SHOPIFY_CLIENT_ID}"
        f"&scope={scopes}"
        f"&redirect_uri={redirect_uri}"
    )
    return RedirectResponse(url)


@app.get("/shopify/callback")
async def shopify_callback(code: str = None, shop: str = None):
    if not code:
        return HTMLResponse("<h1>Errore: codice mancante</h1>")
    redirect_uri = "https://lamape-gestionale-backend.onrender.com/shopify/callback"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
            json={
                "client_id": SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
                "code": code,
            },
            timeout=10,
        )
        data = resp.json()
    token = data.get("access_token", "")
    return HTMLResponse(f"""
    <h2>Token ottenuto!</h2>
    <p>Copia questo token e mandalo all'assistente:</p>
    <code style="font-size:18px;background:#eee;padding:12px;display:block">{token}</code>
    """)


@app.get("/api/products")
async def api_products(request: Request):
    global _products_cache, _products_cache_time
    verify_token(request)

    if _products_cache and (time.time() - _products_cache_time) < CACHE_TTL:
        return _products_cache

    all_products = []
    url = f"{BASE_URL}/products.json?limit=250"
    async with httpx.AsyncClient() as client:
        while url:
            resp = await client.get(url, headers=SHOPIFY_HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            all_products.extend(data.get("products", []))
            # Shopify cursor pagination: segui il link "next" se presente
            link_header = resp.headers.get("Link", "")
            url = None
            for part in link_header.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break

    result = []
    for product in all_products:
        variants = []
        for v in product.get("variants", []):
            variants.append({
                "id": str(v["id"]),
                "inventoryItemId": str(v.get("inventory_item_id", "")),
                "title": v.get("title", ""),
                "sku": v.get("sku", ""),
                "inventoryQuantity": v.get("inventory_quantity", 0),
            })
        result.append({
            "id": str(product["id"]),
            "title": product.get("title", ""),
            "collection": "",
            "variants": variants,
        })

    _products_cache = result
    _products_cache_time = time.time()
    return result


class UpdateInventoryRequest(BaseModel):
    inventoryItemId: str
    quantity: int


@app.post("/api/update-inventory")
async def api_update_inventory(request: Request, body: UpdateInventoryRequest):
    verify_token(request)
    async with httpx.AsyncClient() as client:
        payload = {
            "location_id": int(SHOPIFY_LOCATION_ID),
            "inventory_item_id": int(body.inventoryItemId),
            "available": body.quantity,
        }
        inv_resp = await client.post(
            f"{BASE_URL}/inventory_levels/set.json",
            headers=SHOPIFY_HEADERS,
            json=payload,
            timeout=10
        )
        if inv_resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Shopify {inv_resp.status_code}: {inv_resp.text}")
    _products_cache_time = 0  # invalida cache dopo aggiornamento
    return {"success": True}


class OcrRequest(BaseModel):
    image_base64: str


@app.post("/api/ocr")
async def api_ocr(request: Request, body: OcrRequest):
    verify_token(request)
    prompt = """Sei un sistema OCR per fatture di abbigliamento italiano.
Estrai tutti gli articoli dalla fattura. Ogni articolo ha:
- Una riga col nome prodotto
- Una riga con "Variante: Colore / Taglia" seguita dalla quantità

Rispondi SOLO con JSON valido, array di oggetti con campi: name, color, size, quantity.
Esempio: [{"name":"Camicia Raso","color":"Beige","size":"Taglia Unica","quantity":1}]
Se non trovi articoli rispondi con array vuoto: []"""

    model = genai.GenerativeModel("gemini-2.5-flash")
    image_data = base64.b64decode(body.image_base64)
    response = model.generate_content([
        prompt,
        {"mime_type": "image/jpeg", "data": image_data},
    ])

    raw = response.text.strip()
    start = raw.find('[')
    end = raw.rfind(']') + 1
    if start == -1 or end == 0:
        return []
    return json.loads(raw[start:end])
