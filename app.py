from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.get("/")
async def home():
    return {"status": "Bot funcionando"}

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()

    print(data)

    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
