import os, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
from datetime import timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.models import utcnow


def register(c, email, pw="Passw0rd!x"):
    r = c.post("/api/auth/register", json={"email": email, "password": pw})
    return {"Authorization": "Bearer " + r.json()["token"]}


def admin(c):
    r = c.post("/api/auth/login", json={"email": "admin@vutto.test", "password": "Admin@12345"})
    return {"Authorization": "Bearer " + r.json()["token"]}


def new_auction(c, h, start_off=-1, end_off=60):
    now = utcnow()
    r = c.post("/api/auctions", headers=h, json={
        "title": "Test Bike", "start_price": 1000, "min_increment": 100,
        "starts_at": (now + timedelta(minutes=start_off)).isoformat(),
        "ends_at": (now + timedelta(minutes=end_off)).isoformat()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bid(c, aid, h, amount):
    return c.post(f"/api/auctions/{aid}/bids", headers=h, json={"amount": amount}).status_code


def test_bid_rules():
    with TestClient(app) as c:
        aid, bh = new_auction(c, admin(c)), register(c, "a@x.com")
        assert bid(c, aid, bh, 900) == 409      # below start price
        assert bid(c, aid, bh, 1000) == 201     # opening bid
        assert bid(c, aid, bh, 1050) == 409     # below min increment
        assert bid(c, aid, bh, 1100) == 201
        d = c.get(f"/api/auctions/{aid}").json()
        assert d["current_price"] == 1100 and d["bid_count"] == 2 and len(d["bids"]) == 2


def test_permissions_and_validation():
    with TestClient(app) as c:
        bh = register(c, "b@x.com")
        assert c.post("/api/auctions", headers=bh, json={}).status_code in (403, 422)
        assert c.post("/api/auctions/1/bids", json={"amount": 5}).status_code == 401
        assert c.post("/api/auth/register", json={"email": "bad", "password": "Passw0rd!x"}).status_code == 422
        assert c.post("/api/auth/login", json={"email": "b@x.com", "password": "wrongpass1"}).status_code == 401


def test_upcoming_ended_cancelled_reject_bids():
    with TestClient(app) as c:
        ah, bh = admin(c), register(c, "c@x.com")
        up = new_auction(c, ah, 30, 90)
        ended = new_auction(c, ah, -60, -1)
        live = new_auction(c, ah)
        assert c.delete(f"/api/auctions/{live}", headers=ah).status_code == 200
        for aid in (up, ended, live):
            assert bid(c, aid, bh, 5000) == 409
