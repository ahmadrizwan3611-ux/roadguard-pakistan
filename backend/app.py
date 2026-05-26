from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import re, os, json, logging, requests, hmac, hashlib, time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIGURATION ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_BUILD_DIR = os.path.join(BASE_DIR, "frontend", "build")
ROOT_STATIC_DIR = os.path.join(BASE_DIR, "static")
REACT_BUILD_DIR = (
    FRONTEND_BUILD_DIR
    if os.path.exists(os.path.join(FRONTEND_BUILD_DIR, "index.html"))
    else ROOT_STATIC_DIR
)

app = Flask(__name__, static_folder=REACT_BUILD_DIR, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", 5 * 1024 * 1024))

allowed_origins = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")
CORS(
    app,
    resources={r"/*": {"origins": [o.strip() for o in allowed_origins if o.strip()]}},
    supports_credentials=True,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
)


# ==================== GROQ HELPER ====================
def call_groq(prompt: str, max_tokens: int = 800):
    if not GROQ_API_KEY:
        return {"success": False, "error": "GROQ_API_KEY not set"}
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        data = r.json()
        if r.status_code == 200:
            return {"success": True, "text": data["choices"][0]["message"]["content"]}
        return {"success": False, "error": data.get("error", {}).get("message")}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== SUPABASE HELPER ====================
def supabase_request(method, path, data=None, use_service_key=False):
    key = SUPABASE_SERVICE_KEY if use_service_key else SUPABASE_ANON_KEY
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key if use_service_key else key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=15)
        elif method == "POST":
            r = requests.post(url, headers=headers, json=data, timeout=15)
        elif method == "PATCH":
            r = requests.patch(url, headers=headers, json=data, timeout=15)
        else:
            return {"error": "Method not supported"}

        if r.status_code >= 400:
            return {"error": r.json() if r.text else r.text}
        return r.json() if r.text else {}
    except Exception as e:
        return {"error": str(e)}


def require_auth(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        # Simple user fetch
        headers = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"}
        try:
            r = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=headers)
            if r.status_code == 200:
                request.user = r.json()
                return f(*args, **kwargs)
        except:
            pass
        return jsonify({"error": "Unauthorized"}), 401

    return decorated


# ==================== ROADGUARD ROUTES ====================


def clean_location(lat, lng):
    try:
        return float(lat), float(lng)
    except:
        return None, None


@app.route("/roadguard/report", methods=["POST"])
@require_auth
def report_hazard():
    data = request.get_json(silent=True) or {}
    lat, lng = clean_location(data.get("latitude"), data.get("longitude"))

    if not lat or not lng:
        return jsonify({"error": "Valid location is required"}), 400

    result = supabase_request(
        "POST",
        "road_hazards",
        {
            "reported_by": request.user["id"],
            "latitude": lat,
            "longitude": lng,
            "hazard_type": data.get("type", "other"),
            "description": str(data.get("description", ""))[:500],
            "photo_url": data.get("photo_url"),
            "status": "pending",
        },
        use_service_key=True,
    )

    if "error" in result:
        return jsonify({"error": "Failed to save report"}), 400

    return (
        jsonify(
            {
                "success": True,
                "message": "Hazard reported successfully. Thank you for making roads safer!",
            }
        ),
        201,
    )


@app.route("/roadguard/nearby", methods=["GET"])
def get_nearby_hazards():
    lat, lng = clean_location(request.args.get("lat"), request.args.get("lng"))
    radius = float(request.args.get("radius", 15))

    if not lat or not lng:
        return jsonify({"error": "lat and lng are required"}), 400

    hazards = supabase_request(
        "GET",
        "road_hazards?status=eq.active&select=*&order=created_at.desc",
        use_service_key=True,
    )

    nearby = []
    for h in (hazards if isinstance(hazards, list) else []):
        h_lat = h.get("latitude")
        h_lng = h.get("longitude")
        if h_lat and h_lng:
            distance = ((h_lat - lat) ** 2 + (h_lng - lng) ** 2) ** 0.5 * 111
            if distance <= radius:
                nearby.append(h)

    return jsonify({"hazards": nearby[:50]}), 200


@app.route("/roadguard/my-reports", methods=["GET"])
@require_auth
def get_my_reports():
    reports = supabase_request(
        "GET",
        f"road_hazards?reported_by=eq.{request.user['id']}&select=*&order=created_at.desc",
        use_service_key=True,
    )
    return jsonify({"reports": reports if isinstance(reports, list) else []}), 200


@app.route("/roadguard/analyze", methods=["POST"])
@require_auth
def analyze_road_report():
    data = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()

    if not description:
        return jsonify({"error": "Description is required"}), 400

    prompt = f"""You are a road safety expert in Pakistan.

Analyze this road hazard report:

"{description}"

Return response in this exact format:

**Risk Level:** High / Medium / Low
**Possible Causes:**
**Recommended Actions for Drivers:**
**Recommended Actions for Authorities:**"""

    result = call_groq(prompt)
    analysis = (
        result["text"] if result["success"] else "AI analysis is currently unavailable."
    )

    return jsonify({"analysis": analysis}), 200


# ==================== BASIC HEALTH ====================
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify(
        {
            "status": "healthy",
            "service": "RoadGuard",
            "message": "Road Safety Platform for Pakistan",
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
