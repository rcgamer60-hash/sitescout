import os
import asyncio
import json
from datetime import datetime
from typing import Optional

import anthropic
import stripe
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(dotenv_path="../.env", override=True)

from auth import create_token, get_current_user, hash_password, verify_password
from database import FREE_SEARCH_LIMIT, get_conn, init_db
from places import search_businesses, get_place_details
from scraper import check_website_quality
from scanner import get_market_mode, get_scan, get_strength

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
STRIPE_AGENCY_PRICE_ID = os.getenv("STRIPE_AGENCY_PRICE_ID", "")
STRIPE_SCANNER_PRICE_ID = os.getenv("STRIPE_SCANNER_PRICE_ID", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

OUTREACH_TOOL = {
    "name": "outreach_package",
    "description": "Cold outreach copy package for a local business lead: email, SMS, and call script.",
    "input_schema": {
        "type": "object",
        "properties": {
            "email_subject": {"type": "string"},
            "email_body": {"type": "string"},
            "sms_text": {"type": "string", "description": "Hard max 155 characters"},
            "call_script": {"type": "string"},
        },
        "required": ["email_subject", "email_body", "sms_text", "call_script"],
    },
}


def ai() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


app = FastAPI(title="SiteScout")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.on_event("startup")
def startup():
    try:
        init_db()
        admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        if admin_email:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE users SET subscription_status='active', subscription_plan='agency'
                       WHERE email = %s""",
                    (admin_email,),
                )
    except Exception as e:
        # DB may be temporarily unavailable (e.g. Supabase paused) — app still boots
        print(f"[startup] DB init warning: {e}")


@app.get("/")
def root():
    return FileResponse("../frontend/index.html")


@app.get("/bypass")
def magic_bypass(key: str = ""):
    bypass_key = os.getenv("BYPASS_KEY", "").strip()
    if not bypass_key or key != bypass_key:
        raise HTTPException(403, "Invalid bypass key")
    admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    if not admin_email:
        raise HTTPException(500, "No admin email configured")
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", (admin_email,)).fetchone()
    if not row:
        raise HTTPException(404, "Admin account not found — sign up first at /")
    token = create_token(row["id"])
    html = f"""<!DOCTYPE html><html><head><title>Logging in...</title></head><body>
<script>
  localStorage.setItem('ss_token', '{token}');
  window.location.href = '/';
</script>
<p>Redirecting...</p>
</body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════

class SignupRequest(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = ""
    agency_name: Optional[str] = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None
    agency_name: Optional[str] = None


@app.post("/api/auth/signup")
def signup(req: SignupRequest):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    hashed = hash_password(req.password)
    try:
        with get_conn() as conn:
            row = conn.execute(
                """INSERT INTO users (email, password_hash, full_name, agency_name)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (req.email.lower().strip(), hashed,
                 req.full_name or "", req.agency_name or ""),
            ).fetchone()
            user_id = row["id"]
    except Exception:
        raise HTTPException(400, "Email already registered")
    token = create_token(user_id)
    return {"token": token, "user_id": user_id}


@app.post("/api/auth/login")
def login(req: LoginRequest):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = %s", (req.email.lower().strip(),)
        ).fetchone()
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    token = create_token(row["id"])
    return {"token": token, "user": _safe_user(dict(row))}


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return _safe_user(user)


@app.patch("/api/auth/me")
def update_profile(req: UpdateProfileRequest, user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Nothing to update")
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = %s",
            [*fields.values(), user["id"]],
        )
    return {"updated": True}


def _safe_user(u: dict) -> dict:
    return {
        "id": u["id"],
        "email": u["email"],
        "full_name": u.get("full_name") or "",
        "agency_name": u.get("agency_name") or "",
        "subscription_status": u.get("subscription_status") or "free",
        "subscription_plan": u.get("subscription_plan") or "",
        "searches_used": u.get("searches_used") or 0,
        "scanner_addon_status": u.get("scanner_addon_status") or "inactive",
    }


def require_scanner_access(user: dict = Depends(get_current_user)) -> dict:
    if user.get("scanner_addon_status") != "active" and user.get("email", "").lower() != ADMIN_EMAIL:
        raise HTTPException(402, "Scanner add-on required")
    return user


# ═══════════════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════════════

class SearchRequest(BaseModel):
    query: Optional[str] = ""
    location: str
    max_results: int = 20


@app.post("/api/search")
async def search(req: SearchRequest, user: dict = Depends(get_current_user)):
    is_paid = user.get("subscription_status") == "active"
    searches_used = user.get("searches_used") or 0

    if not is_paid and searches_used >= FREE_SEARCH_LIMIT:
        raise HTTPException(402, f"Free plan limited to {FREE_SEARCH_LIMIT} searches. Upgrade to Pro for unlimited.")

    search_query = (req.query or "").strip() or "local businesses"

    # Pull wide net from Google Places — fetch 60 raw, filter down to opportunities
    places = await search_businesses(search_query, req.location, 60)
    if not places:
        return {"found": 0, "results": []}

    # Enrich: fetch phone/website + check quality in parallel
    details_list = await asyncio.gather(
        *[get_place_details(p["place_id"]) for p in places],
        return_exceptions=True,
    )

    websites = [
        (d.get("website") if not isinstance(d, Exception) else None)
        for d in details_list
    ]

    async def _quality(url):
        if not url:
            return {}
        return await check_website_quality(url)

    quality_list = await asyncio.gather(
        *[_quality(w) for w in websites],
        return_exceptions=True,
    )

    enriched = []
    for place, details, quality in zip(places, details_list, quality_list):
        if isinstance(details, Exception):
            details = {}
        if isinstance(quality, Exception):
            quality = {}

        website = details.get("website")
        rating = details.get("rating") or place.get("rating")
        review_count = details.get("review_count") or place.get("review_count", 0)
        web_quality = quality.get("quality_score", 0) if isinstance(quality, dict) else 0
        web_alive = quality.get("alive", False) if isinstance(quality, dict) else False

        # Score is purely based on website quality — clean 0-5 scale
        if not website:
            score = 5
            label = "No website"
        elif not web_alive:
            score = 4
            label = "Broken website"
        elif web_quality == 0:
            score = 3
            label = "Weak website"
        elif web_quality == 1:
            score = 2
            label = "Weak website"
        elif web_quality == 2:
            score = 1
            label = "Basic website"
        else:
            score = 0
            label = "Established"

        row = {
            "name": place["name"],
            "business_type": place["business_type"],
            "address": place["address"],
            "phone": details.get("phone"),
            "website": website,
            "rating": rating,
            "review_count": review_count,
            "presence_score": score,
            "presence_label": label,
        }
        enriched.append(row)

        # Auto-save all opportunity leads (score >= 1) and capture DB id
        if score >= 1:
            with get_conn() as conn:
                exists = conn.execute(
                    "SELECT id FROM leads WHERE user_id = %s AND name = %s AND address = %s",
                    (user["id"], row["name"], row["address"]),
                ).fetchone()
                if exists:
                    row["id"] = exists["id"]
                else:
                    result = conn.execute(
                        """INSERT INTO leads
                           (user_id, name, business_type, address, phone, website,
                            presence_score, presence_label, status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'new')
                           RETURNING id""",
                        (user["id"], row["name"], row["business_type"], row["address"],
                         row["phone"], row["website"], row["presence_score"], row["presence_label"]),
                    ).fetchone()
                    row["id"] = result["id"]

    # Only surface actual opportunities — drop Established (score=0) from results
    enriched = [r for r in enriched if r.get("presence_score", 0) >= 1]
    enriched.sort(key=lambda r: r["presence_score"], reverse=True)

    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET searches_used = searches_used + 1 WHERE id = %s",
            (user["id"],),
        )
        conn.execute(
            "INSERT INTO searches (user_id, query, location, results_count) VALUES (%s, %s, %s, %s)",
            (user["id"], search_query, req.location, len(enriched)),
        )

    return {
        "found": len(enriched),
        "results": enriched,
        "searches_used": searches_used + 1,
    }


# ═══════════════════════════════════════════════════════════
# LEADS
# ═══════════════════════════════════════════════════════════

class LeadUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/leads")
def list_leads(
    status: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    with get_conn() as conn:
        q = "SELECT * FROM leads WHERE user_id = %s"
        params = [user["id"]]
        if status:
            q += " AND status = %s"
            params.append(status)
        q += " ORDER BY presence_score DESC, created_at DESC"
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, body: LeadUpdate, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM leads WHERE id = %s AND user_id = %s",
            (lead_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            raise HTTPException(400, "Nothing to update")
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        conn.execute(
            f"UPDATE leads SET {set_clause} WHERE id = %s",
            [*fields.values(), lead_id],
        )
    return {"updated": True}


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM leads WHERE id = %s AND user_id = %s",
            (lead_id, user["id"]),
        )
    return {"deleted": True}


# ═══════════════════════════════════════════════════════════
# AI OUTREACH — one Sonnet tool call generates email+SMS+call script together
# ═══════════════════════════════════════════════════════════

@app.post("/api/leads/{lead_id}/outreach")
def generate_outreach(lead_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE id = %s AND user_id = %s",
            (lead_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        lead = dict(row)

        existing = conn.execute(
            "SELECT * FROM outreach WHERE lead_id = %s AND user_id = %s ORDER BY id DESC LIMIT 1",
            (lead_id, user["id"]),
        ).fetchone()
        if existing:
            return dict(existing)

    agency = user.get("agency_name") or "my agency"
    sender = user.get("full_name") or "the team"
    presence = lead.get("presence_label", "No website")
    biz_name = lead["name"]
    biz_type = lead.get("business_type") or "local business"
    city = (lead.get("address") or "").split(",")[0]

    first_name = sender.split()[0] if sender else "Ryan"
    outreach_prompt = f"""You are {sender} from {agency}, a web design agency reaching out to local businesses.

Business: {biz_name}
Type: {biz_type}
City: {city}
Website status: {presence}

Write a complete cold outreach package using the outreach_package tool: an email, an SMS, and a call script — all for the same business, all sounding like the same real person, never robotic or template-like.

EMAIL rules:
- Under 120 words
- Reference their exact situation: {presence.lower()}
- Mention you can show them a FREE website mockup — no strings attached
- Soft CTA: ask if they'd be open to seeing it (yes/no question)
- Sign as {sender} from {agency}

SMS rules:
- HARD MAX 155 characters — count every character
- One clause naming the problem, one clause offering a free mockup
- End with "Worth a look? - {first_name}"
- No emojis, no links

CALL SCRIPT rules:
- 30-second cold call opener with stage directions in [brackets]
- Opener (5 sec): name drop + quick permission ask
- Hook (10 sec): call out their exact situation ({presence.lower()}), make it feel personal not generic
- Offer (10 sec): free mockup, no commitment, takes 20 min to build
- Close (5 sec): soft yes/no question — "Would it be worth a 2-minute look?"
- Under 120 words total"""

    outreach_resp = ai().messages.create(
        model=SONNET,
        max_tokens=900,
        tools=[OUTREACH_TOOL],
        tool_choice={"type": "tool", "name": "outreach_package"},
        messages=[{"role": "user", "content": outreach_prompt}],
    )
    package = next(
        block.input for block in outreach_resp.content
        if block.type == "tool_use" and block.name == "outreach_package"
    )
    subject = package["email_subject"].strip()
    email_body = package["email_body"].strip()
    sms_text = package["sms_text"].strip()
    if len(sms_text) > 160:
        sms_text = sms_text[:157] + "..."
    call_script = package["call_script"].strip()

    with get_conn() as conn:
        row = conn.execute(
            """INSERT INTO outreach (lead_id, user_id, email_subject, email_body, sms_text)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (lead_id, user["id"], subject, email_body, sms_text),
        ).fetchone()

    result = dict(row)
    result["call_script"] = call_script
    return result


@app.post("/api/leads/{lead_id}/meta-ad")
def generate_meta_ad(lead_id: int, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE id = %s AND user_id = %s",
            (lead_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        lead = dict(row)

    biz_name = lead["name"]
    biz_type = lead.get("business_type") or "local business"
    city = (lead.get("address") or "").split(",")[0].strip()
    presence = lead.get("presence_label", "No website")

    prompt = f"""You are an expert Meta (Facebook/Instagram) ad copywriter specializing in local service businesses.

Business: {biz_name}
Type: {biz_type}
City: {city}
Website status: {presence}

Write 3 Meta ad variants for a web design agency pitching this business. Each ad should use a different angle:
1. "Pain" — lead with the problem their bad/missing website is causing them
2. "Social proof" — lead with what other local businesses gained from a real website
3. "Urgency/Offer" — lead with a free mockup offer and limited availability

Rules based on what converts in Meta ads:
- Start each primary text with a location callout: "{city} [business type] owners:"
- Keep primary text under 150 words
- Headline under 40 chars
- Use transformation language, star ratings (⭐⭐⭐⭐⭐), and scarcity where natural
- CTAs: "Book Now", "Get a Free Quote", or "Send Message"
- Visual guidance: what photo/video they should use

Return ONLY valid JSON, no markdown:
{{"ads": [
  {{"label": "Pain", "primary_text": "...", "headline": "...", "cta": "...", "visual": "..."}},
  {{"label": "Social Proof", "primary_text": "...", "headline": "...", "cta": "...", "visual": "..."}},
  {{"label": "Urgency/Offer", "primary_text": "...", "headline": "...", "cta": "...", "visual": "..."}}
]}}"""

    resp = ai().messages.create(
        model=SONNET,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    import json as _json
    raw = resp.content[0].text.strip()
    try:
        return _json.loads(raw)
    except Exception:
        import re
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return _json.loads(match.group())
        raise HTTPException(500, "Failed to parse ad copy")


# ═══════════════════════════════════════════════════════════
# TRADING SCANNER (paid add-on)
# ═══════════════════════════════════════════════════════════

@app.get("/api/trading/market-mode")
async def trading_market_mode(user: dict = Depends(require_scanner_access)):
    return await get_market_mode()


@app.get("/api/trading/scan")
async def trading_scan(user: dict = Depends(require_scanner_access)):
    return await get_scan()


@app.get("/api/trading/strength")
async def trading_strength(user: dict = Depends(require_scanner_access)):
    return await get_strength()


# ═══════════════════════════════════════════════════════════
# BILLING — Stripe Checkout
# ═══════════════════════════════════════════════════════════

_PLAN_PRICE_IDS = {
    "pro": STRIPE_PRO_PRICE_ID,
    "agency": STRIPE_AGENCY_PRICE_ID,
    "scanner": STRIPE_SCANNER_PRICE_ID,
}


@app.get("/api/billing/checkout/{plan}")
def create_checkout(plan: str, user: dict = Depends(get_current_user)):
    if not stripe.api_key:
        raise HTTPException(503, "Stripe not configured")

    price_id = _PLAN_PRICE_IDS.get(plan)
    if not price_id:
        raise HTTPException(503, f"Stripe price ID for '{plan}' not configured")

    base_url = os.getenv("APP_URL", "https://sitescout.onrender.com")

    try:
        customer_id = user.get("stripe_customer_id")
        if not customer_id:
            customer = stripe.Customer.create(
                email=user["email"],
                metadata={"user_id": str(user["id"])},
            )
            customer_id = customer["id"]
            with get_conn() as conn:
                conn.execute(
                    "UPDATE users SET stripe_customer_id = %s WHERE id = %s",
                    (customer_id, user["id"]),
                )

        success_param = "scanner_upgraded=1" if plan == "scanner" else "upgraded=1"
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{base_url}/?{success_param}",
            cancel_url=f"{base_url}/?canceled=1",
            metadata={"user_id": str(user["id"]), "plan": plan},
        )
        return {"url": session.url}
    except stripe.StripeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, "Invalid webhook signature")

    etype = event["type"]

    if etype in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        sub = event["data"]["object"]
        customer_id = sub["customer"]
        status = sub["status"]
        deleted = etype == "customer.subscription.deleted"

        price_id = None
        if sub.get("items", {}).get("data"):
            price_id = sub["items"]["data"][0]["price"]["id"]

        # Each subscription only ever carries ONE of our products' price_id — branch
        # on it so a scanner add-on event never clobbers the pro/agency plan columns
        # (and vice versa) for customers who have both.
        if price_id == STRIPE_SCANNER_PRICE_ID:
            with get_conn() as conn:
                conn.execute(
                    """UPDATE users SET scanner_addon_status = %s, scanner_stripe_subscription_id = %s
                       WHERE stripe_customer_id = %s""",
                    ("inactive" if deleted else ("active" if status == "active" else "canceled"),
                     None if deleted else sub["id"], customer_id),
                )
        elif price_id in (STRIPE_PRO_PRICE_ID, STRIPE_AGENCY_PRICE_ID):
            plan = "" if deleted else ("pro" if price_id == STRIPE_PRO_PRICE_ID else "agency")
            new_status = "free" if deleted else ("active" if status == "active" else "canceled")
            with get_conn() as conn:
                conn.execute(
                    """UPDATE users SET subscription_status = %s, subscription_plan = %s
                       WHERE stripe_customer_id = %s""",
                    (new_status, plan, customer_id),
                )

    return {"received": True}
