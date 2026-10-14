import json, logging, os, time, uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .models import Auction, Base, Bid, SessionLocal, User, engine, utcnow
from .security import admin_only, current_user, get_db, hash_password, make_token, verify_password

STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


# ---------- logging & metrics ----------
class JsonFormatter(logging.Formatter):
    def format(self, r):
        d = {"ts": self.formatTime(r), "level": r.levelname, "msg": r.getMessage()}
        d.update(getattr(r, "extra_fields", {}))
        return json.dumps(d)


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
log = logging.getLogger("auction")
log.addHandler(handler)
log.setLevel(logging.INFO)
metrics = Counter()


# ---------- websocket rooms (single process; see README for scaling) ----------
class Rooms:
    def __init__(self):
        self.rooms: dict[int, set[WebSocket]] = defaultdict(set)

    async def broadcast(self, aid: int, message: dict):
        for ws in list(self.rooms[aid]):
            try:
                await ws.send_json(message)
            except Exception:
                self.rooms[aid].discard(ws)


rooms = Rooms()


# ---------- helpers ----------
def status_of(a: Auction) -> str:
    now = utcnow()
    if a.cancelled:
        return "cancelled"
    if now < a.starts_at:
        return "upcoming"
    return "live" if now < a.ends_at else "ended"


def auction_out(a: Auction) -> dict:
    return {
        "id": a.id, "title": a.title, "description": a.description, "image_url": a.image_url,
        "start_price": a.start_price, "min_increment": a.min_increment,
        "current_price": a.current_price, "bid_count": a.bid_count, "leader_id": a.leader_id,
        "starts_at": a.starts_at.isoformat() + "Z", "ends_at": a.ends_at.isoformat() + "Z",
        "status": status_of(a),
    }


def mask(email: str) -> str:
    return email[0] + "***@" + email.split("@")[1]


def to_utc_naive(d: datetime) -> datetime:
    return d if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)


def seed():
    with SessionLocal() as db:
        if db.scalar(select(User).limit(1)):
            return
        db.add(User(email=os.getenv("ADMIN_EMAIL", "admin@vutto.test"),
                    password_hash=hash_password(os.getenv("ADMIN_PASSWORD", "Admin@12345")), role="admin"))
        db.add(User(email="buyer@vutto.test", password_hash=hash_password("Buyer@12345")))
        now = utcnow()
        demo = [("Royal Enfield Classic 350 (2019)", 85000), ("Yamaha R15 V3 (2020)", 110000),
                ("KTM Duke 200 (2018)", 95000)]
        for i, (t, p) in enumerate(demo):
            db.add(Auction(title=t, description="Single owner, service history available.", start_price=p,
                           current_price=p, min_increment=1000, starts_at=now - timedelta(minutes=5),
                           ends_at=now + timedelta(hours=6 + i)))
        db.commit()


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    seed()
    yield


app = FastAPI(title="Bike Auction Platform", lifespan=lifespan)


@app.middleware("http")
async def observe(request: Request, call_next):
    rid = request.headers.get("x-request-id", uuid.uuid4().hex[:12])
    start = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - start) * 1000, 1)
    metrics[f'http_requests_total{{status="{response.status_code}"}}'] += 1
    if request.url.path.startswith("/api"):
        log.info("request", extra={"extra_fields": {"rid": rid, "method": request.method,
                 "path": request.url.path, "status": response.status_code, "ms": ms}})
    response.headers["x-request-id"] = rid
    return response


# ---------- schemas ----------
class Credentials(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    password: str = Field(min_length=8, max_length=128)


class AuctionIn(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(default="", max_length=2000)
    image_url: str = Field(default="", max_length=500)
    start_price: float = Field(gt=0)
    min_increment: float = Field(default=500, gt=0)
    starts_at: datetime
    ends_at: datetime


class BidIn(BaseModel):
    amount: float = Field(gt=0)


# ---------- routes ----------
@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def prom():
    return "\n".join(f"{k} {v}" for k, v in sorted(metrics.items())) + "\n"


@app.post("/api/auth/register", status_code=201)
def register(c: Credentials, db: Session = Depends(get_db)):
    email = c.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Email already registered")
    user = User(email=email, password_hash=hash_password(c.password))
    db.add(user)
    db.commit()
    return {"token": make_token(user), "role": user.role}


@app.post("/api/auth/login")
def login(c: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == c.email.lower()))
    if not user or not verify_password(c.password, user.password_hash):
        metrics["login_failures_total"] += 1
        raise HTTPException(401, "Invalid email or password")
    return {"token": make_token(user), "role": user.role}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "role": user.role}


