from fastapi import FastAPI, Request, HTTPException
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

BASE_URL = f"https://{SHOPIFY_STORE}/admin/api/2026-01"
SHOPIFY_HEADERS = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}

genai.configure(api_key=GEMINI_API_KEY)


def verify_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {GESTIONALE_TOKEN}":
        raise HTTPException(status_code=401, detail="Non autorizzato")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/products")
async def api_products(request: Request):
    verify_token(request)
    url = f"{BASE_URL}/products.json?limit=250"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=SHOPIFY_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

    result = []
    for product in data.get("products", []):
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
