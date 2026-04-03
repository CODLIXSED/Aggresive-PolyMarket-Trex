import requests, os, sqlite3, pandas as pd, numpy as np, subprocess, time
from sklearn.ensemble import RandomForestClassifier

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DB = "data.db"

MODE = "AGGRESSIVE"  # AGGRESSIVE / SNIPER

# ================= TELEGRAM =================
def send(msg):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id": CHAT_ID, "text": msg})

# ================= DATABASE (ANTI LOCK) =================
def db():
    return sqlite3.connect(DB, timeout=10)

def execute(q, p=()):
    for _ in range(5):
        try:
            conn = db()
            conn.execute(q, p)
            conn.commit()
            conn.close()
            return
        except:
            time.sleep(1)

def fetch(q):
    for _ in range(5):
        try:
            conn = db()
            df = pd.read_sql(q, conn)
            conn.close()
            return df
        except:
            time.sleep(1)
    return pd.DataFrame()

def init_db():
    execute("CREATE TABLE IF NOT EXISTS market(market TEXT, price REAL, ts DATETIME DEFAULT CURRENT_TIMESTAMP)")
    execute("CREATE TABLE IF NOT EXISTS trades(market TEXT, signal TEXT, conf REAL, ts DATETIME DEFAULT CURRENT_TIMESTAMP)")

# ================= MARKET =================
def get_markets():
    data = requests.get("https://gamma-api.polymarket.com/markets").json()
    out=[]
    for m in data[:10]:
        try:
            out.append((m["question"], float(m.get("lastTradePrice",0.5)), m["id"]))
        except:
            pass
    return out

# ================= ORDERBOOK =================
def imbalance(mid):
    try:
        ob = requests.get(f"https://clob.polymarket.com/orderbook/{mid}").json()
        b = sum(float(x[1]) for x in ob.get("bids",[])[:10])
        a = sum(float(x[1]) for x in ob.get("asks",[])[:10])
        return (b-a)/(b+a+1e-6)
    except:
        return 0

# ================= MACRO =================
def macro():
    try:
        btc = float(requests.get("https://api.coindesk.com/v1/bpi/currentprice.json")
                    .json()["bpi"]["USD"]["rate"].replace(",",""))
        return "RISK_ON" if btc > 30000 else "RISK_OFF"
    except:
        return "NEUTRAL"

# ================= FEATURE =================
def features(df):
    df["ret"] = df.price.pct_change()
    df["mom"] = df.ret.rolling(5).mean()
    df["vol"] = df.ret.rolling(5).std()
    df["ema9"] = df.price.ewm(span=9).mean()
    df["ema21"] = df.price.ewm(span=21).mean()

    delta = df.price.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain/(loss+1e-6)
    df["rsi"] = 100 - (100/(1+rs))

    df = df.dropna()
    if len(df) < 25:
        return None

    last = df.iloc[-1]
    return [last["mom"], last["vol"], last["rsi"], last["ema9"]-last["ema21"]]

# ================= ML =================
def train(df):
    df["future"] = df.price.shift(-3)
    df["y"] = (df.future > df.price).astype(int)
    df = df.dropna()

    if len(df) < 30:
        return None

    X = np.column_stack([
        df.price.pct_change().fillna(0),
        df.price.rolling(5).std().fillna(0),
        df.price.rolling(10).mean().fillna(0)
    ])
    y = df["y"]

    model = RandomForestClassifier(n_estimators=120)
    model.fit(X,y)
    return model

def predict(model, feat):
    if model is None or feat is None:
        return "WAIT",0

    pred = model.predict([feat])[0]
    prob = max(model.predict_proba([feat])[0])

    threshold = 55 if MODE=="AGGRESSIVE" else 70

    if prob*100 < threshold:
        return "NO TRADE", prob*100

    return ("BUY", prob*100) if pred==1 else ("SELL", prob*100)

# ================= RISK =================
def risk(price, signal):
    r = 0.02 if MODE=="AGGRESSIVE" else 0.015
    if signal == "BUY":
        return price*(1-r), price*(1+r*2)
    else:
        return price*(1+r), price*(1-r*2)

# ================= SAVE =================
def save_repo():
    try:
        subprocess.run(["git","config","--global","user.email","bot@bot.com"])
        subprocess.run(["git","config","--global","user.name","bot"])
        subprocess.run(["git","add",DB])
        subprocess.run(["git","commit","-m","update"],check=False)
        subprocess.run(["git","push"])
    except:
        pass

# ================= MAIN =================
def run():
    init_db()

    mkts = get_markets()
    bias = macro()

    best=[]

    for name,price,mid in mkts:
        execute("INSERT INTO market VALUES(?,?,CURRENT_TIMESTAMP)", (name,price))

        df = fetch(f"SELECT price FROM market WHERE market='{name}' ORDER BY ts DESC LIMIT 120")[::-1]

        if len(df) < 30:
            continue

        feat = features(df)
        imb = imbalance(mid)

        model = train(df)
        sig, conf = predict(model, feat)

        if sig in ["WAIT","NO TRADE"]:
            continue

        if MODE=="AGGRESSIVE":
            if conf < 55 or abs(imb)<0.02:
                continue
        else:
            if conf < 70 or abs(imb)<0.05:
                continue
            if bias=="RISK_OFF" and sig=="BUY":
                continue

        score = conf + abs(imb*100)
        best.append((score,name,price,sig,conf,imb))

    best = sorted(best, reverse=True)[:3]

    if best:
        msg = f"🔥 MODE: {MODE}\n"
        for i,(s,n,p,sg,c,imb) in enumerate(best,1):
            sl,tp = risk(p,sg)
            msg += f"""
#{i} {n}
{sg} | {c:.1f}%
Entry {p:.2f}
SL {sl:.2f} TP {tp:.2f}
OB {imb:.2f}
"""
    else:
        msg = f"⚠️ MODE {MODE}: no trade"

    send(msg)
    save_repo()

if __name__ == "__main__":
    run()