@app.get("/api/auctions")
def list_auctions(status: str | None = None, db: Session = Depends(get_db)):
    out = [auction_out(a) for a in db.scalars(select(Auction).order_by(Auction.ends_at)).all()]
    return [a for a in out if not status or a["status"] == status]


@app.get("/api/auctions/{aid}")
def get_auction(aid: int, db: Session = Depends(get_db)):
    a = db.get(Auction, aid)
    if not a:
        raise HTTPException(404, "Auction not found")
    rows = db.execute(select(Bid, User.email).join(User, User.id == Bid.user_id)
                      .where(Bid.auction_id == aid).order_by(Bid.id.desc()).limit(20)).all()
    return {**auction_out(a), "bids": [{"amount": b.amount, "bidder": mask(e),
            "at": b.created_at.isoformat() + "Z"} for b, e in rows]}


@app.post("/api/auctions", status_code=201)
def create_auction(body: AuctionIn, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    s, e = to_utc_naive(body.starts_at), to_utc_naive(body.ends_at)
    if e <= s:
        raise HTTPException(422, "ends_at must be after starts_at")
    a = Auction(title=body.title, description=body.description, image_url=body.image_url,
                start_price=body.start_price, current_price=body.start_price,
                min_increment=body.min_increment, starts_at=s, ends_at=e)
    db.add(a)
    db.commit()
    log.info("auction_created", extra={"extra_fields": {"auction_id": a.id}})
    return auction_out(a)


@app.delete("/api/auctions/{aid}")
def cancel_auction(aid: int, db: Session = Depends(get_db), _: User = Depends(admin_only)):
    a = db.get(Auction, aid)
    if not a:
        raise HTTPException(404, "Auction not found")
    a.cancelled = True
    db.commit()
    anyio.from_thread.run(rooms.broadcast, aid, {"type": "cancelled", "auction": auction_out(a)})
    return auction_out(a)


@app.post("/api/auctions/{aid}/bids", status_code=201)
def place_bid(aid: int, body: BidIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    now, amt = utcnow(), body.amount
    # One atomic conditional UPDATE: the row lock serialises concurrent bidders,
    # so two people can never both win with the same price.
    result = db.execute(update(Auction).where(
        Auction.id == aid, Auction.cancelled.is_(False),
        Auction.starts_at <= now, Auction.ends_at > now,
        or_(and_(Auction.bid_count == 0, Auction.start_price <= amt),
            and_(Auction.bid_count > 0, Auction.current_price + Auction.min_increment <= amt)),
    ).values(current_price=amt, leader_id=user.id, bid_count=Auction.bid_count + 1))
    if result.rowcount == 0:
        db.rollback()
        metrics["bids_rejected_total"] += 1
        a = db.get(Auction, aid)
        if not a:
            raise HTTPException(404, "Auction not found")
        if status_of(a) != "live":
            raise HTTPException(409, f"Auction is {status_of(a)}")
        floor = a.start_price if a.bid_count == 0 else a.current_price + a.min_increment
        raise HTTPException(409, f"Bid too low. Minimum is {floor:g}")
    db.add(Bid(auction_id=aid, user_id=user.id, amount=amt))
    db.commit()
    metrics["bids_accepted_total"] += 1
    a = db.get(Auction, aid)
    anyio.from_thread.run(rooms.broadcast, aid,
                          {"type": "bid", "auction": auction_out(a), "bidder": mask(user.email)})
    return auction_out(a)


@app.websocket("/ws/auctions/{aid}")
async def ws_auction(ws: WebSocket, aid: int):
    await ws.accept()
    rooms.rooms[aid].add(ws)
    metrics["ws_connections_total"] += 1
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from client
    except WebSocketDisconnect:
        rooms.rooms[aid].discard(ws)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
